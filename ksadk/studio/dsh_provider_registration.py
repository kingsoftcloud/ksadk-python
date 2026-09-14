"""Managed DSH AgentProvider registration for the normal Studio lifecycle.

Studio treats the selected DSH Profile as the installed/enabled source of
truth, then starts each AgentProvider contribution in its own fixed-command
sidecar. Only descriptor-fenced, ready registrations enter Studio's selector;
ordinary client-only DSH bundles remain visible to plugin management without
being mistaken for AgentProviders.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from ksadk.plugins.bridges.dsh import (
    DshPluginInventory,
    DshProfilePluginBridge,
    DshProfileProjection,
)
from ksadk.plugins.contracts import CompositionProfile, PluginManifest
from ksadk.plugins.dsh_home import default_studio_dsh_home, prepare_studio_dsh_home, studio_dsh_home
from ksadk.plugins.dsh_toolchain import DSH_CORE_PACKAGES, DshToolchainManager
from ksadk.plugins.host import ManagedPlugin, PluginHostError
from ksadk.plugins.providers.codex_dsh import (
    SHIPPED_CODEX_DSH_PACKAGE,
    SHIPPED_CODEX_PROVIDER_VERSION,
    KsADKCodexDshBridgeFactory,
    shipped_codex_dsh_bundle,
    shipped_codex_dsh_host_command,
)
from ksadk.plugins.providers.dsh import (
    DshAgentProviderFactory,
    DshAgentProviderHost,
    DshAgentProviderRegistration,
)
from ksadk.plugins.providers.harness_dsh import (
    SHIPPED_HARNESS_DSH_PACKAGE,
    SHIPPED_HARNESS_PROVIDER_VERSION,
    KsADKHarnessDshBridgeFactory,
    shipped_harness_dsh_bundle,
    shipped_harness_dsh_host_command,
)

_PROFILE_FILES = ("package.json", "cordis.patch.yml", "index.mjs")
_MAX_PACKAGE_JSON_BYTES = 2 * 1024 * 1024
_DSH_PLATFORM_BUNDLES = frozenset(DSH_CORE_PACKAGES)
_SHIPPED_PROVIDER_PACKAGES = frozenset({SHIPPED_CODEX_DSH_PACKAGE, SHIPPED_HARNESS_DSH_PACKAGE})
_SHIPPED_RESOURCE_PACKAGES = (
    ("@kingsoftcloud/dsh-platform-resources", "dsh-platform-resources"),
    ("@kingsoftcloud/dsh-knowledge", "dsh-knowledge"),
    ("@kingsoftcloud/dsh-memory", "dsh-memory"),
    ("@kingsoftcloud/dsh-skill-center", "dsh-skill-center"),
)
_SHIPPED_RESOURCE_VERSION = "0.1.0"
_RESOURCE_RUNTIME_BUNDLE = "@deepseek-ai/dsh-web-app"


class StudioDshProviderRegistrationError(RuntimeError):
    """A selected DSH Profile could not produce trustworthy registrations."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class StudioDshProviderStatus(BaseModel):
    """Lifecycle evidence for one installed DSH Profile package."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    package_name: str
    package_version: str
    display_name: str = ""
    state: Literal["installed", "enabled", "ready", "bound", "failed", "disposed"]
    provider_ref: str | None = None
    error_code: str | None = None


class StudioDshProviderInventory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["stopped", "starting", "ready", "bound", "failed", "disposed"]
    profile: str
    profile_digest: str | None = None
    providers: tuple[str, ...] = ()
    packages: tuple[StudioDshProviderStatus, ...] = ()
    error_code: str | None = None


@dataclass(frozen=True)
class StudioDshProviderRegistrations:
    manifests: Mapping[str, PluginManifest]
    factories: Mapping[str, Any]
    inventory: StudioDshProviderInventory


@dataclass(frozen=True)
class _ProfileSnapshot:
    projection: DshProfileProjection
    packages: tuple[DshPluginInventory, ...]


BridgeFactory = Callable[..., DshProfilePluginBridge]
HostFactory = Callable[..., DshAgentProviderHost]


class _FreshDshAgentProviderFactory:
    """Give every PluginHost graph an independently owned provider process."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        projection: DshProfileProjection,
        cwd: Path,
        environment: Mapping[str, str],
        registration: DshAgentProviderRegistration,
        host_factory: HostFactory,
    ) -> None:
        self._command = tuple(command)
        self._projection = projection
        self._cwd = cwd
        self._environment = dict(environment)
        self._registration = registration
        self._host_factory = host_factory

    async def stage(
        self,
        manifest: PluginManifest,
        *,
        profile: CompositionProfile,
        services: Mapping[str, Any],
    ) -> ManagedPlugin:
        host = self._host_factory(
            self._command,
            projection=self._projection,
            cwd=self._cwd,
            environment=self._environment,
        )
        try:
            current = await host.registration()
            if current != self._registration:
                raise PluginHostError(
                    "dsh_provider_registration_changed",
                    "DSH provider registration changed after Studio discovery",
                )
            return await DshAgentProviderFactory(host, current).stage(
                manifest,
                profile=profile,
                services=services,
            )
        except BaseException:
            await host.dispose()
            raise


class _FreshShippedDshBridgeFactory:
    """Bind a shipped provider to a host owned by the consuming PluginHost loop."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        projection: DshProfileProjection,
        cwd: Path,
        environment: Mapping[str, str],
        registration: DshAgentProviderRegistration,
        package_name: str,
        host_factory: HostFactory,
    ) -> None:
        self._command = tuple(command)
        self._projection = projection
        self._cwd = cwd
        self._environment = dict(environment)
        self._registration = registration
        self._package_name = package_name
        self._host_factory = host_factory

    async def stage(
        self,
        manifest: PluginManifest,
        *,
        profile: CompositionProfile,
        services: Mapping[str, Any],
    ) -> ManagedPlugin:
        host = self._host_factory(
            self._command,
            projection=self._projection,
            cwd=self._cwd,
            environment=self._environment,
        )
        try:
            current = await host.registration()
            if current != self._registration:
                raise PluginHostError(
                    "dsh_provider_registration_changed",
                    "DSH provider registration changed after Studio discovery",
                )
            if self._package_name == SHIPPED_CODEX_DSH_PACKAGE:
                factory: Any = KsADKCodexDshBridgeFactory(host, current, owns_host=True)
            elif self._package_name == SHIPPED_HARNESS_DSH_PACKAGE:
                factory = KsADKHarnessDshBridgeFactory(host, current, owns_host=True)
            else:  # guarded by _register_package
                raise PluginHostError(
                    "dsh_provider_package_unsupported",
                    "shipped DSH provider package is unsupported",
                )
            return await factory.stage(manifest, profile=profile, services=services)
        except BaseException:
            await host.dispose()
            raise


class StudioDshProviderRegistrationManager:
    """Own Profile discovery, provider preflight, registration, and disposal."""

    def __init__(
        self,
        workspace: Path,
        *,
        dsh_home: Path,
        profile: str = "web",
        dsh_command: Sequence[str] | None = None,
        node_command: Sequence[str] | None = None,
        cordis_module: Path | None = None,
        bridge_factory: BridgeFactory = DshProfilePluginBridge,
        host_factory: HostFactory = DshAgentProviderHost,
    ) -> None:
        self._workspace = workspace.resolve()
        self._dsh_home = dsh_home.expanduser().resolve()
        self._profile = profile
        self._dsh_command = tuple(dsh_command) if dsh_command is not None else None
        self._node_command = tuple(node_command) if node_command is not None else None
        self._cordis_module = cordis_module.resolve() if cordis_module is not None else None
        self._bridge_factory = bridge_factory
        self._host_factory = host_factory
        self._lock = asyncio.Lock()
        self._hosts: dict[str, DshAgentProviderHost] = {}
        self._registrations: StudioDshProviderRegistrations | None = None
        self._inventory = StudioDshProviderInventory(state="stopped", profile=profile)

    @classmethod
    def discover(cls, workspace: Path) -> "StudioDshProviderRegistrationManager | None":
        """Discover an initialized managed Profile without downloading tools."""

        home = studio_dsh_home(workspace)
        profile = os.environ.get("KSADK_DSH_PROFILE", "").strip() or "web"
        configured_bin = os.environ.get("KSADK_DSH_BIN", "").strip()
        manifest = home / "profiles" / profile / "package.json"
        if not manifest.is_file():
            return None
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        dsh = payload.get("dsh") if isinstance(payload, dict) else None
        profile_payload = dsh.get("profile") if isinstance(dsh, dict) else None
        bundles = profile_payload.get("bundles") if isinstance(profile_payload, dict) else None
        if not isinstance(bundles, list) or any(not isinstance(item, str) for item in bundles):
            return None
        command = (str(Path(configured_bin).expanduser()),) if configured_bin else None
        return cls(workspace, dsh_home=home, profile=profile, dsh_command=command)

    @classmethod
    def discover_or_create_workspace_default(
        cls, workspace: Path
    ) -> "StudioDshProviderRegistrationManager | None":
        """Discover DSH or prepare the isolated first-run Studio Profile.

        The default is deliberately narrower than :meth:`discover`: only the
        Studio-owned versioned home and official ``web`` Profile qualify for
        automatic official-provider bootstrap.  Explicit DSH homes/profiles
        are user-owned and are never mutated by Studio startup.
        """

        configured_home = os.environ.get("KSADK_DSH_HOME", "").strip()
        configured_profile = os.environ.get("KSADK_DSH_PROFILE", "").strip()
        if configured_home or configured_profile:
            return cls.discover(workspace)
        root = workspace.resolve()
        home = default_studio_dsh_home(root)
        configured_bin = os.environ.get("KSADK_DSH_BIN", "").strip()
        if configured_bin:
            command: Sequence[str] | None = (str(Path(configured_bin).expanduser()),)
        else:
            try:
                command = DshToolchainManager().require_command()
            except Exception:
                # DSH is optional.  A missing toolchain must not make the
                # normal Studio/legacy Codex path unavailable.
                return None
        return cls(root, dsh_home=home, profile="web", dsh_command=command)

    @classmethod
    def create_workspace_resource_default(
        cls, workspace: Path
    ) -> "StudioDshProviderRegistrationManager | None":
        """Own the official-only Profile frozen into resource Builds."""

        if os.environ.get("KSADK_DSH_HOME", "").strip() or os.environ.get(
            "KSADK_DSH_PROFILE", ""
        ).strip():
            return None
        root = workspace.resolve()
        configured_bin = os.environ.get("KSADK_DSH_BIN", "").strip()
        if configured_bin:
            command: Sequence[str] | None = (str(Path(configured_bin).expanduser()),)
        else:
            try:
                command = DshToolchainManager().require_command()
            except Exception:
                return None
        return cls(
            root,
            dsh_home=default_studio_dsh_home(root),
            profile="agentkit-resources",
            dsh_command=command,
        )

    @property
    def inventory(self) -> StudioDshProviderInventory:
        return self._inventory

    @property
    def _owns_workspace_default_profile(self) -> bool:
        configured_home = os.environ.get("KSADK_DSH_HOME", "").strip()
        configured_profile = os.environ.get("KSADK_DSH_PROFILE", "").strip()
        expected_home = default_studio_dsh_home(self._workspace)
        return (
            not configured_home
            and not configured_profile
            and self._profile in {"web", "agentkit-resources"}
            and self._dsh_home == expected_home
        )

    async def bootstrap_official_codex_provider(
        self,
    ) -> Literal["installed", "already_enabled", "disabled", "skipped"]:
        """Apply the wheel-owned Codex default once for the Studio Profile.

        This method never enables an existing disabled package and never
        mutates an explicit/user-owned DSH Profile.  The marker also preserves
        an explicit uninstall across future Studio starts.
        """

        if not self._owns_workspace_default_profile:
            return "skipped"
        async with self._lock:
            return await asyncio.to_thread(self._bootstrap_official_codex_provider_sync)

    def _bootstrap_official_codex_provider_sync(
        self,
    ) -> Literal["installed", "already_enabled", "disabled", "skipped"]:
        """Perform the blocking DSH package mutation outside the event loop."""

        if not self._owns_workspace_default_profile:
            return "skipped"
        prepare_studio_dsh_home(self._dsh_home)
        marker = self._default_marker_path
        marker_payload = self._read_default_marker(marker)
        command = self._dsh_command or DshToolchainManager().require_command()
        shipped = shipped_codex_dsh_bundle()
        with self._bridge_factory(
            dsh_home=self._dsh_home,
            profile=self._profile,
            dsh_command=command,
            cwd=self._workspace,
        ) as bridge:
            self._repair_owned_profile_layout(bridge)
            installed = {item.name: item for item in bridge.list_plugins()}
            current = installed.get(SHIPPED_CODEX_DSH_PACKAGE)
            if current is None:
                # A stale marker only records that an older bootstrap ran; it
                # is not proof that the package is still present.  Profiles
                # can be migrated or edited by DSH, so restore the shipped
                # default whenever the official package has disappeared.
                current = bridge.install_plugin(str(shipped.root), accept_host_permissions=True)
                if current.name != SHIPPED_CODEX_DSH_PACKAGE:
                    raise StudioDshProviderRegistrationError(
                        "codex_dsh_package_mismatch",
                        "the official Codex install returned a different package",
                    )
                result: Literal["installed", "already_enabled", "disabled"] = "installed"
            else:
                result = "already_enabled" if current.enabled else "disabled"
            if current.version != SHIPPED_CODEX_PROVIDER_VERSION:
                raise StudioDshProviderRegistrationError(
                    "codex_dsh_bundle_not_active",
                    "the official Codex DSH Bundle version is not supported",
                )
            if not current.enabled and result == "installed":
                current = bridge.set_enabled(SHIPPED_CODEX_DSH_PACKAGE, enabled=True)
                if not current.enabled:
                    raise StudioDshProviderRegistrationError(
                        "codex_dsh_enable_failed",
                        "the official Codex DSH Bundle did not become enabled",
                    )
            self._verify_shipped_bundle_bytes(SHIPPED_CODEX_DSH_PACKAGE)
        if marker_payload.get("codexProviderApplied") is not True:
            self._write_default_marker(
                marker,
                {
                    **marker_payload,
                    "version": 1,
                    "codexProviderApplied": True,
                    "codexProviderVersion": SHIPPED_CODEX_PROVIDER_VERSION,
                },
            )
        return result

    async def bootstrap_official_harness_provider(self) -> Literal["installed", "already_enabled", "disabled", "skipped"]:
        """Install and preflight the wheel-owned Harness provider in the managed profile."""
        if not self._owns_workspace_default_profile:
            return "skipped"
        async with self._lock:
            return await asyncio.to_thread(self._bootstrap_official_harness_provider_sync)

    def _bootstrap_official_harness_provider_sync(self) -> Literal["installed", "already_enabled", "disabled", "skipped"]:
        if not self._owns_workspace_default_profile:
            return "skipped"
        command = self._dsh_command or DshToolchainManager().require_command()
        shipped = shipped_harness_dsh_bundle()
        with self._bridge_factory(dsh_home=self._dsh_home, profile=self._profile, dsh_command=command, cwd=self._workspace) as bridge:
            self._repair_owned_profile_layout(bridge)
            installed = {item.name: item for item in bridge.list_plugins()}
            current = installed.get(SHIPPED_HARNESS_DSH_PACKAGE)
            if current is None:
                current = bridge.install_plugin(str(shipped.root), accept_host_permissions=True)
                result = "installed"
            else:
                result = "already_enabled" if current.enabled else "disabled"
            if current.name != SHIPPED_HARNESS_DSH_PACKAGE or current.version != SHIPPED_HARNESS_PROVIDER_VERSION:
                raise StudioDshProviderRegistrationError("harness_dsh_bundle_not_active", "the official Harness DSH Bundle version is not supported")
            if result == "installed" and not current.enabled:
                current = bridge.set_enabled(SHIPPED_HARNESS_DSH_PACKAGE, enabled=True)
            if not current.enabled:
                return "disabled"
            self._verify_shipped_bundle_bytes(SHIPPED_HARNESS_DSH_PACKAGE)
        return result

    async def bootstrap_official_resource_plugins(
        self,
    ) -> Literal["installed", "already_enabled", "disabled", "skipped"]:
        """Apply the wheel-owned platform-resource stack once to Studio's Profile."""

        if not self._owns_workspace_default_profile:
            return "skipped"
        async with self._lock:
            return await asyncio.to_thread(self._bootstrap_official_resource_plugins_sync)

    def _bootstrap_official_resource_plugins_sync(
        self,
    ) -> Literal["installed", "already_enabled", "disabled", "skipped"]:
        if not self._owns_workspace_default_profile:
            return "skipped"
        prepare_studio_dsh_home(self._dsh_home)
        marker = self._default_marker_path
        marker_payload = self._read_default_marker(marker)
        command = self._dsh_command or DshToolchainManager().require_command()
        installed_any = False
        disabled = False
        enable_defaults = marker_payload.get("platformResourcesEnabled") is not True
        with self._bridge_factory(
            dsh_home=self._dsh_home,
            profile=self._profile,
            dsh_command=command,
            cwd=self._workspace,
        ) as bridge:
            self._repair_owned_profile_layout(bridge)
            inventory = {item.name: item for item in bridge.list_plugins()}
            for package_name, directory_name in _SHIPPED_RESOURCE_PACKAGES:
                current = inventory.get(package_name)
                if current is None:
                    # A completed marker plus a missing package records a later,
                    # explicit uninstall. Startup must preserve that choice.
                    if marker_payload.get("platformResourcesApplied") is True:
                        continue
                    root = (
                        Path(__file__).parents[1]
                        / "plugins"
                        / "providers"
                        / "bundles"
                        / directory_name
                    )
                    current = bridge.install_plugin(
                        str(root), accept_host_permissions=True
                    )
                    if current.name != package_name:
                        raise StudioDshProviderRegistrationError(
                            "resource_dsh_package_mismatch",
                            "an official resource install returned a different package",
                        )
                    installed_any = True
                if current.version != _SHIPPED_RESOURCE_VERSION:
                    raise StudioDshProviderRegistrationError(
                        "resource_dsh_bundle_not_active",
                        "an official platform resource Bundle version is not supported",
                    )
                if not current.enabled and enable_defaults:
                    current = bridge.set_enabled(package_name, enabled=True)
                    if not current.enabled:
                        raise StudioDshProviderRegistrationError(
                            "resource_dsh_enable_failed",
                            "an official platform resource Bundle did not become enabled",
                        )
                disabled = disabled or not current.enabled
                self._verify_shipped_resource_bundle_bytes(package_name, directory_name)
        if self._profile == "agentkit-resources":
            self._ensure_resource_runtime_bundle()
        if marker_payload.get("platformResourcesApplied") is not True:
            self._write_default_marker(
                marker,
                {
                    **marker_payload,
                    "version": 1,
                    "platformResourcesApplied": True,
                    "platformResourcesEnabled": True,
                    "platformResourcesVersion": _SHIPPED_RESOURCE_VERSION,
                },
            )
        elif enable_defaults:
            self._write_default_marker(
                marker,
                {**marker_payload, "platformResourcesEnabled": True},
            )
        if disabled:
            return "disabled"
        return "installed" if installed_any else "already_enabled"

    def _ensure_resource_runtime_bundle(self) -> None:
        """Give the private resource Profile the official loopback transport.

        The capability exporter uses DSH's authenticated ``webServer`` and
        ``connection`` services even when no browser UI is mounted. Custom
        Profiles start with only ``dsh-base``, so the Studio-owned execution
        Profile must explicitly include DSH's official web runtime layer.
        """

        manifest_path = self._profile_root / "package.json"
        try:
            if manifest_path.stat().st_size > _MAX_PACKAGE_JSON_BYTES:
                raise ValueError("package.json is too large")
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            dsh = payload.get("dsh")
            profile = dsh.get("profile") if isinstance(dsh, dict) else None
            bundles = profile.get("bundles") if isinstance(profile, dict) else None
            if not isinstance(bundles, list) or any(
                not isinstance(item, str) for item in bundles
            ):
                raise ValueError("bundle order is invalid")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise StudioDshProviderRegistrationError(
                "resource_dsh_profile_invalid",
                "the official resource execution Profile is invalid",
            ) from error
        if _RESOURCE_RUNTIME_BUNDLE in bundles:
            return
        try:
            base_index = bundles.index("@deepseek-ai/dsh-base")
        except ValueError as error:
            raise StudioDshProviderRegistrationError(
                "resource_dsh_profile_invalid",
                "the official resource execution Profile has no DSH base Bundle",
            ) from error
        bundles.insert(base_index + 1, _RESOURCE_RUNTIME_BUNDLE)
        self._write_default_marker(manifest_path, payload)

    def _repair_owned_profile_layout(self, bridge: DshProfilePluginBridge) -> None:
        """Repair only Studio's default legacy Profile before projecting it.

        Older Studio versions created the ``web`` Profile with pnpm's hoisted
        linker. Some community dependency graphs then contained links to the
        ambient pnpm store and could not form a closed Build snapshot. The
        frozen lock remains the authority; the bridge keeps the original tree
        as an atomic rollback point while reinstalling the isolated layout.
        """

        if not self._owns_workspace_default_profile:
            return
        settings_path = self._dsh_home / "profiles" / self._profile / "pnpm-workspace.yaml"
        if not settings_path.is_file():
            return
        try:
            settings = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as error:
            raise StudioDshProviderRegistrationError(
                "dsh_profile_layout_invalid",
                "the Studio-owned DSH Profile layout settings are unreadable",
            ) from error
        if not isinstance(settings, dict):
            raise StudioDshProviderRegistrationError(
                "dsh_profile_layout_invalid",
                "the Studio-owned DSH Profile layout settings are invalid",
            )
        if settings.get("nodeLinker") not in {"hoisted", "isolated"}:
            return
        bridge.migrate_to_isolated_layout(
            accept_host_permissions=True,
            recover_external_dependency_links=True,
        )

    @property
    def _default_marker_path(self) -> Path:
        # A bootstrap receipt belongs to one versioned DSH home and Profile.
        # A workspace-level receipt from an older home must not make a newly
        # isolated home look explicitly uninstalled before its first install.
        return self._dsh_home / f"official-dsh-defaults-{self._profile}.json"

    @staticmethod
    def _read_default_marker(path: Path) -> dict[str, object]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise StudioDshProviderRegistrationError(
                "dsh_default_marker_invalid",
                "the Studio official DSH defaults marker is unreadable",
            ) from error
        if not isinstance(payload, dict):
            raise StudioDshProviderRegistrationError(
                "dsh_default_marker_invalid",
                "the Studio official DSH defaults marker is invalid",
            )
        return payload

    @staticmethod
    def _write_default_marker(path: Path, payload: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    @property
    def host_pid(self) -> int | None:
        return next(iter(self.host_pids), None)

    @property
    def host_pids(self) -> tuple[int, ...]:
        return tuple(host.pid for host in self._hosts.values() if host.pid is not None)

    async def start(self) -> StudioDshProviderRegistrations:
        async with self._lock:
            if self._registrations is not None:
                return self._registrations
            if self._inventory.state == "disposed":
                raise StudioDshProviderRegistrationError(
                    "dsh_provider_manager_disposed",
                    "Studio DSH provider registration manager is disposed",
                )
            self._inventory = StudioDshProviderInventory(state="starting", profile=self._profile)
            try:
                snapshot = await asyncio.to_thread(self._discover_profile)
                result = await self._register_snapshot(snapshot)
            except BaseException as error:
                await self._dispose_hosts()
                code = str(getattr(error, "code", "dsh_provider_registration_failed"))
                self._inventory = StudioDshProviderInventory(
                    state="failed", profile=self._profile, error_code=code
                )
                if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                raise StudioDshProviderRegistrationError(
                    code,
                    "managed DSH Profile could not be projected safely",
                ) from error
            self._registrations = result
            self._inventory = result.inventory
            return result

    async def refresh(self) -> StudioDshProviderRegistrations:
        async with self._lock:
            if self._inventory.state == "disposed":
                raise StudioDshProviderRegistrationError(
                    "dsh_provider_manager_disposed",
                    "Studio DSH provider registration manager is disposed",
                )
            self._registrations = None
            await self._dispose_hosts()
            self._inventory = StudioDshProviderInventory(state="stopped", profile=self._profile)
        return await self.start()

    def mark_bound(self, provider_refs: Sequence[str]) -> None:
        if self._registrations is None or self._inventory.state != "ready":
            raise StudioDshProviderRegistrationError(
                "dsh_provider_registration_not_ready",
                "DSH provider registrations cannot be bound before preflight",
            )
        expected = tuple(self._registrations.manifests)
        if tuple(provider_refs) != expected:
            raise StudioDshProviderRegistrationError(
                "dsh_provider_registration_mismatch",
                "Studio did not bind the exact ready DSH provider registration set",
            )
        packages = tuple(
            item.model_copy(update={"state": "bound"})
            if item.state == "ready" and item.provider_ref in expected
            else item
            for item in self._inventory.packages
        )
        self._inventory = self._inventory.model_copy(
            update={"state": "bound", "packages": packages}
        )

    async def aclose(self) -> None:
        async with self._lock:
            self._registrations = None
            await self._dispose_hosts()
            packages = tuple(
                item.model_copy(update={"state": "disposed"})
                if item.state in {"ready", "bound"}
                else item
                for item in self._inventory.packages
            )
            self._inventory = StudioDshProviderInventory(
                state="disposed",
                profile=self._profile,
                profile_digest=self._inventory.profile_digest,
                packages=packages,
            )

    def _discover_profile(self) -> _ProfileSnapshot:
        prepare_studio_dsh_home(self._dsh_home)
        command = self._dsh_command or DshToolchainManager().require_command()
        with self._bridge_factory(
            dsh_home=self._dsh_home,
            profile=self._profile,
            dsh_command=command,
            cwd=self._workspace,
        ) as bridge:
            packages = bridge.list_plugins()
            projection = bridge.project_profile()
        enabled = {item.name for item in packages if item.enabled}
        projected = set(projection.bundles)
        if not enabled.issubset(projected) or projected - enabled - _DSH_PLATFORM_BUNDLES:
            raise StudioDshProviderRegistrationError(
                "dsh_provider_profile_inventory_mismatch",
                "DSH installed inventory and projected Profile disagree",
            )
        return _ProfileSnapshot(projection=projection, packages=packages)

    async def _register_snapshot(
        self, snapshot: _ProfileSnapshot
    ) -> StudioDshProviderRegistrations:
        manifests: dict[str, PluginManifest] = {}
        factories: dict[str, Any] = {}
        statuses = [
            StudioDshProviderStatus(
                package_name=item.name,
                package_version=item.version,
                display_name=item.display_name,
                state="enabled" if item.enabled else "installed",
            )
            for item in snapshot.packages
        ]
        provider_packages: dict[str, str] = {}
        for index, package in enumerate(snapshot.packages):
            if not package.enabled:
                continue
            host: DshAgentProviderHost | None = None
            try:
                registration, factory, host = await self._register_package(
                    package, snapshot.projection
                )
                if registration is None or factory is None or host is None:
                    continue
                descriptor = registration.descriptor
                provider_ref = f"plugin://{descriptor.provider_id}@{descriptor.provider_version}"
                prior_package = provider_packages.get(provider_ref)
                if prior_package is not None:
                    await host.dispose()
                    prior_host = self._hosts.pop(prior_package, None)
                    if prior_host is not None:
                        await prior_host.dispose()
                    manifests.pop(provider_ref, None)
                    factories.pop(provider_ref, None)
                    statuses[index] = statuses[index].model_copy(
                        update={
                            "state": "failed",
                            "provider_ref": provider_ref,
                            "error_code": "dsh_provider_registration_conflict",
                        }
                    )
                    for prior_index, status in enumerate(statuses):
                        if status.package_name == prior_package:
                            statuses[prior_index] = status.model_copy(
                                update={
                                    "state": "failed",
                                    "error_code": "dsh_provider_registration_conflict",
                                }
                            )
                            break
                    continue
                provider_packages[provider_ref] = package.name
                manifests[provider_ref] = registration.manifest
                factories[provider_ref] = factory
                # Discovery is a bounded admission probe. Keeping its process
                # alive would bind Studio construction to that event loop and
                # make later API or Scheduler loops reuse foreign transports.
                # The fresh factory repeats the exact registration fence and
                # owns its execution host in the consuming PluginHost loop.
                await host.dispose()
                statuses[index] = statuses[index].model_copy(
                    update={
                        "state": "ready",
                        "provider_ref": provider_ref,
                        "display_name": descriptor.display_name,
                    }
                )
            except BaseException as error:
                if host is not None:
                    try:
                        await host.dispose()
                    except BaseException:
                        pass
                if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                statuses[index] = statuses[index].model_copy(
                    update={
                        "state": "failed",
                        "error_code": str(
                            getattr(error, "code", "dsh_provider_registration_failed")
                        ),
                    }
                )
        inventory = StudioDshProviderInventory(
            state="ready",
            profile=self._profile,
            profile_digest=snapshot.projection.config_digest,
            providers=tuple(manifests),
            packages=tuple(statuses),
        )
        return StudioDshProviderRegistrations(
            manifests=manifests, factories=factories, inventory=inventory
        )

    async def _register_package(
        self,
        package: DshPluginInventory,
        projection: DshProfileProjection,
    ) -> tuple[
        DshAgentProviderRegistration | None,
        Any | None,
        DshAgentProviderHost | None,
    ]:
        if package.name in _SHIPPED_PROVIDER_PACKAGES:
            error_prefix = (
                "codex_dsh" if package.name == SHIPPED_CODEX_DSH_PACKAGE else "harness_dsh"
            )
            expected_version = (
                SHIPPED_CODEX_PROVIDER_VERSION
                if package.name == SHIPPED_CODEX_DSH_PACKAGE
                else SHIPPED_HARNESS_PROVIDER_VERSION
            )
            if package.version != expected_version:
                raise StudioDshProviderRegistrationError(
                    f"{error_prefix}_bundle_not_active",
                    "the exact shipped DSH AgentProvider Bundle is not active",
                )
            self._verify_shipped_bundle_bytes(package.name)
            command = (
                shipped_codex_dsh_host_command()
                if package.name == SHIPPED_CODEX_DSH_PACKAGE
                else shipped_harness_dsh_host_command()
            )
            cwd = self._workspace
            environment: dict[str, str] = {}
        else:
            entry = self._provider_host_entry(package.name)
            if entry is None:
                return None, None, None
            command = (*self._resolve_node_command(), str(entry))
            cwd = self._profile_root
            environment = {"KSADK_DSH_CORDIS_MODULE": str(self._resolve_cordis_module())}
        host = self._host_factory(
            command,
            projection=projection,
            cwd=cwd,
            environment=environment,
        )
        try:
            registration = await host.registration()
            if registration.descriptor.plugin_name != package.name:
                raise PluginHostError(
                    "dsh_provider_package_mismatch",
                    "DSH provider descriptor does not match its installed package",
                )
            provider_inventory = await host.inventory()
            if provider_inventory.state != "ready":
                raise PluginHostError(
                    "dsh_provider_inventory_not_ready",
                    "DSH AgentProvider inventory is not ready",
                )
            if package.name in _SHIPPED_PROVIDER_PACKAGES:
                factory: Any = _FreshShippedDshBridgeFactory(
                    command,
                    projection=projection,
                    cwd=cwd,
                    environment=environment,
                    registration=registration,
                    package_name=package.name,
                    host_factory=self._host_factory,
                )
            else:
                factory = _FreshDshAgentProviderFactory(
                    command,
                    projection=projection,
                    cwd=cwd,
                    environment=environment,
                    registration=registration,
                    host_factory=self._host_factory,
                )
            return registration, factory, host
        except BaseException:
            await host.dispose()
            raise

    @property
    def _profile_root(self) -> Path:
        return self._dsh_home / "profiles" / self._profile

    def _provider_host_entry(self, package_name: str) -> Path | None:
        package_root = self._profile_root / "node_modules"
        for segment in package_name.split("/"):
            package_root /= segment
        manifest_path = package_root / "package.json"
        try:
            if manifest_path.stat().st_size > _MAX_PACKAGE_JSON_BYTES:
                raise ValueError("package.json is too large")
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise PluginHostError(
                "dsh_provider_package_invalid",
                "installed DSH provider package.json is invalid",
            ) from error
        exports = payload.get("exports") if isinstance(payload, dict) else None
        if not isinstance(exports, dict) or "./provider-host" not in exports:
            return None
        relative = exports.get("./provider-host")
        if not isinstance(relative, str) or not relative.startswith("./"):
            raise PluginHostError(
                "dsh_provider_host_export_invalid",
                "DSH provider-host export must be one relative file",
            )
        try:
            resolved_root = package_root.resolve(strict=True)
            entry = (resolved_root / relative).resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise PluginHostError(
                "dsh_provider_host_unavailable",
                "DSH provider-host export is unavailable",
            ) from error
        if not entry.is_relative_to(resolved_root) or not entry.is_file():
            raise PluginHostError(
                "dsh_provider_host_export_invalid",
                "DSH provider-host export escapes its installed package",
            )
        return entry

    def _resolve_node_command(self) -> tuple[str, ...]:
        if self._node_command is not None:
            if not self._node_command:
                raise PluginHostError("dsh_provider_node_unavailable", "Node command is empty")
            return self._node_command
        configured = os.environ.get("NODE", "").strip() or "node"
        resolved = shutil.which(configured)
        if resolved is None:
            raise PluginHostError("dsh_provider_node_unavailable", "Node executable is unavailable")
        return (str(Path(resolved).resolve()),)

    def _resolve_cordis_module(self) -> Path:
        if self._cordis_module is not None:
            return self._cordis_module
        return DshToolchainManager().resolve_module_entry("@deepseek-ai/cordis")

    async def _dispose_hosts(self) -> None:
        hosts = tuple(self._hosts.values())
        self._hosts.clear()
        for host in hosts:
            try:
                await host.dispose()
            except BaseException:
                pass

    def _verify_shipped_bundle_bytes(self, package_name: str) -> None:
        if package_name == SHIPPED_CODEX_DSH_PACKAGE:
            shipped = shipped_codex_dsh_bundle().root
            error_prefix = "codex_dsh"
        elif package_name == SHIPPED_HARNESS_DSH_PACKAGE:
            shipped = shipped_harness_dsh_bundle().root
            error_prefix = "harness_dsh"
        else:  # pragma: no cover - callers use the closed official allowlist
            raise ValueError(f"unsupported shipped DSH package {package_name!r}")
        installed = self._profile_root / "node_modules"
        for segment in package_name.split("/"):
            installed /= segment
        try:
            matches = all(
                (installed / name).read_bytes().rstrip() == (shipped / name).read_bytes().rstrip()
                for name in _PROFILE_FILES
            )
        except OSError as error:
            raise StudioDshProviderRegistrationError(
                f"{error_prefix}_bundle_unreadable",
                "the installed DSH AgentProvider Bundle cannot be verified",
            ) from error
        if not matches:
            raise StudioDshProviderRegistrationError(
                f"{error_prefix}_bundle_digest_mismatch",
                "the installed DSH AgentProvider Bundle differs from the wheel-owned Bundle",
            )

    def _verify_shipped_resource_bundle_bytes(
        self, package_name: str, directory_name: str
    ) -> None:
        shipped = (
            Path(__file__).parents[1]
            / "plugins"
            / "providers"
            / "bundles"
            / directory_name
        )
        installed = self._profile_root / "node_modules"
        for segment in package_name.split("/"):
            installed /= segment
        names = ["package.json", "cordis.patch.yml", "index.mjs"]
        if directory_name == "dsh-platform-resources":
            names.append("client.mjs")
        try:
            package_matches = json.loads(
                (installed / "package.json").read_text(encoding="utf-8")
            ) == json.loads((shipped / "package.json").read_text(encoding="utf-8"))
            matches = package_matches and all(
                (installed / name).read_bytes().rstrip()
                == (shipped / name).read_bytes().rstrip()
                for name in names
                if name != "package.json"
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise StudioDshProviderRegistrationError(
                "resource_dsh_bundle_unreadable",
                "an installed platform resource Bundle cannot be verified",
            ) from error
        if not matches:
            raise StudioDshProviderRegistrationError(
                "resource_dsh_bundle_digest_mismatch",
                "an installed platform resource Bundle differs from the wheel-owned Bundle",
            )


def merge_provider_registrations(
    *registrations: StudioDshProviderRegistrations,
) -> tuple[dict[str, PluginManifest], dict[str, Any]]:
    """Merge registration sources without last-writer-wins ambiguity."""

    manifests: dict[str, PluginManifest] = {}
    factories: dict[str, Any] = {}
    for item in registrations:
        if item.manifests.keys() != item.factories.keys():
            raise StudioDshProviderRegistrationError(
                "dsh_provider_registration_partial",
                "provider manifests and factories must have identical exact references",
            )
        for provider_ref, manifest in item.manifests.items():
            if provider_ref in manifests and (
                manifests[provider_ref] != manifest
                or factories[provider_ref] is not item.factories[provider_ref]
            ):
                raise StudioDshProviderRegistrationError(
                    "dsh_provider_registration_conflict",
                    "multiple DSH registrations disagree for one exact provider reference",
                )
            manifests[provider_ref] = manifest
            factories[provider_ref] = item.factories[provider_ref]
    return manifests, factories


__all__ = [
    "StudioDshProviderInventory",
    "StudioDshProviderRegistrationError",
    "StudioDshProviderRegistrationManager",
    "StudioDshProviderRegistrations",
    "StudioDshProviderStatus",
    "merge_provider_registrations",
]
