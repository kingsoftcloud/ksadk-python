"""Shared shipped-DSH host and bridge coverage for every official provider."""

from __future__ import annotations

from pathlib import Path

import pytest

from ksadk.plugins.bridges.dsh import DshProfileProjection
from ksadk.plugins.contracts import CompositionProfile
from ksadk.plugins.providers.codex_dsh import (
    SHIPPED_CODEX_DSH_PACKAGE,
    SHIPPED_CODEX_PROVIDER_ID,
    SHIPPED_CODEX_PROVIDER_VERSION,
    KsADKCodexDshBridgeFactory,
    KsADKCodexDshBridgeRuntime,
    shipped_codex_dsh_bundle,
    shipped_codex_dsh_host_command,
)
from ksadk.plugins.providers.dsh import DshAgentProviderHost
from ksadk.plugins.providers.harness_dsh import (
    SHIPPED_HARNESS_DSH_PACKAGE,
    SHIPPED_HARNESS_PROVIDER_ID,
    SHIPPED_HARNESS_PROVIDER_VERSION,
    shipped_harness_dsh_bundle,
    shipped_harness_dsh_host_command,
)


def _projection(package: str, *, digest: str) -> DshProfileProjection:
    return DshProfileProjection(
        profile="ksadk",
        bundles=("@deepseek-ai/dsh-base", package),
        config_digest="sha256:" + digest * 64,
        config_bytes=128,
        host_version="0.1.1-rc.2",
    )


def test_official_providers_share_one_fixed_descriptor_host() -> None:
    codex = shipped_codex_dsh_host_command()
    harness = shipped_harness_dsh_host_command()

    assert codex[:-1] == harness[:-1]
    assert codex[-2] == "ksadk.plugins.providers.dsh_descriptor_host"
    assert codex[1] == "-B"
    assert codex[-1] == "codex"
    assert harness[-1] == "harness"
    assert shipped_codex_dsh_bundle().package_name == SHIPPED_CODEX_DSH_PACKAGE
    assert shipped_harness_dsh_bundle().package_name == SHIPPED_HARNESS_DSH_PACKAGE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("package", "provider_id", "version", "command", "digest"),
    [
        (
            SHIPPED_CODEX_DSH_PACKAGE,
            SHIPPED_CODEX_PROVIDER_ID,
            SHIPPED_CODEX_PROVIDER_VERSION,
            shipped_codex_dsh_host_command,
            "c",
        ),
        (
            SHIPPED_HARNESS_DSH_PACKAGE,
            SHIPPED_HARNESS_PROVIDER_ID,
            SHIPPED_HARNESS_PROVIDER_VERSION,
            shipped_harness_dsh_host_command,
            "e",
        ),
    ],
)
async def test_shared_descriptor_host_preserves_provider_identity(
    package: str,
    provider_id: str,
    version: str,
    command,
    digest: str,
) -> None:
    host = DshAgentProviderHost(
        command(),
        projection=_projection(package, digest=digest),
        cwd=Path(__file__).parents[2],
    )
    try:
        registration = await host.registration()
        assert registration.descriptor.plugin_name == package
        assert registration.descriptor.provider_id == provider_id
        assert registration.descriptor.provider_version == version
        assert registration.preflight.ready is True
    finally:
        if host.pid is not None:
            await host.dispose()


class _ExecutionRuntime:
    def __init__(self) -> None:
        self.ready = False

    async def start(self) -> None:
        self.ready = True

    async def health(self) -> bool:
        return self.ready

    async def prepare(self, bundle, *, capabilities):  # pragma: no cover - stage only
        return bundle, capabilities

    async def drain(self) -> None:
        self.ready = False

    async def dispose(self) -> None:
        self.ready = False


class _ExecutionFactory:
    def __init__(self) -> None:
        self.runtime = _ExecutionRuntime()

    async def stage(self, manifest, *, profile, services):
        del manifest, profile, services
        return self.runtime


@pytest.mark.asyncio
async def test_codex_named_factory_uses_shared_registration_bridge() -> None:
    host = DshAgentProviderHost(
        shipped_codex_dsh_host_command(),
        projection=_projection(SHIPPED_CODEX_DSH_PACKAGE, digest="d"),
        cwd=Path(__file__).parents[2],
    )
    runtime: KsADKCodexDshBridgeRuntime | None = None
    try:
        registration = await host.registration()
        profile = CompositionProfile.model_validate(
            {
                "agentProvider": {
                    "ref": (
                        f"plugin://{SHIPPED_CODEX_PROVIDER_ID}" f"@{SHIPPED_CODEX_PROVIDER_VERSION}"
                    )
                }
            }
        )
        factory = KsADKCodexDshBridgeFactory(
            host,
            registration,
            execution_factory=_ExecutionFactory(),  # type: ignore[arg-type]
        )
        runtime = await factory.stage(
            registration.manifest,
            profile=profile,
            services={},
        )
        assert isinstance(runtime, KsADKCodexDshBridgeRuntime)
        await runtime.start()
        assert await runtime.health() is True
    finally:
        if runtime is not None:
            await runtime.dispose()
        elif host.pid is not None:
            await host.dispose()
