"""Shipped DSH Harness provider -> registration -> legacy adapter bridge."""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType

import pytest

from ksadk.harness.reasoner import HarnessReasoningTurn
from ksadk.plugins.bridges.dsh import DshProfileProjection
from ksadk.plugins.bundle import ResolvedPluginBundle
from ksadk.plugins.contracts import CompositionProfile
from ksadk.plugins.host import PluginHost, PluginHostError
from ksadk.plugins.providers.dsh import DSH_HOST_USER_PERMISSION, DshAgentProviderHost
from ksadk.plugins.providers.harness import KsADKHarnessProviderFactory
from ksadk.plugins.providers.harness_dsh import (
    SHIPPED_HARNESS_DSH_PACKAGE,
    SHIPPED_HARNESS_PROVIDER_ID,
    SHIPPED_HARNESS_PROVIDER_VERSION,
    KsADKHarnessDshBridgeFactory,
    shipped_harness_dsh_bundle,
    shipped_harness_dsh_host_command,
)
from ksadk.plugins.providers.legacy_catalog import (
    KSADK_HARNESS_AGENT_PROVIDER_PLUGIN_ID,
    builtin_agent_provider_manifests,
    legacy_harness_agent_provider_manifest,
)
from ksadk.plugins.resolver import PluginRegistry
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.studio.contracts import BundleManifest


class _Reasoner:
    async def complete(self, *, model, prompt, messages, tools):  # noqa: ANN001
        del prompt, tools
        assert model == "fixture-model"
        latest = next(
            str(item.get("content") or "")
            for item in reversed(messages)
            if item.get("role") == "user"
        )
        return HarnessReasoningTurn(final_text=f"DSH Harness: {latest}")


def _projection(*, digest: str = "a") -> DshProfileProjection:
    return DshProfileProjection(
        profile="ksadk",
        bundles=("@deepseek-ai/dsh-base", SHIPPED_HARNESS_DSH_PACKAGE),
        config_digest="sha256:" + digest * 64,
        config_bytes=128,
        host_version="0.1.1-rc.2",
    )


def _bundle(
    tmp_path: Path,
    registry: PluginRegistry,
    profile: CompositionProfile,
) -> ResolvedPluginBundle:
    composition = registry.resolve(profile)
    return ResolvedPluginBundle(
        root=tmp_path,
        manifest=BundleManifest(
            bundle_format="agentkit.bundle/v2",
            agent_id="shipped-harness-agent",
            source_revision=1,
            resolved_digest="sha256:" + "1" * 64,
            runtime_type="harness",
            plugin_lock_digest=composition.plugin_lock_digest,
            composition_profile_digest=composition.profile_digest,
            files=[],
            bundle_digest="sha256:" + "2" * 64,
        ),
        resolved_agent_spec=MappingProxyType(
            {
                "model": MappingProxyType({"model": "fixture-model"}),
                "instructions": MappingProxyType(
                    {"system": "Use the shipped DSH Harness provider."}
                ),
                "execution": MappingProxyType({"strategy": "direct"}),
            }
        ),
        composition=composition,
    )


def test_shipped_harness_is_a_standard_dsh_bundle_in_wheel_package_data() -> None:
    bundle = shipped_harness_dsh_bundle()
    package = json.loads((bundle.root / "package.json").read_text(encoding="utf-8"))

    assert bundle.package_name == SHIPPED_HARNESS_DSH_PACKAGE
    assert bundle.version == SHIPPED_HARNESS_PROVIDER_VERSION
    assert package["dsh"] == {"bundle": {"patch": "./cordis.patch.yml"}}
    assert "ksadk" not in package
    assert not (bundle.root / "ksadk-plugin.json").exists()
    assert "ctx.provide('ksadkHarnessAgentProvider'" in (bundle.root / "index.mjs").read_text(
        encoding="utf-8"
    )

    pyproject = (Path(__file__).parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    assert '"plugins/providers/bundles/**/*"' in pyproject


def test_harness_is_not_a_core_builtin_and_no_provider_bypasses_dsh() -> None:
    assert builtin_agent_provider_manifests() == ()

    legacy = legacy_harness_agent_provider_manifest()
    assert legacy.metadata.id == KSADK_HARNESS_AGENT_PROVIDER_PLUGIN_ID
    assert legacy.spec.provenance.source == "builtin"
    assert legacy.spec.isolation == "in-process"


@pytest.mark.asyncio
async def test_shipped_dsh_registration_gates_legacy_harness_execution(
    tmp_path: Path,
) -> None:
    host = DshAgentProviderHost(
        shipped_harness_dsh_host_command(),
        projection=_projection(),
        cwd=Path(__file__).parents[2],
    )
    plugin_host: PluginHost | None = None
    try:
        registration = await host.registration()
        assert registration.descriptor.plugin_name == SHIPPED_HARNESS_DSH_PACKAGE
        assert registration.descriptor.provider_id == SHIPPED_HARNESS_PROVIDER_ID
        assert registration.descriptor.profile_digest == _projection().config_digest
        assert registration.manifest.spec.provenance.source == "runtime-native"
        assert (await host.inventory()).activation_count == 0

        registry = PluginRegistry([registration.manifest])
        profile = CompositionProfile.model_validate(
            {
                "agentProvider": {
                    "ref": (
                        f"plugin://{SHIPPED_HARNESS_PROVIDER_ID}"
                        f"@{SHIPPED_HARNESS_PROVIDER_VERSION}"
                    )
                }
            }
        )
        session_service = InMemorySessionService()
        factory = KsADKHarnessDshBridgeFactory(
            host,
            registration,
            execution_factory=KsADKHarnessProviderFactory(
                session_service=session_service,
                reasoner=_Reasoner(),
            ),
        )
        plugin_host = PluginHost(
            registry,
            {SHIPPED_HARNESS_PROVIDER_ID: factory},
            allowed_permissions=frozenset({DSH_HOST_USER_PERMISSION}),
        )
        inventory = await plugin_host.apply(profile)
        assert inventory.plugins[0].id == SHIPPED_HARNESS_PROVIDER_ID

        result = await plugin_host.execute(
            _bundle(tmp_path, registry, profile),
            {
                "user_id": "user-1",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        assert result.output_text == "DSH Harness: hello"
        assert result.inventory.provider == SHIPPED_HARNESS_PROVIDER_ID
        assert len(await session_service.list_sessions("shipped-harness-agent", "user-1")) == 1

        await plugin_host.dispose()
        plugin_host = None
        assert host.pid is None
    finally:
        if plugin_host is not None:
            await plugin_host.dispose()
        elif host.pid is not None:
            await host.dispose()


@pytest.mark.asyncio
async def test_inactive_bundle_and_changed_registration_never_fall_back_to_builtin(
    tmp_path: Path,
) -> None:
    inactive = DshProfileProjection(
        profile="ksadk",
        bundles=("@deepseek-ai/dsh-base",),
        config_digest="sha256:" + "b" * 64,
        config_bytes=64,
        host_version="0.1.1-rc.2",
    )
    host = DshAgentProviderHost(
        shipped_harness_dsh_host_command(),
        projection=inactive,
        cwd=Path(__file__).parents[2],
    )
    with pytest.raises(PluginHostError) as unavailable:
        await host.registration()
    assert unavailable.value.code == "dsh_provider_remote_error"
    assert host.pid is None

    admitted_host = DshAgentProviderHost(
        shipped_harness_dsh_host_command(),
        projection=_projection(digest="c"),
        cwd=Path(__file__).parents[2],
    )
    replacement_host = DshAgentProviderHost(
        shipped_harness_dsh_host_command(),
        projection=_projection(digest="d"),
        cwd=Path(__file__).parents[2],
    )
    try:
        admitted = await admitted_host.registration()
        replacement = await replacement_host.registration()
        profile = CompositionProfile.model_validate(
            {
                "agentProvider": {
                    "ref": (
                        f"plugin://{SHIPPED_HARNESS_PROVIDER_ID}"
                        f"@{SHIPPED_HARNESS_PROVIDER_VERSION}"
                    )
                }
            }
        )
        factory = KsADKHarnessDshBridgeFactory(replacement_host, admitted)
        with pytest.raises(PluginHostError) as changed:
            await factory.stage(
                admitted.manifest,
                profile=profile,
                services={
                    "session_service": InMemorySessionService(),
                    "harness_reasoner": _Reasoner(),
                },
            )
        assert changed.value.code == "harness_dsh_registration_changed"
        assert admitted.descriptor.profile_digest != replacement.descriptor.profile_digest
    finally:
        if admitted_host.pid is not None:
            await admitted_host.dispose()
        if replacement_host.pid is not None:
            await replacement_host.dispose()
