"""KsADK Harness AgentProvider packaged as a standard DSH Bundle.

DSH owns installation and discovery. The shared bridge validates the exact
registration, then delegates execution to the optional in-process KsADK
Harness implementation so credentials and runtime services stay in KsADK.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ksadk.plugins.contracts import CompositionProfile, PluginManifest
from ksadk.plugins.providers.dsh import DshAgentProviderHost, DshAgentProviderRegistration
from ksadk.plugins.providers.shipped_dsh import (
    HARNESS_DSH_SPEC,
    ShippedDshBridgeFactory,
    ShippedDshBridgeRuntime,
    ShippedDshBundle,
    load_shipped_dsh_bundle,
    shipped_dsh_host_command,
)

if TYPE_CHECKING:
    from ksadk.plugins.providers.harness import KsADKHarnessProviderFactory

SHIPPED_HARNESS_DSH_PACKAGE = HARNESS_DSH_SPEC.package_name
SHIPPED_HARNESS_PROVIDER_ID = HARNESS_DSH_SPEC.provider_id
SHIPPED_HARNESS_PROVIDER_VERSION = HARNESS_DSH_SPEC.version
ShippedHarnessDshBundle = ShippedDshBundle


def shipped_harness_dsh_bundle() -> ShippedHarnessDshBundle:
    return load_shipped_dsh_bundle(HARNESS_DSH_SPEC)


def shipped_harness_dsh_host_command(*, python_executable: str | None = None) -> tuple[str, ...]:
    return shipped_dsh_host_command(HARNESS_DSH_SPEC, python_executable=python_executable)


class KsADKHarnessDshBridgeRuntime(ShippedDshBridgeRuntime):
    """Named Harness bridge type retained for inventory and public typing."""


class KsADKHarnessDshBridgeFactory(ShippedDshBridgeFactory):
    runtime_class = KsADKHarnessDshBridgeRuntime

    def __init__(
        self,
        host: DshAgentProviderHost,
        registration: DshAgentProviderRegistration,
        *,
        execution_factory: KsADKHarnessProviderFactory | None = None,
        owns_host: bool = True,
    ) -> None:
        if execution_factory is None:
            # Keep the Harness/ADK dependency optional until this provider is
            # selected; listing Codex or DSH plugins must stay cheap.
            from ksadk.plugins.providers.harness import KsADKHarnessProviderFactory

            execution_factory = KsADKHarnessProviderFactory()
        super().__init__(
            spec=HARNESS_DSH_SPEC,
            host=host,
            registration=registration,
            execution_factory=execution_factory,
            owns_host=owns_host,
        )

    async def stage(
        self,
        manifest: PluginManifest,
        *,
        profile: CompositionProfile,
        services: Mapping[str, Any],
    ) -> KsADKHarnessDshBridgeRuntime:
        runtime = await super().stage(manifest, profile=profile, services=services)
        assert isinstance(runtime, KsADKHarnessDshBridgeRuntime)
        return runtime


__all__ = [
    "KsADKHarnessDshBridgeFactory",
    "KsADKHarnessDshBridgeRuntime",
    "SHIPPED_HARNESS_DSH_PACKAGE",
    "SHIPPED_HARNESS_PROVIDER_ID",
    "SHIPPED_HARNESS_PROVIDER_VERSION",
    "ShippedHarnessDshBundle",
    "shipped_harness_dsh_bundle",
    "shipped_harness_dsh_host_command",
]
