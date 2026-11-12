from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ksadk.api import AgentEngineAPIError
from ksadk.studio.a2a_bindings import (
    A2ABindingFreezeRequest,
    A2ACatalogService,
)
from ksadk.studio.api import create_studio_app
from ksadk.studio.cloud import (
    DirectAgentEngineCloudDeploymentGateway,
    InMemoryCloudGateway,
)
from ksadk.studio.contracts import (
    AgentBindings,
    AgentSpec,
    BuildRecord,
    BuildStatus,
    DeploymentRequest,
    DeploymentTarget,
    Instructions,
    ModelSpec,
    NetworkPolicy,
    RemoteAgentBinding,
    SecuritySpec,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.service import StudioService

SPACE = "as-000000000021"
SPACE_TWO = "as-000000000031"
AGENT = "00000000000040008000000000000022"
VERSION = "00000000000040008000000000000023"
NEXT_VERSION = "00000000000040008000000000000024"


class CatalogGateway(InMemoryCloudGateway):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple] = []
        self.current_version = "00000000000040008000000000000023"
        self.fail_with: Exception | None = None

    def _raise_if_needed(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    async def get_a2a_space(self, space_id: str):
        self._raise_if_needed()
        self.calls.append(("space", space_id))
        return {"a2a_space_id": space_id, "status": "available"}

    async def list_a2a_spaces(self, *, page: int, size: int):
        self._raise_if_needed()
        self.calls.append(("spaces", page, size))
        return {
            "spaces": [
                {
                    "a2a_space_id": "as-000000000021",
                    "name": "Finance Space",
                    "status": "available",
                    "agent_count": 1,
                    "credential": "must-not-leak",
                }
            ],
            "total_count": 1,
            "page_number": page,
            "page_size": size,
        }

    async def list_a2a_space_available_agents(self, space_id: str):
        self._raise_if_needed()
        self.calls.append(("agents", space_id))
        return {
            "agents": [
                {
                    "a2a_agent_id": "00000000000040008000000000000022",
                    "version_id": self.current_version,
                    "card_sha256": "a" * 64,
                    "status": "available",
                    "invocation_status": "available",
                    "agent_card": {
                        "name": "Finance Agent",
                        "description": "Answers finance questions",
                        "url": "https://private-runtime.example.com/a2a",
                    },
                    "service_url": "https://private-runtime.example.com/a2a",
                    "call_permit": "must-not-leak",
                },
                {
                    "a2a_agent_id": "hidden-agent",
                    "version_id": self.current_version,
                    "status": "unavailable",
                    "invocation_status": "unavailable",
                    "agent_card": {"name": "Hidden"},
                },
            ]
        }

    async def get_a2a_agent(self, agent_id: str):
        self._raise_if_needed()
        self.calls.append(("agent", agent_id))
        return {
            "a2a_agent_id": "00000000000040008000000000000022",
            "latest_version_id": self.current_version,
            "name": "Finance Agent",
            "description": "Answers finance questions",
            "status": "available",
            "invocation_status": "available",
            "credential_ref": "secret://internal/a2a",
        }

    async def list_a2a_agent_versions(self, agent_id: str, *, page: int, size: int):
        self._raise_if_needed()
        self.calls.append(("versions", agent_id, page, size))
        return {
            "versions": [
                {
                    "version_id": self.current_version,
                    "sequence": 1,
                    "agent_version": "1.0.0",
                    "card_sha256": "a" * 64,
                    "archive_uri": "ks3://private/archive.zip",
                }
            ],
            "total_count": 1,
        }


def freeze_request() -> A2ABindingFreezeRequest:
    return A2ABindingFreezeRequest(
        space_id=SPACE,
        target_agent_id=AGENT,
        expected_version_id=VERSION,
        local_name="finance",
        exposure="native",
        policy_ref="policy://a2a/strict",
    )


def test_catalog_preserves_action_pagination_and_filters_sensitive_fields() -> None:
    gateway = CatalogGateway()
    catalog = A2ACatalogService(lambda: gateway)

    spaces = asyncio.run(catalog.list_spaces(page=2, size=10))
    agents = asyncio.run(catalog.list_space_agents(SPACE))
    detail = asyncio.run(catalog.get_agent(AGENT))
    versions = asyncio.run(catalog.list_agent_versions(AGENT, page=3, size=5))

    assert spaces == {
        "items": [
            {
                "spaceId": SPACE,
                "name": "Finance Space",
                "status": "available",
                "memberCount": 1,
            }
        ],
        "total": 1,
        "page": 2,
        "size": 10,
    }
    assert agents["items"] == [
        {
            "spaceId": SPACE,
            "targetAgentId": AGENT,
            "versionId": VERSION,
            "name": "Finance Agent",
            "description": "Answers finance questions",
            "cardSha256": "a" * 64,
            "callable": True,
        }
    ]
    assert detail == {
        "targetAgentId": AGENT,
        "versionId": VERSION,
        "name": "Finance Agent",
        "status": "available",
        "callable": True,
    }
    assert versions == {
        "items": [
            {
                "versionId": VERSION,
                "sequence": 1,
                "agentVersion": "1.0.0",
                "cardSha256": "a" * 64,
            }
        ],
        "total": 1,
        "page": 3,
        "size": 5,
    }
    assert gateway.calls == [
        ("spaces", 2, 10),
        ("space", SPACE),
        ("agents", "as-000000000021"),
        ("agent", "00000000000040008000000000000022"),
        ("versions", "00000000000040008000000000000022", 3, 5),
    ]
    assert "must-not-leak" not in str((spaces, agents, detail, versions))
    assert "private-runtime" not in str((spaces, agents, detail, versions))


def test_get_agent_projects_real_server_top_level_name() -> None:
    gateway = CatalogGateway()

    async def real_server_agent(agent_id: str):
        return {
            "a2a_agent_id": "00000000000040008000000000000022",
            "latest_version_id": gateway.current_version,
            "name": "Top-level Finance Agent",
            "description": "Server detail projection",
            "status": "available",
            "invocation_status": "available",
            "service_url": "https://private-runtime.example.com/a2a",
        }

    gateway.get_a2a_agent = real_server_agent  # type: ignore[method-assign]
    catalog = A2ACatalogService(lambda: gateway)

    detail = asyncio.run(catalog.get_agent(AGENT))

    assert detail["name"] == "Top-level Finance Agent"
    assert "private-runtime" not in str(detail)


def test_freeze_and_build_validation_detect_version_drift() -> None:
    gateway = CatalogGateway()
    catalog = A2ACatalogService(lambda: gateway)

    frozen = asyncio.run(catalog.freeze_binding(freeze_request()))
    assert isinstance(frozen, RemoteAgentBinding)
    assert frozen.expected_version_id == VERSION
    follow_current = asyncio.run(
        catalog.freeze_binding(
            A2ABindingFreezeRequest(
                space_id=SPACE,
                target_agent_id=AGENT,
                local_name="finance_current",
                exposure="tool",
                policy_ref="policy://a2a/strict",
                follow_current=True,
            )
        )
    )
    assert follow_current.expected_version_id == VERSION
    assert "followCurrent" not in follow_current.model_dump(by_alias=True)
    asyncio.run(catalog.validate_bindings([frozen]))

    gateway.current_version = "00000000000040008000000000000024"
    catalog = A2ACatalogService(
        lambda: gateway,

    )
    with pytest.raises(StudioError) as exc_info:
        asyncio.run(catalog.validate_bindings([frozen]))

    assert exc_info.value.code == "A2A_BINDING_VERSION_DRIFT"
    assert exc_info.value.details == {
        "bindingId": frozen.binding_id,
        "expectedVersionId": VERSION,
        "actualVersionId": NEXT_VERSION,
    }


def test_catalog_routes_use_process_gateway_and_redact_failures(tmp_path: Path) -> None:
    gateway = CatalogGateway()
    service = StudioService(
        tmp_path,
        cloud_gateway=gateway,

    )
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        response = client.get("/api/v1/a2a/spaces?page=1&size=20")
        assert response.status_code == 200
        assert response.json()["items"][0]["spaceId"] == SPACE

        frozen = client.post(
            "/api/v1/a2a/bindings:freeze",
            json=freeze_request().model_dump(by_alias=True, mode="json"),
        )
        assert frozen.status_code == 200, frozen.text
        assert frozen.json()["targetAgentId"] == AGENT

        gateway.fail_with = RuntimeError("Bearer secret-value-that-must-not-reach-browser")
        failed = client.get("/api/v1/a2a/spaces?page=1&size=20")

    assert failed.status_code == 502
    assert failed.json()["error"]["code"] == "A2A_CATALOG_UNAVAILABLE"
    assert "secret-value" not in failed.text


def test_catalog_routes_reject_invalid_path_ids_before_cloud_access(tmp_path: Path) -> None:
    gateway = CatalogGateway()
    service = StudioService(
        tmp_path,
        cloud_gateway=gateway,

    )
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        response = client.get("/api/v1/a2a/spaces/not-a-space-id/agents")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "A2A_CATALOG_RESOURCE_ID_INVALID"
    assert gateway.calls == []


def test_catalog_routes_preserve_permission_denial_without_leaking_upstream_error(
    tmp_path: Path,
) -> None:
    gateway = CatalogGateway()
    gateway.fail_with = AgentEngineAPIError(
        403,
        "denied Bearer must-not-leak",
        details={"credential": "must-not-leak"},
    )
    service = StudioService(
        tmp_path,
        cloud_gateway=gateway,

    )
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        response = client.get("/api/v1/a2a/spaces?page=1&size=20")

    assert response.status_code == 403
    assert response.json()["error"] == {
        "code": "A2A_CATALOG_FORBIDDEN",
        "message": "当前账号无权读取 A2A 目录",
        "details": {"operation": "list_a2a_spaces"},
        "requestId": response.headers["x-request-id"],
    }
    assert "must-not-leak" not in response.text


def test_catalog_routes_redact_gateway_studio_error_messages(tmp_path: Path) -> None:
    gateway = CatalogGateway()
    gateway.fail_with = StudioError(
        "A2A_CATALOG_UNAVAILABLE",
        "Bearer must-not-leak",
        status_code=501,
        details={"credential": "must-not-leak"},
    )
    service = StudioService(
        tmp_path,
        cloud_gateway=gateway,

    )
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        response = client.get("/api/v1/a2a/spaces?page=1&size=20")

    assert response.status_code == 501
    assert response.json()["error"]["code"] == "A2A_CATALOG_UNAVAILABLE"
    assert "must-not-leak" not in response.text


def test_catalog_rejects_malformed_server_pagination_as_safe_protocol_error() -> None:
    gateway = CatalogGateway()

    async def malformed_spaces(*, page: int, size: int):
        return {
            "spaces": [],
            "total_count": "not-an-integer",
            "page_number": page,
            "page_size": size,
        }

    gateway.list_a2a_spaces = malformed_spaces  # type: ignore[method-assign]
    catalog = A2ACatalogService(lambda: gateway)

    with pytest.raises(StudioError) as exc_info:
        asyncio.run(catalog.list_spaces(page=1, size=20))

    assert exc_info.value.code == "A2A_CATALOG_PROTOCOL_INVALID"
    assert "not-an-integer" not in str(exc_info.value.details)


def test_catalog_rejects_non_list_server_collections_instead_of_hiding_drift() -> None:
    gateway = CatalogGateway()

    async def malformed_spaces(*, page: int, size: int):
        return {
            "spaces": {"unexpected": "object"},
            "total_count": 1,
            "page_number": page,
            "page_size": size,
        }

    gateway.list_a2a_spaces = malformed_spaces  # type: ignore[method-assign]
    catalog = A2ACatalogService(lambda: gateway)

    with pytest.raises(StudioError) as exc_info:
        asyncio.run(catalog.list_spaces(page=1, size=20))

    assert exc_info.value.code == "A2A_CATALOG_PROTOCOL_INVALID"


def test_binding_freeze_rejects_malformed_authoritative_card_digest() -> None:
    gateway = CatalogGateway()

    async def agents_with_bad_digest(space_id: str):
        payload = await CatalogGateway.list_a2a_space_available_agents(gateway, space_id)
        payload["agents"][0]["card_sha256"] = "not-a-sha256"
        return payload

    gateway.list_a2a_space_available_agents = (  # type: ignore[method-assign]
        agents_with_bad_digest
    )
    catalog = A2ACatalogService(lambda: gateway)

    with pytest.raises(StudioError) as exc_info:
        asyncio.run(catalog.freeze_binding(freeze_request()))

    assert exc_info.value.code == "A2A_CATALOG_PROTOCOL_INVALID"


def test_binding_validation_checks_each_space_without_cross_space_cache_aliasing() -> None:
    gateway = CatalogGateway()
    catalog = A2ACatalogService(lambda: gateway)
    first = asyncio.run(catalog.freeze_binding(freeze_request()))
    second = RemoteAgentBinding.freeze(
        space_id=SPACE_TWO,
        target_agent_id=AGENT,
        expected_version_id=VERSION,
        local_name="finance_backup",
        exposure="tool",
        policy_ref="policy://a2a/strict",
    )

    asyncio.run(catalog.validate_bindings([first, second]))

    assert gateway.calls[-4:] == [("space", SPACE), ("agents", SPACE),
                                  ("space", SPACE_TWO), ("agents", SPACE_TWO)]


def test_direct_cloud_gateway_calls_frozen_a2a_actions_with_real_pagination_shape() -> None:
    class ActionClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def _action_async(self, action: str, payload: dict):
            self.calls.append((action, payload))
            return {"action": action}

    client = ActionClient()
    gateway = DirectAgentEngineCloudDeploymentGateway(
        region="pre-online",
        client=client,
        ks3_credentials={"access_key": "fixture-ak", "secret_key": "fixture-sk"},
    )

    asyncio.run(gateway.list_a2a_spaces(page=2, size=30))
    asyncio.run(gateway.list_a2a_space_available_agents("as-000000000021"))
    asyncio.run(gateway.get_a2a_agent("00000000000040008000000000000022"))
    asyncio.run(gateway.list_a2a_agent_versions("00000000000040008000000000000022", page=3, size=5))

    assert client.calls == [
        ("ListAToASpaces", {"PageNumber": 2, "PageSize": 30}),
        ("ListAToASpaceAvailableAgents", {"A2ASpaceId": "as-000000000021"}),
        ("GetAToAAgent", {"A2AAgentId": "00000000000040008000000000000022"}),
        (
            "ListAToAAgentVersions",
            {"A2AAgentId": "00000000000040008000000000000022", "PageNumber": 3, "PageSize": 5},
        ),
    ]


def test_submit_build_fails_on_drift_without_rewriting_revision(tmp_path: Path) -> None:
    gateway = CatalogGateway()
    service = StudioService(
        tmp_path,
        cloud_gateway=gateway,

    )
    frozen = asyncio.run(service.a2a_catalog.freeze_binding(freeze_request()))
    draft = service.create_agent(
        agent_id="drift-agent",
        name="Drift Agent",
        spec=AgentSpec(
            instructions=Instructions(system="Use the pinned remote agent."),
            model=ModelSpec(
                model="fixture-model",
                endpoint_url="https://model.example.com/v1/chat/completions",
                credential_ref="env://MODEL_API_KEY",
            ),
            bindings=AgentBindings(remote_agents=[frozen]),
            security=SecuritySpec(
                network=NetworkPolicy(allowed_hosts=["model.example.com"]),
            ),
        ),
    )
    gateway.current_version = "00000000000040008000000000000024"

    async def submit_and_wait():
        operation = service.submit_build(
            draft.metadata.id,
            revision=draft.metadata.revision,
            idempotency_key="drift-build-1",
        )
        return await service.operations.wait(operation.id)

    operation = asyncio.run(submit_and_wait())

    assert operation.status == "FAILED"
    assert operation.error == {
        "code": "A2A_BINDING_VERSION_DRIFT",
        "message": "远端 Agent 当前版本与冻结 binding 不一致",
        "field": None,
        "details": {
            "bindingId": frozen.binding_id,
            "expectedVersionId": VERSION,
            "actualVersionId": NEXT_VERSION,
        },
    }
    persisted = service.drafts.get(draft.metadata.id)
    assert persisted.metadata.revision == draft.metadata.revision
    assert persisted.spec.bindings.remote_agents == [frozen]
    assert service.builds.list_for_agent(draft.metadata.id) == []


def test_submit_deployment_revalidates_frozen_bundle_before_upload(tmp_path: Path) -> None:
    gateway = CatalogGateway()
    service = StudioService(
        tmp_path,
        cloud_gateway=gateway,

    )
    frozen = asyncio.run(service.a2a_catalog.freeze_binding(freeze_request()))
    draft = service.create_agent(
        agent_id="deploy-drift-agent",
        name="Deploy Drift Agent",
        spec=AgentSpec(
            instructions=Instructions(system="Use the pinned remote agent."),
            model=ModelSpec(
                model="fixture-model",
                endpoint_url="https://model.example.com/v1/chat/completions",
                credential_ref="env://MODEL_API_KEY",
            ),
            bindings=AgentBindings(remote_agents=[frozen]),
            security=SecuritySpec(
                network=NetworkPolicy(allowed_hosts=["model.example.com"]),
            ),
        ),
    )

    async def build_then_deploy():
        build_operation = service.submit_build(
            draft.metadata.id,
            revision=draft.metadata.revision,
            idempotency_key="deploy-drift-build",
        )
        completed_build = await service.operations.wait(build_operation.id)
        assert completed_build.status == "SUCCEEDED"
        gateway.current_version = "00000000000040008000000000000024"
        deploy_operation = service.submit_deployment(
            completed_build.resource_id,
            DeploymentRequest(target=DeploymentTarget(region="test", environment="test")),
            idempotency_key="deploy-drift-deploy",
        )
        return await service.operations.wait(deploy_operation.id)

    deployment = asyncio.run(build_then_deploy())

    assert deployment.status == "FAILED"
    assert deployment.error["code"] == "A2A_BINDING_VERSION_DRIFT"
    assert gateway.uploads == []


def test_build_binding_loader_rejects_non_object_resolved_spec(tmp_path: Path) -> None:
    gateway = CatalogGateway()
    service = StudioService(
        tmp_path,
        cloud_gateway=gateway,

    )
    archive = service.workspace.resolve("dist/malformed/agent-bundle.zip")
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("resolved-agent-spec.json", "[]")
    service.builds.save(
        BuildRecord(
            id="build_malformed_a2a",
            agent_id="malformed-agent",
            source_revision=1,
            status=BuildStatus.SUCCEEDED,
            artifact_path=service.workspace.relative(archive),
        )
    )

    with pytest.raises(StudioError) as exc_info:
        service._remote_bindings_for_build("build_malformed_a2a")

    assert exc_info.value.code == "A2A_BINDING_BUNDLE_INVALID"
    assert gateway.calls == []

@pytest.mark.parametrize("drift", ["space", "membership", "callable"])
async def test_space_membership_and_callable_drift_reject_frozen_binding(drift):
    gateway = CatalogGateway()
    catalog = A2ACatalogService(lambda: gateway)
    frozen = await catalog.freeze_binding(freeze_request())
    if drift == "space":
        async def changed_space(space_id):
            return {"a2a_space_id": space_id, "status": "unavailable"}
        gateway.get_a2a_space = changed_space
    else:
        original = gateway.list_a2a_space_available_agents
        async def changed_members(space_id):
            value = await original(space_id)
            if drift == "membership":
                value["agents"] = []
            else:
                value["agents"][0]["invocation_status"] = "unavailable"
            return value
        gateway.list_a2a_space_available_agents = changed_members
    with pytest.raises(StudioError) as error:
        await catalog.validate_bindings([frozen])
    assert error.value.code == "A2A_BINDING_TARGET_UNAVAILABLE"
    assert frozen.expected_version_id == VERSION


@pytest.mark.parametrize("change", ["replace", "remove"])
async def test_current_build_does_not_reuse_previous_remote_bindings(tmp_path, monkeypatch, change):
    gateway = CatalogGateway()
    service = StudioService(tmp_path, cloud_gateway=gateway)
    async def started():
        pass
    monkeypatch.setattr(service, "start", started)
    remote = await service.a2a_catalog.freeze_binding(freeze_request())
    draft = service.create_agent(
        agent_id="binding-cache", name="Binding Cache",
        spec=AgentSpec(
            instructions=Instructions(system="Delegate research."),
            model=ModelSpec(model="fixture-model", endpoint_url="https://model.example.com/v1/chat/completions",
                            credential_ref="env://MODEL_API_KEY"),
            bindings=AgentBindings(remote_agents=[remote]),
            security=SecuritySpec(network=NetworkPolicy(allowed_hosts=["model.example.com"])),
        ),
    )
    first = await service.ensure_current_build(draft.metadata.id)
    original_archive = service.workspace.resolve(first.artifact_path).read_bytes()
    changed = [] if change == "remove" else [RemoteAgentBinding.freeze(
        space_id=SPACE, target_agent_id=AGENT, expected_version_id=VERSION,
        local_name="renamed_expert", exposure="tool", policy_ref="policy://a2a/strict",
    )]
    service.update_agent_bindings(draft.metadata.id, AgentBindings(remote_agents=changed),
                                  expected_revision=draft.metadata.revision)
    second = await service.ensure_current_build(draft.metadata.id)
    assert second.id != first.id
    assert second.a2a_binding_digest != first.a2a_binding_digest
    assert service._remote_bindings_for_build(second.id) == changed
    assert service.workspace.resolve(first.artifact_path).read_bytes() == original_archive
    assert service._remote_bindings_for_build(first.id) == [remote]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["handoff", "remove"])
async def test_orchestration_change_invalidates_build_cache(tmp_path, monkeypatch, change):
    from ksadk.studio.contracts import A2AOrchestration, RuntimeRef
    gateway = CatalogGateway()
    service = StudioService(tmp_path, cloud_gateway=gateway)
    async def started():
        pass
    monkeypatch.setattr(service, "start", started)
    remote = await service.a2a_catalog.freeze_binding(freeze_request())
    source = tmp_path / "project"
    source.mkdir()
    (source / "graph.py").write_text("root_agent = None\n")
    plan = A2AOrchestration.model_validate({
        "entryNodeId": "local", "nodes": [{"nodeId": "local", "kind": "local"},
        {"nodeId": "remote", "kind": "remote", "bindingId": remote.binding_id}],
        "edges": [{"source": "local", "target": "remote", "kind": "sequential"}],
    })
    original = service.create_agent(agent_id="composition-cache", name="Composition Cache", spec=AgentSpec(
        runtime=RuntimeRef(type="langgraph", project_path="project", entry_point="graph.py"),
        instructions=Instructions(system="Delegate research."),
        model=ModelSpec(model="fixture", endpoint_url="https://model.example.com/v1/chat/completions", credential_ref="env://MODEL_KEY"),
        security=SecuritySpec(network=NetworkPolicy(allowed_hosts=["model.example.com"])),
        bindings=AgentBindings(remote_agents=[remote]), a2a_orchestration=plan,
    ))
    first = await service.ensure_current_build(original.metadata.id)
    previous = service.workspace.resolve(first.artifact_path).read_bytes()
    spec = original.spec.model_copy(deep=True)
    if change == "remove":
        spec.a2a_orchestration = None
    else:
        spec.a2a_orchestration.edges[0].kind = "handoff"
    service.update_agent(original.metadata.id, spec, expected_revision=original.metadata.revision)
    second = await service.ensure_current_build(original.metadata.id)
    assert first.id != second.id
    assert first.a2a_binding_digest == second.a2a_binding_digest
    assert first.a2a_orchestration_digest != second.a2a_orchestration_digest
    assert service.workspace.resolve(first.artifact_path).read_bytes() == previous


def test_orchestration_preview_revalidates_binding_before_returning_source(tmp_path):
    from ksadk.studio.contracts import A2AOrchestration, RuntimeRef
    gateway = CatalogGateway()
    service = StudioService(tmp_path, cloud_gateway=gateway)
    remote = asyncio.run(service.a2a_catalog.freeze_binding(freeze_request()))
    service.create_agent(agent_id="preview", name="Preview", spec=AgentSpec(
        runtime=RuntimeRef(type="langgraph", project_path=".", entry_point="graph.py"),
        bindings=AgentBindings(remote_agents=[remote]),
        a2a_orchestration=A2AOrchestration.model_validate({"entryNodeId": "remote", "nodes": [
            {"nodeId": "remote", "kind": "remote", "bindingId": remote.binding_id}], "edges": []}),
    ))
    app = create_studio_app(tmp_path, service=service, security_enabled=False)
    with TestClient(app) as client:
        first = client.get("/api/v1/agents/preview/a2a:preview")
        assert first.status_code == 200
        assert "remote_node" in first.json()["source"]
        assert first.json()["digest"].startswith("sha256:")
        gateway.current_version = NEXT_VERSION
        changed = client.get("/api/v1/agents/preview/a2a:preview")
        assert changed.status_code == 409
        assert "source" not in changed.json()


def test_unsaved_preview_is_revision_guarded_and_does_not_modify_draft(tmp_path):
    from ksadk.studio.contracts import RuntimeRef
    gateway = CatalogGateway()
    service = StudioService(tmp_path, cloud_gateway=gateway)
    remote = asyncio.run(service.a2a_catalog.freeze_binding(freeze_request()))
    draft = service.create_agent(agent_id="unsaved-preview", name="Unsaved Preview", spec=AgentSpec(
        runtime=RuntimeRef(type="langgraph", project_path=".", entry_point="graph.py")))
    payload = {
        "expectedRevision": draft.metadata.revision,
        "runtime": draft.spec.runtime.model_dump(by_alias=True),
        "bindings": [remote.model_dump(by_alias=True)],
        "orchestration": {"entryNodeId": "remote", "nodes": [
            {"nodeId": "remote", "kind": "remote", "bindingId": remote.binding_id}], "edges": []},
    }
    app = create_studio_app(tmp_path, service=service, security_enabled=False)
    with TestClient(app) as client:
        url = "/api/v1/agents/unsaved-preview/a2a:preview"
        preview = client.post(url, json=payload)
        assert preview.status_code == 200
        assert "remote_node" in preview.json()["source"]
        assert "ksadk_graph_factory" in preview.json()["entrySource"]
        assert service.drafts.get(draft.metadata.id) == draft
        assert service.builds.list_for_agent(draft.metadata.id) == []
        payload["bindings"].append(remote.model_dump(by_alias=True))
        assert client.post(url, json=payload).status_code == 422
        payload["bindings"].pop()
        payload["expectedRevision"] += 1
        assert client.post(url, json=payload).status_code == 409
        payload["expectedRevision"] = draft.metadata.revision
        payload["orchestration"]["edges"] = [{"source": "remote", "target": "remote", "kind": "loop"}]
        assert client.post(url, json=payload).status_code == 422
        assert service.drafts.get(draft.metadata.id) == draft


async def test_catalog_accepts_actual_action_client_acronym_normalization():
    from ksadk.api.client import AgentEngineClient
    class ActualWire(CatalogGateway):
        async def get_a2a_space(self, space_id):
            return AgentEngineClient._to_snake_case({"A2ASpaceId": space_id, "Status": "available"})
        async def list_a2a_space_available_agents(self, space_id):
            return AgentEngineClient._to_snake_case({"Agents": [{
                "A2AAgentId": AGENT, "VersionId": VERSION, "CardSha256": "a" * 64,
                "Status": "available", "InvocationStatus": "available", "AgentCard": {"Name": "Expert"}}]})
    gateway = ActualWire()
    catalog = A2ACatalogService(lambda: gateway)
    frozen = await catalog.freeze_binding(freeze_request())
    assert frozen.target_agent_id == AGENT
    assert frozen.expected_version_id == VERSION


async def test_catalog_rejects_conflicting_resource_identity_aliases():
    class ConflictingWire(CatalogGateway):
        async def get_a2a_space(self, space_id):
            return {"a2a_space_id": space_id, "a2_a_space_id": SPACE_TWO, "status": "available"}
    with pytest.raises(StudioError) as failure:
        await A2ACatalogService(lambda: ConflictingWire()).freeze_binding(freeze_request())
    assert failure.value.code == "A2A_CATALOG_PROTOCOL_INVALID"
