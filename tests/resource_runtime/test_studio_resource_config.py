import pytest
from pydantic import ValidationError

from ksadk.memory.provider_resolver import resolve_memory_provider
from ksadk.studio.contracts import AgentBindings, AgentSpec, NativePluginBinding
from ksadk.studio.repository import AgentDraftRepository
from ksadk.studio.workspace import Workspace


def binding(kind="knowledge-base", **updates):
    plugin = {
        "knowledge-base": "knowledge",
        "memory-instance": "memory",
        "skill-space": "skill-center",
    }[kind]
    payload = {
        "ecosystem": "dsh",
        "pluginRef": f"plugin://kingsoftcloud.dsh-{plugin}@0.1.0",
        "snapshotDigest": "sha256:" + "a" * 64,
        "components": ["tool:fixture"],
        "config": {
            "binding": {
                "id": "binding-a",
                "connectionRef": "connection-a",
                "resource": {"kind": kind, "id": "resource-a", "region": "region-a"},
            }
        },
    }
    payload.update(updates)
    return NativePluginBinding.model_validate(payload)


def test_resource_binding_round_trips_through_real_draft_repository(tmp_path):
    workspace = Workspace(tmp_path)
    workspace.initialize()
    repository = AgentDraftRepository(workspace)
    original = binding()
    repository.create(
        agent_id="resource-agent",
        name="Resource Agent",
        spec=AgentSpec(bindings=AgentBindings(plugins=[original])),
    )
    loaded = repository.get("resource-agent")
    assert loaded.spec.bindings.plugins == [original]


@pytest.mark.parametrize("change", ["ecosystem", "kind", "version", "unknown", "missing"])
def test_official_resource_config_is_validated_at_binding_boundary(change):
    payload = binding().model_dump(by_alias=True, mode="json")
    if change == "ecosystem":
        payload["ecosystem"] = "codex"
    elif change == "kind":
        payload["config"]["binding"]["resource"]["kind"] = "memory-instance"
    elif change == "version":
        payload["config"]["schemaVersion"] = 2
    elif change == "unknown":
        payload["config"]["datasetOverride"] = "other"
    else:
        payload["config"] = {}
    with pytest.raises(ValidationError):
        NativePluginBinding.model_validate(payload)


def test_multiple_plugin_versions_cannot_bind_two_libraries():
    first = binding()
    second = binding(pluginRef="plugin://kingsoftcloud.dsh-knowledge@0.2.0")
    with pytest.raises(ValidationError):
        AgentBindings(plugins=[first, second])


def test_codex_manifest_cannot_bypass_resource_cardinality():
    from ksadk.studio.codex_manifest import CodexAgentManifest, CodexRuntimeRef

    with pytest.raises(ValidationError):
        CodexAgentManifest(
            name="resource-agent",
            version="1.0.0",
            runtime=CodexRuntimeRef(version="0.147.0"),
            model="test-model",
            prompt="test",
            plugins=[binding(), binding(pluginRef="plugin://kingsoftcloud.dsh-knowledge@0.2.0")],
        )


def test_third_party_plugin_configuration_is_not_interpreted_as_official():
    arbitrary = binding(pluginRef="plugin://example.custom@1.0.0", config={"custom": "value"})
    assert arbitrary.config == {"custom": "value"}


@pytest.mark.parametrize("case", ["missing", "disabled", "wrong-kind", "broad-scope"])
def test_memory_reference_must_resolve_to_enabled_memory_binding(case):
    memory_binding = binding("memory-instance", enabled=case != "disabled")
    plugins = [] if case == "missing" else [binding() if case == "wrong-kind" else memory_binding]
    with pytest.raises(ValidationError):
        AgentSpec(
            bindings=AgentBindings(plugins=plugins),
            memory={
                "enabled": True,
                "providerRef": "binding://binding-a",
                "scopes": ["workspace"] if case == "broad-scope" else ["user"],
            },
        )


def test_valid_memory_binding_does_not_fall_back_to_sqlite_without_activation():
    spec = AgentSpec(
        bindings=AgentBindings(plugins=[binding("memory-instance")]),
        memory={"enabled": True, "providerRef": "binding://binding-a", "scopes": ["user"]},
    )
    with pytest.raises(ValueError, match="REQUIRES_ACTIVATION"):
        resolve_memory_provider(spec.memory.provider_ref)
