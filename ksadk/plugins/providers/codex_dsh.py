"""Official Codex AgentProvider packaged as a standard DSH Bundle.

DSH owns installation and discovery. The shared DSH bridge validates that
registration, then delegates execution to the existing Codex RuntimeAdapter;
this module contains no second Codex loop.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ksadk.plugins.contracts import CompositionProfile, PluginManifest
from ksadk.plugins.providers.codex import CodexAgentProviderFactory
from ksadk.plugins.providers.dsh import DshAgentProviderHost, DshAgentProviderRegistration
from ksadk.plugins.providers.shipped_dsh import (
    CODEX_DSH_SPEC,
    ShippedDshBridgeFactory,
    ShippedDshBridgeRuntime,
    ShippedDshBundle,
    load_shipped_dsh_bundle,
    shipped_dsh_host_command,
)

SHIPPED_CODEX_DSH_PACKAGE = CODEX_DSH_SPEC.package_name
SHIPPED_CODEX_PROVIDER_ID = CODEX_DSH_SPEC.provider_id
SHIPPED_CODEX_PROVIDER_VERSION = CODEX_DSH_SPEC.version
ShippedCodexDshBundle = ShippedDshBundle


def shipped_codex_dsh_bundle() -> ShippedCodexDshBundle:
    return load_shipped_dsh_bundle(CODEX_DSH_SPEC)


def shipped_codex_dsh_host_command(*, python_executable: str | None = None) -> tuple[str, ...]:
    return shipped_dsh_host_command(CODEX_DSH_SPEC, python_executable=python_executable)


class KsADKCodexDshBridgeRuntime(ShippedDshBridgeRuntime):
    """Named Codex bridge type retained for inventory and public typing."""


class KsADKCodexDshBridgeFactory(ShippedDshBridgeFactory):
    runtime_class = KsADKCodexDshBridgeRuntime

    def __init__(
        self,
        host: DshAgentProviderHost,
        registration: DshAgentProviderRegistration,
        *,
        execution_factory: CodexAgentProviderFactory | None = None,
        owns_host: bool = True,
    ) -> None:
        super().__init__(
            spec=CODEX_DSH_SPEC,
            host=host,
            registration=registration,
            execution_factory=execution_factory or CodexAgentProviderFactory(),
            owns_host=owns_host,
        )

    async def stage(
        self,
        manifest: PluginManifest,
        *,
        profile: CompositionProfile,
        services: Mapping[str, Any],
    ) -> KsADKCodexDshBridgeRuntime:
        runtime = await super().stage(manifest, profile=profile, services=services)
        assert isinstance(runtime, KsADKCodexDshBridgeRuntime)
        return runtime


__all__ = [
    "KsADKCodexDshBridgeFactory",
    "KsADKCodexDshBridgeRuntime",
    "SHIPPED_CODEX_DSH_PACKAGE",
    "SHIPPED_CODEX_PROVIDER_ID",
    "SHIPPED_CODEX_PROVIDER_VERSION",
    "ShippedCodexDshBundle",
    "shipped_codex_dsh_bundle",
    "shipped_codex_dsh_host_command",
]
