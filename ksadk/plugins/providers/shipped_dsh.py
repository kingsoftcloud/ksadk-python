"""Shared implementation for wheel-owned DSH AgentProvider bundles.

Provider-specific modules declare identity and select an execution factory.
This module owns the repeated package validation, fixed host command,
registration fences, and bridge lifecycle.  It does not own an agent loop.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ksadk.plugins.bundle import ResolvedPluginBundle
from ksadk.plugins.contracts import CompositionProfile, PluginManifest
from ksadk.plugins.host import PluginExecutionContext, PluginHostError
from ksadk.plugins.providers.dsh import (
    DshAgentProviderHost,
    DshAgentProviderRegistration,
    dsh_agent_provider_manifest,
)


@dataclass(frozen=True)
class ShippedDshProviderSpec:
    key: str
    display_name: str
    package_name: str
    provider_id: str
    version: str
    bundle_directory: str
    bridge_required_code: str
    bridge_required_message: str

    @property
    def provider_ref(self) -> str:
        return f"plugin://{self.provider_id}@{self.version}"

    def error_code(self, suffix: str) -> str:
        return f"{self.key}_dsh_{suffix}"


CODEX_DSH_SPEC = ShippedDshProviderSpec(
    key="codex",
    display_name="Codex",
    package_name="@kingsoftcloud/ksadk-codex-provider",
    provider_id="io.ksadk.codex-provider",
    version="1.0.0",
    bundle_directory="ksadk-codex",
    bridge_required_code="codex_provider_bridge_required",
    bridge_required_message=(
        "Codex execution requires the registration-gated RuntimeAdapter bridge"
    ),
)

HARNESS_DSH_SPEC = ShippedDshProviderSpec(
    key="harness",
    display_name="KsADK Harness",
    package_name="@kingsoftcloud/ksadk-harness-provider",
    provider_id="io.ksadk.harness-provider",
    version="1.0.0",
    bundle_directory="ksadk-harness",
    bridge_required_code="harness_legacy_bridge_required",
    bridge_required_message=(
        "Harness execution requires the registration-gated RuntimeAdapter bridge"
    ),
)

SHIPPED_DSH_PROVIDER_SPECS = {
    CODEX_DSH_SPEC.key: CODEX_DSH_SPEC,
    HARNESS_DSH_SPEC.key: HARNESS_DSH_SPEC,
}


@dataclass(frozen=True)
class ShippedDshBundle:
    package_name: str
    version: str
    root: Path
    patch: Path


def load_shipped_dsh_bundle(spec: ShippedDshProviderSpec) -> ShippedDshBundle:
    """Locate and validate one immutable DSH bundle shipped in the wheel."""

    root = Path(__file__).with_name("bundles") / spec.bundle_directory
    package_path = root / "package.json"
    try:
        payload = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PluginHostError(
            spec.error_code("bundle_invalid"),
            f"shipped {spec.display_name} DSH Bundle package.json is unavailable",
        ) from error
    if not isinstance(payload, dict):
        raise PluginHostError(
            spec.error_code("bundle_invalid"),
            f"shipped {spec.display_name} DSH Bundle package.json must be an object",
        )
    dsh = payload.get("dsh")
    bundle = dsh.get("bundle") if isinstance(dsh, dict) else None
    patch_value = bundle.get("patch") if isinstance(bundle, dict) else None
    if (
        payload.get("name") != spec.package_name
        or payload.get("version") != spec.version
        or patch_value != "./cordis.patch.yml"
        or "ksadk" in payload
    ):
        raise PluginHostError(
            spec.error_code("bundle_invalid"),
            f"shipped {spec.display_name} provider is not a standard pinned DSH Bundle",
        )
    patch = (root / str(patch_value)).resolve()
    try:
        patch.relative_to(root.resolve())
    except ValueError as error:
        raise PluginHostError(
            spec.error_code("bundle_invalid"),
            "DSH Bundle patch escapes its package root",
        ) from error
    if not patch.is_file() or not (root / "index.mjs").is_file():
        raise PluginHostError(
            spec.error_code("bundle_invalid"),
            "DSH Bundle contribution files are missing",
        )
    return ShippedDshBundle(
        package_name=spec.package_name,
        version=spec.version,
        root=root.resolve(),
        patch=patch,
    )


def shipped_dsh_host_command(
    spec: ShippedDshProviderSpec,
    *,
    python_executable: str | None = None,
) -> tuple[str, ...]:
    """Return fixed argv; bundle content never controls the executable."""

    load_shipped_dsh_bundle(spec)
    executable = str(python_executable or sys.executable).strip()
    if not executable or "\x00" in executable:
        raise PluginHostError(spec.error_code("host_invalid"), "Python host executable is invalid")
    return executable, "-m", "ksadk.plugins.providers.dsh_descriptor_host", spec.key


class _ExecutionRuntime(Protocol):
    async def start(self) -> None: ...

    async def health(self) -> bool: ...

    async def prepare(
        self,
        bundle: ResolvedPluginBundle,
        *,
        capabilities: PluginExecutionContext,
    ) -> Any: ...

    async def drain(self) -> None: ...

    async def dispose(self) -> None: ...


class _ExecutionFactory(Protocol):
    async def stage(
        self,
        manifest: PluginManifest,
        *,
        profile: CompositionProfile,
        services: Mapping[str, Any],
    ) -> _ExecutionRuntime: ...


class ShippedDshBridgeRuntime:
    """DSH registration lifecycle delegating execution to the owning runtime."""

    def __init__(
        self,
        *,
        spec: ShippedDshProviderSpec,
        host: DshAgentProviderHost,
        registration: DshAgentProviderRegistration,
        execution_runtime: _ExecutionRuntime,
        owns_host: bool,
    ) -> None:
        self._spec = spec
        self._host = host
        self._registration = registration
        self._execution_runtime = execution_runtime
        self._owns_host = owns_host
        self._started = False
        self._disposed = False

    async def start(self) -> None:
        if self._disposed:
            raise PluginHostError(
                self._spec.error_code("bridge_disposed"),
                f"{self._spec.display_name} DSH bridge is disposed",
            )
        current = await self._host.registration()
        require_same_registration(self._spec, current, self._registration)
        await self._execution_runtime.start()
        self._started = True

    async def health(self) -> bool:
        if not self._started or self._disposed:
            return False
        return await self._host.health() and await self._execution_runtime.health()

    async def prepare(
        self,
        bundle: ResolvedPluginBundle,
        *,
        capabilities: PluginExecutionContext,
    ) -> Any:
        if not await self.health():
            raise PluginHostError(
                self._spec.error_code("bridge_unavailable"),
                f"{self._spec.display_name} DSH provider is not ready",
            )
        if bundle.composition.profile.agent_provider.ref != self._spec.provider_ref:
            raise PluginHostError(
                self._spec.error_code("profile_mismatch"),
                "Agent Bundle does not select the registered "
                f"{self._spec.display_name} DSH provider",
            )
        return await self._execution_runtime.prepare(bundle, capabilities=capabilities)

    async def drain(self) -> None:
        if self._disposed:
            return
        await self._execution_runtime.drain()
        if self._owns_host and self._host.pid is not None:
            await self._host.drain()
        self._started = False

    async def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._started = False
        first_error: BaseException | None = None
        try:
            await self._execution_runtime.dispose()
        except BaseException as error:  # cleanup must continue
            first_error = error
        if self._owns_host:
            try:
                await self._host.dispose()
            except BaseException as error:  # cleanup must continue
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error


class ShippedDshBridgeFactory:
    """Admit one exact DSH registration and create its execution bridge."""

    runtime_class = ShippedDshBridgeRuntime

    def __init__(
        self,
        *,
        spec: ShippedDshProviderSpec,
        host: DshAgentProviderHost,
        registration: DshAgentProviderRegistration,
        execution_factory: _ExecutionFactory,
        owns_host: bool = True,
    ) -> None:
        validate_registration(spec, registration)
        self._spec = spec
        self._host = host
        self._registration = registration
        self._execution_factory = execution_factory
        self._owns_host = owns_host
        self.runtime: ShippedDshBridgeRuntime | None = None

    async def stage(
        self,
        manifest: PluginManifest,
        *,
        profile: CompositionProfile,
        services: Mapping[str, Any],
    ) -> ShippedDshBridgeRuntime:
        expected_manifest = dsh_agent_provider_manifest(
            self._registration.descriptor,
            preflight=self._registration.preflight,
        )
        if manifest != expected_manifest:
            raise PluginHostError(
                self._spec.error_code("manifest_mismatch"),
                f"{self._spec.display_name} provider manifest did not come from "
                "the ready DSH registration",
            )
        if profile.agent_provider.ref != self._spec.provider_ref:
            raise PluginHostError(
                self._spec.error_code("profile_mismatch"),
                "composition profile does not select the shipped "
                f"{self._spec.display_name} DSH provider",
            )
        current = await self._host.registration()
        require_same_registration(self._spec, current, self._registration)
        execution_runtime = await self._execution_factory.stage(
            manifest, profile=profile, services=services
        )
        self.runtime = self.runtime_class(
            spec=self._spec,
            host=self._host,
            registration=self._registration,
            execution_runtime=execution_runtime,
            owns_host=self._owns_host,
        )
        return self.runtime


def validate_registration(
    spec: ShippedDshProviderSpec,
    registration: DshAgentProviderRegistration,
) -> None:
    descriptor = registration.descriptor
    preflight = registration.preflight
    if (
        descriptor.provider_id != spec.provider_id
        or descriptor.provider_version != spec.version
        or descriptor.plugin_name != spec.package_name
        or not preflight.ready
        or preflight.profile_digest != descriptor.profile_digest
        or preflight.descriptor_digest != descriptor.descriptor_digest
    ):
        raise PluginHostError(
            spec.error_code("registration_invalid"),
            f"{spec.display_name} DSH registration does not match the shipped provider fences",
        )


def require_same_registration(
    spec: ShippedDshProviderSpec,
    current: DshAgentProviderRegistration,
    expected: DshAgentProviderRegistration,
) -> None:
    validate_registration(spec, current)
    if (
        current.descriptor != expected.descriptor
        or current.preflight != expected.preflight
        or current.manifest != expected.manifest
    ):
        raise PluginHostError(
            spec.error_code("registration_changed"),
            f"{spec.display_name} DSH provider changed after admission",
        )


__all__ = [
    "CODEX_DSH_SPEC",
    "HARNESS_DSH_SPEC",
    "SHIPPED_DSH_PROVIDER_SPECS",
    "ShippedDshBridgeFactory",
    "ShippedDshBridgeRuntime",
    "ShippedDshBundle",
    "ShippedDshProviderSpec",
    "load_shipped_dsh_bundle",
    "require_same_registration",
    "shipped_dsh_host_command",
    "validate_registration",
]
