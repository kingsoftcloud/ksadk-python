"""AgentRevision -> CompositionProfile -> PluginLock materialization tests."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from ksadk.plugins.composition import (
    CompositionCompileError,
    CompositionCompiler,
    CompositionPolicy,
    PluginCapabilitySelection,
    ResourcePluginMaterialization,
    RuntimePluginSelection,
)
from ksadk.plugins.contracts import PluginManifest
from ksadk.plugins.resolver import PluginRegistry
from ksadk.studio.contracts import (
    AgentDraft,
    AgentMetadata,
    AgentSpec,
    CapabilityBinding,
    ContextContributorsSpec,
    MemorySpec,
    ResourceDescriptor,
    RuntimeRef,
)


def _manifest(
    plugin_id: str,
    *,
    definition: str,
    slot: str,
    digit: str,
    mode: str = "unique",
) -> PluginManifest:
    return PluginManifest.model_validate(
        {
            "metadata": {"id": plugin_id, "version": "1.0.0"},
            "spec": {
                "domain": "ksadk-platform",
                "runtime": "python",
                "entrypoint": "tests.plugins.test_composition_compiler:factory",
                "provides": [
                    {"definition": definition, "slot": slot, "mode": mode}
                ],
                "isolation": "in-process",
                "compatibility": {"kernelApi": ">=1,<2"},
                "healthContract": "plugin.health/v1",
                "provenance": {
                    "source": "builtin",
                    "digest": f"sha256:{digit * 64}",
                },
            },
        }
    )


def _shared_state_manifest() -> PluginManifest:
    payload = _manifest(
        "io.ksadk.shared-state",
        definition="session.event-store/v1",
        slot="session.events",
        digit="d",
    ).model_dump(by_alias=True, mode="json")
    payload["spec"]["provides"].append(
        {
            "definition": "memory.provider/v1",
            "slot": "memory.primary",
            "mode": "unique",
        }
    )
    return PluginManifest.model_validate(payload)


def _selection(
    plugin_id: str,
    definition: str,
    *,
    slot: str | None = None,
) -> PluginCapabilitySelection:
    return PluginCapabilitySelection(
        ref=f"plugin://{plugin_id}@1.0.0",
        definition=definition,
        slot=slot,
    )


@dataclass
class _Catalog:
    resources: dict[str, ResourceDescriptor]

    def get(self, resource: str) -> ResourceDescriptor:
        return self.resources[resource]


def _resource(resource_id: str, kind: str, digit: str) -> ResourceDescriptor:
    return ResourceDescriptor.model_validate(
        {
            "resourceId": resource_id,
            "kind": kind,
            "name": resource_id.rsplit(":", 2)[-2],
            "displayName": resource_id,
            "version": "1.0.0",
            "digest": f"sha256:{digit * 64}",
            "status": "ready",
        }
    )


def _registry() -> PluginRegistry:
    manifests = [
        _manifest(
            "io.example.fixture-provider",
            definition="agent.provider/v1",
            slot="agent.execution",
            digit="1",
        ),
        _manifest(
            "io.ksadk.sqlite-session-store",
            definition="session.event-store/v1",
            slot="session.events",
            digit="4",
        ),
        _manifest(
            "io.ksadk.sqlite-memory",
            definition="memory.provider/v1",
            slot="memory.primary",
            digit="5",
        ),
        _manifest(
            "io.ksadk.memory-context",
            definition="context.contributor/v1",
            slot="context.memory",
            digit="6",
            mode="multiple",
        ),
        _manifest(
            "io.ksadk.core-renderer",
            definition="session.item.renderer/v1",
            slot="renderer.core",
            digit="7",
            mode="multiple",
        ),
        _manifest(
            "io.ksadk.workspace-mcp",
            definition="mcp.connector/v1",
            slot="capability.mcp.workspace",
            digit="8",
            mode="multiple",
        ),
        _manifest(
            "io.ksadk.workspace-skill",
            definition="skill.source/v1",
            slot="capability.skill.workspace",
            digit="9",
            mode="multiple",
        ),
        _manifest(
            "io.ksadk.native-provider",
            definition="agent.provider/v1",
            slot="agent.execution",
            digit="a",
        ),
        _shared_state_manifest(),
    ]
    return PluginRegistry(manifests)


def _policy(*, mcp_ref: str = "plugin://io.ksadk.workspace-mcp@1.0.0") -> CompositionPolicy:
    return CompositionPolicy(
        runtimes={
            "adk": RuntimePluginSelection(
                provider_ref="plugin://io.example.fixture-provider@1.0.0",
                supported_strategies=frozenset({"direct", "plan-act-observe"}),
            )
        },
        session_store=_selection(
            "io.ksadk.sqlite-session-store",
            "session.event-store/v1",
            slot="session.events",
        ),
        memory_providers={
            "local-default": _selection(
                "io.ksadk.sqlite-memory",
                "memory.provider/v1",
                slot="memory.primary",
            )
        },
        context_contributors={
            "memory_recall": _selection(
                "io.ksadk.memory-context",
                "context.contributor/v1",
                slot="context.memory",
            )
        },
        renderers=(
            _selection(
                "io.ksadk.core-renderer",
                "session.item.renderer/v1",
                slot="renderer.core",
            ),
        ),
        resource_materializations={
            "mcp:local:workspace-git:1.0.0": ResourcePluginMaterialization(
                kind="mcp",
                plugin_ref=mcp_ref,
            ),
            "skill:local:release-review:1.0.0": ResourcePluginMaterialization(
                kind="skill",
                plugin_ref="plugin://io.ksadk.workspace-skill@1.0.0",
                required=False,
            ),
        },
    )


def _draft() -> AgentDraft:
    spec = AgentSpec(
        runtime=RuntimeRef(
            type="adk",
            project_path="fixture",
            entry_point="agent.py",
            version="1.0.0",
        )
    )
    spec.execution.strategy = "plan-act-observe"
    spec.bindings.policy_template = "strict"
    spec.bindings.mcp_servers = [
        CapabilityBinding(
            resource_id="mcp:local:workspace-git:1.0.0",
            config={"namespace": "source"},
        )
    ]
    spec.bindings.skills = [
        CapabilityBinding(resource_id="skill:local:release-review:1.0.0")
    ]
    spec.memory = MemorySpec(enabled=True, provider_ref="local-default")
    spec.context.contributors = ContextContributorsSpec(memory_recall=True)
    return AgentDraft(
        metadata=AgentMetadata(id="phase-two-agent", name="Phase Two Agent", revision=7),
        spec=spec,
    )


def _compiler(*, policy: CompositionPolicy | None = None) -> CompositionCompiler:
    resources = {
        "mcp:local:workspace-git:1.0.0": _resource(
            "mcp:local:workspace-git:1.0.0", "mcp", "b"
        ),
        "skill:local:release-review:1.0.0": _resource(
            "skill:local:release-review:1.0.0", "skill", "c"
        ),
    }
    return CompositionCompiler(_registry(), _Catalog(resources), policy or _policy())


def test_revision_bindings_compile_to_one_fully_materialized_lock() -> None:
    resolved = _compiler().compile(_draft())

    assert resolved.profile.agent_provider.ref == (
        "plugin://io.example.fixture-provider@1.0.0"
    )
    assert resolved.profile.agent_provider.config == {
        "runtimeType": "adk",
        "runtimeVersion": "1.0.0",
    }
    assert all(item.ref.startswith("plugin://") for item in resolved.profile.capabilities)
    assert {entry.id for entry in resolved.plugin_lock.plugins} == {
        "io.example.fixture-provider",
        "io.ksadk.sqlite-session-store",
        "io.ksadk.sqlite-memory",
        "io.ksadk.memory-context",
        "io.ksadk.core-renderer",
        "io.ksadk.workspace-mcp",
        "io.ksadk.workspace-skill",
    }
    mcp = next(
        item
        for item in resolved.profile.capabilities
        if item.ref == "plugin://io.ksadk.workspace-mcp@1.0.0"
    )
    assert mcp.config["resources"] == [
        {
            "resourceId": "mcp:local:workspace-git:1.0.0",
            "kind": "mcp",
            "name": "workspace-git",
            "version": "1.0.0",
            "digest": "sha256:" + "b" * 64,
            "binding": {"namespace": "source"},
            "materializer": {},
        }
    ]
    assert resolved.profile.ui_contributions == [
        "plugin://io.ksadk.core-renderer@1.0.0"
    ]
    assert resolved.profile.policies == {
        "network": "policy://restricted@1",
        "tool": "policy://strict@1",
    }


def test_installed_external_provider_is_selected_by_exact_reference() -> None:
    policy = _policy()
    provider_ref = "plugin://io.example.fixture-provider@1.0.0"
    policy = CompositionPolicy(
        runtimes=policy.runtimes,
        providers={
            provider_ref: RuntimePluginSelection(
                provider_ref=provider_ref,
                provider_config={"profile": "stable"},
                supported_strategies=frozenset({"direct", "plan-act-observe"}),
            )
        },
        session_store=policy.session_store,
        resource_materializations=policy.resource_materializations,
        memory_providers=policy.memory_providers,
        context_contributors=policy.context_contributors,
        renderers=policy.renderers,
    )
    draft = _draft()
    draft.spec.runtime = RuntimeRef(
        type="plugin",
        provider_ref=provider_ref,
        provider_config={"workspace": "fixture"},
        version="1.0.0",
    )

    resolved = _compiler(policy=policy).compile(draft)

    assert resolved.profile.agent_provider.ref == provider_ref
    assert resolved.profile.agent_provider.config == {
        "profile": "stable",
        "workspace": "fixture",
        "runtimeType": "plugin",
        "runtimeVersion": "1.0.0",
    }


def test_plugin_runtime_rejects_uninstalled_provider_or_clear_secret() -> None:
    provider_ref = "plugin://io.example.fixture-provider@1.0.0"
    draft = _draft()
    draft.spec.runtime = RuntimeRef(type="plugin", provider_ref=provider_ref)

    with pytest.raises(CompositionCompileError) as captured:
        _compiler().compile(draft)

    assert captured.value.code == "agent_provider_unavailable"
    assert captured.value.field == "spec.runtime.providerRef"

    with pytest.raises(ValueError, match="Secret 引用"):
        RuntimeRef(
            type="plugin",
            provider_ref=provider_ref,
            provider_config={"apiKey": "clear-text"},
        )


def test_compilation_is_independent_of_binding_and_policy_map_order() -> None:
    first = _compiler().compile(_draft())
    policy = _policy()
    reversed_policy = CompositionPolicy(
        runtimes=dict(reversed(list(policy.runtimes.items()))),
        session_store=policy.session_store,
        resource_materializations=dict(
            reversed(list(policy.resource_materializations.items()))
        ),
        memory_providers=dict(reversed(list(policy.memory_providers.items()))),
        context_contributors=dict(reversed(list(policy.context_contributors.items()))),
        renderers=tuple(reversed(policy.renderers)),
    )
    second = _compiler(policy=reversed_policy).compile(_draft())

    assert first.profile_digest == second.profile_digest
    assert first.plugin_lock_digest == second.plugin_lock_digest


def test_raw_mcp_or_skill_identity_is_rejected_instead_of_looking_active() -> None:
    compiler = _compiler(policy=_policy(mcp_ref="mcp://workspace/git@3"))

    with pytest.raises(CompositionCompileError) as captured:
        compiler.compile(_draft())

    assert captured.value.code == "resource_capability_unmaterialized"
    assert captured.value.field == "spec.bindings.mcp"


def test_enabled_binding_without_materializer_fails_before_resolution() -> None:
    policy = _policy()
    missing = CompositionPolicy(
        runtimes=policy.runtimes,
        session_store=policy.session_store,
        resource_materializations={
            key: value
            for key, value in policy.resource_materializations.items()
            if value.kind != "skill"
        },
        memory_providers=policy.memory_providers,
        context_contributors=policy.context_contributors,
        renderers=policy.renderers,
    )

    with pytest.raises(CompositionCompileError) as captured:
        _compiler(policy=missing).compile(_draft())

    assert captured.value.code == "resource_capability_unmaterialized"
    assert "release-review" in str(captured.value)


def test_provider_policy_rejects_an_unsupported_strategy_in_revision() -> None:
    policy = _policy()
    native = CompositionPolicy(
        runtimes={
            "adk": RuntimePluginSelection(
                provider_ref="plugin://io.ksadk.native-provider@1.0.0"
            )
        },
        session_store=policy.session_store,
    )

    with pytest.raises(CompositionCompileError) as captured:
        _compiler(policy=native).compile(_draft())

    assert captured.value.code == "execution_strategy_unavailable"
    assert captured.value.field == "spec.execution.strategy"


def test_materializer_manifest_must_provide_the_resource_definition() -> None:
    policy = _policy(mcp_ref="plugin://io.ksadk.workspace-skill@1.0.0")

    with pytest.raises(CompositionCompileError) as captured:
        _compiler(policy=policy).compile(_draft())

    assert captured.value.code == "plugin_capability_mismatch"
    assert "mcp.connector/v1" in str(captured.value)


def test_one_plugin_can_own_multiple_selected_definitions_once() -> None:
    policy = _policy()
    shared = "plugin://io.ksadk.shared-state@1.0.0"
    policy = CompositionPolicy(
        runtimes=policy.runtimes,
        session_store=PluginCapabilitySelection(
            ref=shared,
            definition="session.event-store/v1",
            slot="session.events",
            config={"database": "state.sqlite3"},
        ),
        resource_materializations=policy.resource_materializations,
        memory_providers={
            "local-default": PluginCapabilitySelection(
                ref=shared,
                definition="memory.provider/v1",
                slot="memory.primary",
            )
        },
        context_contributors=policy.context_contributors,
        renderers=policy.renderers,
    )

    resolved = _compiler(policy=policy).compile(_draft())

    selected = [item for item in resolved.profile.capabilities if item.ref == shared]
    assert len(selected) == 1
    assert selected[0].config == {
        "database": "state.sqlite3",
        "providerRef": "local-default",
        "scopes": ["agent", "user", "workspace"],
    }


def test_conflicting_multi_definition_plugin_config_fails_closed() -> None:
    policy = _policy()
    shared = "plugin://io.ksadk.shared-state@1.0.0"
    policy = CompositionPolicy(
        runtimes=policy.runtimes,
        session_store=PluginCapabilitySelection(
            ref=shared,
            definition="session.event-store/v1",
            slot="session.events",
            config={"database": "sessions.sqlite3"},
        ),
        resource_materializations=policy.resource_materializations,
        memory_providers={
            "local-default": PluginCapabilitySelection(
                ref=shared,
                definition="memory.provider/v1",
                slot="memory.primary",
                config={"database": "memory.sqlite3"},
            )
        },
        context_contributors=policy.context_contributors,
        renderers=policy.renderers,
    )

    with pytest.raises(CompositionCompileError) as captured:
        _compiler(policy=policy).compile(_draft())

    assert captured.value.code == "composition_config_conflict"
