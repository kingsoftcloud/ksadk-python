import httpx
import pytest

from ksadk.studio.api import create_studio_app
from ksadk.studio.codex_manifest import CodexAgentManifest, CodexRuntimeRef
from ksadk.studio.contracts import AgentBindings, AgentSpec, MemorySpec
from ksadk.studio.errors import StudioError
from ksadk.studio.resource_binding_validation import resource_binding_diagnostics
from ksadk.studio.resource_connections import ResourceConnectionDeclaration
from ksadk.studio.service import StudioService
from tests.resource_runtime.test_studio_resource_config import binding
from tests.studio.test_resource_connections import declaration


def configure(studio):
    payload = declaration().model_dump()
    payload["target"]["connection_ref"] = "connection-a"
    studio.resource_connections.save(
        ResourceConnectionDeclaration.model_validate(payload),
        expected_revision=0,
    )
    studio.credentials.put_session("RESOURCE_AK", "fake-access")
    studio.credentials.put_session("RESOURCE_SK", "fake-secret")


def test_real_codex_builder_checks_resource_configuration_before_runtime(tmp_path):
    def unexpected_runtime(runtime):
        raise AssertionError("Invalid resource configuration must fail before runtime inspection")

    studio = StudioService(tmp_path, codex_runtime_inspector=unexpected_runtime)
    studio.codex_manifests.save(
        CodexAgentManifest(
            name="resource-agent",
            version="1.0.0",
            runtime=CodexRuntimeRef(version="0.147.0"),
            model="fixture-model",
            prompt="Fixture",
            plugins=[binding()],
        )
    )
    with pytest.raises(StudioError) as missing:
        studio.codex_builder.build("resource-agent")
    assert missing.value.code == "RESOURCE_BINDING_VALIDATION_FAILED"
    assert missing.value.details["diagnostics"][0]["code"] == "RESOURCE_CONNECTION_NOT_FOUND"
    configure(studio)
    with pytest.raises(StudioError) as unsupported:
        studio.codex_builder.build("resource-agent")
    # Static checks do not remove the unimplemented DSH Build/Run delivery gate.
    assert unsupported.value.code == "CODEX_PLUGIN_ECOSYSTEM_UNSUPPORTED"


def test_memory_score_policy_is_checked_from_agent_spec(tmp_path):
    studio = StudioService(tmp_path)
    configure(studio)
    memory = MemorySpec(enabled=True, provider_ref="binding://binding-a", scopes=["user"])
    issues = resource_binding_diagnostics(
        [binding("memory-instance")], memory, studio.resource_connections
    )
    assert any(item.code == "MEMORY_SEARCH_POLICY_UNSUPPORTED" for item in issues)
    memory.recall.min_score = 0
    valid = resource_binding_diagnostics(
        [binding("memory-instance")], memory, studio.resource_connections
    )
    assert [item.code for item in valid] == ["RESOURCE_AUTHORITY_UNVERIFIED"]


def test_disabled_and_unrelated_plugins_do_not_require_resource_connections(tmp_path):
    studio = StudioService(tmp_path)
    plugins = [
        binding(enabled=False),
        binding(pluginRef="plugin://example.custom@1.0.0", config={}),
    ]
    assert resource_binding_diagnostics(plugins, None, studio.resource_connections) == []


async def test_resource_validation_api_uses_saved_revision_and_never_claims_authorization(tmp_path):
    studio = StudioService(tmp_path)
    draft = studio.drafts.create(
        agent_id="resource-agent",
        name="Resource Agent",
        spec=AgentSpec(bindings=AgentBindings(plugins=[binding()])),
    )
    app = create_studio_app(
        tmp_path, service=studio, session_token="fixture-session", csrf_token="fixture-csrf"
    )
    headers = {
        "X-AgentKit-Session": "fixture-session",
        "X-CSRF-Token": "fixture-csrf",
        "Origin": "http://testserver",
    }
    url = "/api/v1/agents/resource-agent/resource-bindings/validate"
    payload = {"expectedRevision": draft.metadata.revision}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (await client.post(url, json=payload)).status_code == 401
        missing = await client.post(url, json=payload, headers=headers)
        assert missing.status_code == 200
        assert not missing.json()["valid"]
        assert missing.json()["diagnostics"][0]["code"] == "RESOURCE_CONNECTION_NOT_FOUND"
        configure(studio)
        configured = await client.post(url, json=payload, headers=headers)
        assert configured.status_code == 200
        assert configured.json()["valid"]
        assert configured.json()["authorizationVerified"] is False
        assert "fake-secret" not in configured.text
        conflict = await client.post(
            url, json={"expectedRevision": draft.metadata.revision + 1}, headers=headers
        )
        assert conflict.status_code == 409
