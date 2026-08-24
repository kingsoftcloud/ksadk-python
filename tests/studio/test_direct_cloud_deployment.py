"""Studio cloud delivery stays on the existing signed Code deployment path."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ksadk.api import AgentEngineAPIError
from ksadk.studio.builder import AgentBundleBuilder
from ksadk.studio.cloud import CloudDeploymentService, DirectAgentEngineCloudDeploymentGateway
from ksadk.studio.contracts import (
    AgentDraft,
    AgentMetadata,
    AgentSpec,
    DeploymentRecord,
    DeploymentRequest,
    DeploymentTarget,
    Instructions,
    ModelSpec,
    NetworkPolicy,
    RuntimeRef,
    SecuritySpec,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.workspace import Workspace


def test_direct_gateway_signs_control_actions_with_process_credentials(monkeypatch) -> None:
    captured: dict[str, str] = {}

    class _CapturedClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("ksadk.studio.cloud.AgentEngineClient", _CapturedClient)
    DirectAgentEngineCloudDeploymentGateway(
        region="pre-online",
        ks3_credentials={"access_key": "test-access", "secret_key": "test-secret"},
    )

    assert captured == {
        "region": "pre-online",
        "access_key": "test-access",
        "secret_key": "test-secret",
    }


def test_studio_composition_explicitly_builds_a_signed_control_client(monkeypatch) -> None:
    from ksadk.studio.service import StudioService

    captured: list[dict[str, str]] = []

    class _CapturedClient:
        def __init__(self, **kwargs) -> None:
            captured.append(kwargs)

    monkeypatch.setenv("KSYUN_ACCESS_KEY", "studio-access")
    monkeypatch.setenv("KSYUN_SECRET_KEY", "studio-secret")
    monkeypatch.setenv("KSYUN_REGION", "pre-online")
    monkeypatch.setattr("ksadk.studio.service.AgentEngineClient", _CapturedClient)

    gateway = StudioService._configured_cloud_gateway()

    assert isinstance(gateway, DirectAgentEngineCloudDeploymentGateway)
    assert captured == [
        {
            "region": "pre-online",
            "access_key": "studio-access",
            "secret_key": "studio-secret",
        },
        {
            "base_url": "http://agent-api-pre.kspmas-internal.ksyun.com",
            "region": "pre-online",
            "access_key": "studio-access",
            "secret_key": "studio-secret",
        },
    ]
    assert gateway.client is not gateway.stream_client


class _Uploader:
    calls: list[tuple[bytes, str]] = []

    def __init__(self, *, region: str, bucket: str | None = None) -> None:
        self.region = region
        self.bucket_name = bucket or "agentengine-test"

    async def upload(self, path: Path, object_key: str) -> str:
        self.calls.append((path.read_bytes(), object_key))
        return f"ks3://{self.bucket_name}/{object_key}"


class _Client:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.updated: list[tuple[str, dict]] = []
        self.dashboard_links: list[dict] = []
        self.session_calls: list[tuple[str, dict]] = []
        self.kernel_ready = True

    async def create_agent(self, payload: dict) -> dict:
        self.created.append(payload)
        return {"agent_id": "ar-studio-1", "instance_id": "instance-studio-1"}

    async def update_agent(self, agent_id: str, payload: dict) -> dict:
        self.updated.append((agent_id, payload))
        return {"agent_id": agent_id}

    async def get_agent(self, *, agent_id: str) -> dict:
        assert agent_id == "ar-studio-1"
        return {
            "status": "Running",
            "endpoint": "http://ar-studio-1.example.test",
            "deployment": {"agent_kernel_ready": self.kernel_ready},
        }

    async def create_dashboard_access_link(self, **kwargs) -> dict:
        self.dashboard_links.append(kwargs)
        return {
            "access_url": f"https://dashboard.example.test/{kwargs['agent_id']}",
            "expires_at": "2026-08-22T00:00:00Z",
        }

    async def list_sessions(self, agent_id: str, *, page: int, size: int) -> dict:
        self.session_calls.append(
            ("ListSessions", {"AgentId": agent_id, "Page": page, "PageSize": size})
        )
        return {"sessions": [{"id": "sess-cloud"}], "total": 1}

    async def create_session(self, agent_id: str) -> dict:
        self.session_calls.append(("CreateSession", {"AgentId": agent_id}))
        return {"session": {"id": "sess-new"}}

    async def delete_session(self, session_id: str) -> bool:
        self.session_calls.append(("DeleteSession", {"SessionId": session_id}))
        return True

    async def list_session_messages(self, **kwargs) -> dict:
        self.session_calls.append(("ListSessionMessages", kwargs))
        return {
            "messages": [{"role": "assistant", "content": "云端回复"}],
            "latest_seq_id": 4,
        }

    async def list_session_events(self, **kwargs) -> dict:
        self.session_calls.append(("ListSessionEvents", kwargs))
        return {"events": [{"event_type": "run.completed"}]}

    async def chat(
        self,
        agent_id: str,
        message,
        *,
        session_id: str | None = None,
        model: str | None = None,
        model_options: dict | None = None,
        tool_approval_mode: str | None = None,
        collaboration_mode: str | None = None,
        goal_objective: str | None = None,
    ) -> dict:
        call = {
            "AgentId": agent_id,
            "SessionId": session_id,
            "Message": message,
            "Model": model,
            "ModelOptions": model_options,
            "ToolApprovalMode": tool_approval_mode,
        }
        if collaboration_mode is not None:
            call["CollaborationMode"] = collaboration_mode
        if goal_objective is not None:
            call["GoalObjective"] = goal_objective
        self.session_calls.append(
            (
                "RunAgent",
                call,
            )
        )
        return {"receipt_status": "accepted", "run_id": "run-cloud"}

    async def list_agent_models(self, *, agent_id: str) -> dict:
        self.session_calls.append(("ListAgentModels", {"AgentId": agent_id}))
        return {"models": [{"id": "qwen3-coder-plus"}], "current": "qwen3-coder-plus"}


class _MissingAgentClient(_Client):
    async def get_agent(self, *, agent_id: str) -> dict:
        raise AgentEngineAPIError(404, "未找到对应的 Agent")


class _DeletedSessionClient(_Client):
    async def list_session_messages(self, **kwargs) -> dict:
        raise AgentEngineAPIError(404, "Session not found")

    async def list_session_events(self, **kwargs) -> dict:
        raise AgentEngineAPIError(404, "Session not found")


@pytest.mark.asyncio
async def test_direct_gateway_uses_ks3_and_existing_agent_actions_only() -> None:
    _Uploader.calls.clear()
    client = _Client()
    gateway = DirectAgentEngineCloudDeploymentGateway(
        region="pre-online",
        client=client,
        uploader_factory=_Uploader,
        ks3_credentials={"access_key": "test-access", "secret_key": "test-secret"},
    )
    bundle = b"deterministic-studio-bundle"
    archive_sha = hashlib.sha256(bundle).hexdigest()
    bundle_digest = f"sha256:{archive_sha}"
    request = DeploymentRequest(
        target=DeploymentTarget(region="pre-online", environment="preproduction")
    )

    bundle_uri = await gateway.upload_bundle(
        bundle=bundle,
        bundle_digest=bundle_digest,
        provenance={"agentId": "studio-graph", "runtimeType": "langgraph"},
    )
    version = await gateway.create_version(
        agent_id="studio-graph",
        bundle_uri=bundle_uri,
        bundle_digest=bundle_digest,
        provenance={},
    )
    deployment = await gateway.create_deployment(
        build_id="build-1",
        version_id=version,
        bundle_digest=bundle_digest,
        request=request,
    )

    assert _Uploader.calls == [(bundle, f"studio-bundles/studio-graph/{archive_sha}/bundle.zip")]
    assert client.created == [
        {
            "name": "studio-graph",
            "description": "Created by AgentKit Studio",
            "framework": "langgraph",
            "artifact_type": "Code",
            "artifact_path": bundle_uri,
            "code_checksum": archive_sha,
            "code_command": [
                "ksadk",
                "web",
                "/app/code/runtime",
                "--port",
                "8080",
                "--host",
                "0.0.0.0",
                "--no-open",
            ],
            "region": "pre-online",
            "ks3": {
                "access_key": "test-access",
                "secret_key": "test-secret",
                "region": "cn-beijing-6",
                "bucket": "agentengine-test",
            },
            "resources": {"cpu": 2, "memory": "4Gi"},
            "scaling": {"min_replicas": 1, "max_replicas": 1, "concurrency": 20},
            "auth_type": "ApiKey",
        }
    ]
    assert deployment.agent_id == "ar-studio-1"
    assert deployment.instance_id == "instance-studio-1"
    assert deployment.bundle_uri == bundle_uri
    assert deployment.requires_kernel is True

    refreshed = await gateway.get_deployment_status(deployment)
    assert refreshed.status == "READY"
    assert refreshed.endpoint == "http://ar-studio-1.example.test"
    client.kernel_ready = False
    assert (await gateway.get_deployment_status(deployment)).status == "DEPLOYING"

    rolled_back = await gateway.replace_deployment(
        deployment,
        build_id="build-previous",
        version_id=version,
        bundle_digest=bundle_digest,
        request=request,
    )
    assert client.updated == [
        (
            "ar-studio-1",
            {
                "artifact_type": "Code",
                "artifact_path": bundle_uri,
                "code_checksum": archive_sha,
                "code_command": [
                    "ksadk",
                    "web",
                    "/app/code/runtime",
                    "--port",
                    "8080",
                    "--host",
                    "0.0.0.0",
                    "--no-open",
                ],
                "ks3": {
                    "access_key": "test-access",
                    "secret_key": "test-secret",
                    "region": "cn-beijing-6",
                    "bucket": "agentengine-test",
                },
            },
        )
    ]
    assert rolled_back.id != deployment.id
    assert rolled_back.agent_id == deployment.agent_id
    assert rolled_back.instance_id == deployment.instance_id


def _build_bundle(workspace: Workspace, *, revision: int, instruction: str):
    source = workspace.root / "runtime"
    source.mkdir(exist_ok=True)
    (source / "agent.py").write_text("graph = object()\n", encoding="utf-8")
    return AgentBundleBuilder(workspace).build(
        AgentDraft(
            metadata=AgentMetadata(id="studio-graph", name="Studio Graph", revision=revision),
            spec=AgentSpec(
                instructions=Instructions(system=instruction),
                model=ModelSpec(
                    model="glm-5.1",
                    endpoint_url="https://model.example.test/v1/chat/completions",
                    credential_ref="env://MODEL_API_KEY",
                ),
                runtime=RuntimeRef(
                    type="langgraph",
                    project_path="runtime",
                    entry_point="agent.py",
                    agent_variable="graph",
                ),
                security=SecuritySpec(network=NetworkPolicy(allowed_hosts=["model.example.test"])),
            ),
        )
    )


@pytest.mark.asyncio
async def test_direct_service_rolls_back_by_updating_the_existing_agent(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    previous = _build_bundle(workspace, revision=1, instruction="Previous")
    current = _build_bundle(workspace, revision=2, instruction="Current")
    client = _Client()
    gateway = DirectAgentEngineCloudDeploymentGateway(
        region="pre-online",
        client=client,
        uploader_factory=_Uploader,
        ks3_credentials={"access_key": "test-access", "secret_key": "test-secret"},
    )
    service = CloudDeploymentService(workspace, gateway=gateway)
    request = DeploymentRequest(
        target=DeploymentTarget(region="pre-online", environment="preproduction")
    )

    deployed = await service.deploy(current.id, request)
    rolled_back = await service.rollback(deployed.id, target_build_id=previous.id)

    assert len(client.created) == 1
    assert [agent_id for agent_id, _payload in client.updated] == ["ar-studio-1"]
    assert rolled_back.agent_id == deployed.agent_id
    assert rolled_back.instance_id == deployed.instance_id
    assert rolled_back.build_id == previous.id


@pytest.mark.asyncio
async def test_legacy_managed_runtime_receipt_uses_normal_runtime_readiness() -> None:
    client = _Client()
    client.kernel_ready = False
    gateway = DirectAgentEngineCloudDeploymentGateway(
        region="pre-online",
        client=client,
        uploader_factory=_Uploader,
        ks3_credentials={"access_key": "test-access", "secret_key": "test-secret"},
    )
    deployment = DeploymentRecord(
        id="dep_yaml_status",
        build_id="build_yaml_status",
        bundle_digest="sha256:" + "a" * 64,
        version_id="managed-aaaaaaaaaaaaaaaa",
        status="DEPLOYING",
        target=DeploymentTarget(region="pre-online", environment="preproduction"),
        agent_id="ar-studio-1",
        artifact_id="managed-runtime",
    )

    assert (await gateway.get_deployment_status(deployment)).status == "READY"


@pytest.mark.asyncio
async def test_new_managed_runtime_receipt_requires_kernel_readiness() -> None:
    client = _Client()
    client.kernel_ready = False
    gateway = DirectAgentEngineCloudDeploymentGateway(
        region="pre-online",
        client=client,
        uploader_factory=_Uploader,
        ks3_credentials={"access_key": "test-access", "secret_key": "test-secret"},
    )
    deployment = DeploymentRecord(
        id="dep_yaml_kernel_status",
        build_id="build_yaml_kernel_status",
        bundle_digest="sha256:" + "d" * 64,
        version_id="managed-dddddddddddddddd",
        status="DEPLOYING",
        target=DeploymentTarget(region="pre-online", environment="preproduction"),
        agent_id="ar-studio-1",
        artifact_id="managed-runtime",
        requires_kernel=True,
    )

    assert (await gateway.get_deployment_status(deployment)).status == "DEPLOYING"
    client.kernel_ready = True
    assert (await gateway.get_deployment_status(deployment)).status == "READY"


@pytest.mark.asyncio
async def test_direct_gateway_marks_deleted_cloud_agent_as_failed_receipt() -> None:
    """A deleted Agent must not strand a Studio receipt in DEPLOYING."""

    gateway = DirectAgentEngineCloudDeploymentGateway(
        region="pre-online",
        client=_MissingAgentClient(),
        uploader_factory=_Uploader,
        ks3_credentials={"access_key": "test-access", "secret_key": "test-secret"},
    )
    deployment = DeploymentRecord(
        id="dep_missing_agent",
        build_id="build_missing_agent",
        bundle_digest="sha256:" + "b" * 64,
        version_id="managed-bbbbbbbbbbbbbbbb",
        status="DEPLOYING",
        target=DeploymentTarget(region="pre-online", environment="preproduction"),
        agent_id="ar-deleted",
        artifact_id="managed-runtime",
    )

    assert (await gateway.get_deployment_status(deployment)).status == "FAILED"


@pytest.mark.asyncio
async def test_direct_gateway_creates_private_receipt_bound_dashboard_link() -> None:
    client = _Client()
    gateway = DirectAgentEngineCloudDeploymentGateway(
        region="pre-online",
        client=client,
        uploader_factory=_Uploader,
        ks3_credentials={"access_key": "test-access", "secret_key": "test-secret"},
    )
    deployment = DeploymentRecord(
        id="dep_dashboard",
        build_id="build_dashboard",
        bundle_digest="sha256:" + "c" * 64,
        version_id="managed-cccccccccccccccc",
        status="READY",
        target=DeploymentTarget(region="pre-online", environment="preproduction"),
        agent_id="ar-dashboard",
        instance_id="instance-dashboard",
        artifact_id="managed-runtime",
    )

    access = await gateway.get_deployment_dashboard_access(deployment)

    assert client.dashboard_links == [
        {
            "agent_id": "ar-dashboard",
            "link_type": "private",
            "path": "/hosted-ui/chat",
        }
    ]
    assert access == {
        "access_url": "https://dashboard.example.test/ar-dashboard",
        "agent_id": "ar-dashboard",
        "instance_id": "instance-dashboard",
        "expires_at": "2026-08-22T00:00:00Z",
    }


def test_account_agent_view_marks_native_runtime_dashboard_fallback_without_chat_capability(
) -> None:
    view = DirectAgentEngineCloudDeploymentGateway._account_agent_view(
        {
            "agent_id": "ar-native-runtime",
            "name": "A name that must not drive routing",
            "status": "running",
            "runtime_kind": "hermes",
        }
    )

    assert view["runtimeType"] == "hermes"
    assert view["chatTransport"] == "official-dashboard"
    assert view["chatRoutingReason"] == (
        "native-runtime-without-session-event-chat-capability"
    )


def test_account_agent_view_honours_declared_session_event_chat_capability() -> None:
    view = DirectAgentEngineCloudDeploymentGateway._account_agent_view(
        {
            "agent_id": "ar-capable-hermes",
            "framework": "hermes",
            "capabilities": {"session_event_chat": {"enabled": True}},
        }
    )

    assert view["chatTransport"] == "studio-session-events"
    assert view["chatRoutingReason"] == "declared-session-event-chat-capability"


def test_account_agent_view_derives_managed_version_from_runtime_manifest() -> None:
    view = DirectAgentEngineCloudDeploymentGateway._account_agent_view(
        {
            "basic": {"agent_id": "ar-managed", "status": "RUNNING"},
            "deployment": {
                "framework": "codex",
                "runtime_config": {"manifest_sha256": "a" * 64},
            },
        }
    )

    assert view["versionId"] == "managed-aaaaaaaaaaaaaaaa"


@pytest.mark.asyncio
async def test_account_native_runtime_dashboard_link_uses_official_root_path() -> None:
    class _NativeClient(_Client):
        async def get_agent(self, *, agent_id: str) -> dict:
            return {
                "basic": {
                    "agent_id": agent_id,
                    "name": "Native Agent",
                    "status": "RUNNING",
                    "framework": "openclaw",
                }
            }

    client = _NativeClient()
    gateway = DirectAgentEngineCloudDeploymentGateway(
        region="pre-online",
        client=client,
        uploader_factory=_Uploader,
        ks3_credentials={"access_key": "test-access", "secret_key": "test-secret"},
    )

    await gateway.get_account_agent_dashboard_access("ar-native-runtime")

    assert client.dashboard_links == [
        {
            "agent_id": "ar-native-runtime",
            "link_type": "private",
            "path": "/",
        }
    ]


@pytest.mark.asyncio
async def test_direct_gateway_deploys_yaml_managed_runtime_without_uploading_bundle() -> None:
    _Uploader.calls.clear()
    client = _Client()
    gateway = DirectAgentEngineCloudDeploymentGateway(
        region="pre-online",
        client=client,
        uploader_factory=_Uploader,
        ks3_credentials={"access_key": "test-access", "secret_key": "test-secret"},
    )
    request = DeploymentRequest(
        target=DeploymentTarget(region="pre-online", environment="preproduction")
    )
    deployment = await gateway.create_managed_runtime_deployment(
        build_id="build_yaml",
        agent_name="yaml-agent",
        manifest="name: yaml-agent\nframework: codex\n",
        runtime_name="codex",
        runtime_version="0.144.4",
        manifest_digest="a" * 64,
        request=request,
    )

    assert _Uploader.calls == []
    assert client.created == [
        {
            "name": "studio-yaml-agent",
            "description": "Created by AgentKit Studio",
            "framework": "codex",
            "artifact_type": "ManagedRuntime",
            "managed_runtime_config": {
                "runtime_name": "codex",
                "runtime_version": "0.144.4",
                "manifest": "name: yaml-agent\nframework: codex\n",
            },
            "region": "pre-online",
            "resources": {"cpu": 1, "memory": "2Gi"},
            "scaling": {"min_replicas": 1, "max_replicas": 1, "concurrency": 20},
            "auth_type": "ApiKey",
        }
    ]
    assert deployment.artifact_id == "managed-runtime"
    assert deployment.bundle_uri is None


@pytest.mark.asyncio
async def test_yaml_managed_runtime_cannot_replace_a_high_code_deployment(
    tmp_path: Path,
) -> None:
    """Studio may manage Code Agents, but must never redeploy them as YAML."""
    client = _Client()
    gateway = DirectAgentEngineCloudDeploymentGateway(
        region="pre-online",
        client=client,
        uploader_factory=_Uploader,
        ks3_credentials={"access_key": "test-access", "secret_key": "test-secret"},
    )
    service = CloudDeploymentService(Workspace(tmp_path), gateway=gateway)
    request = DeploymentRequest(
        target=DeploymentTarget(region="pre-online", environment="preproduction")
    )
    high_code = DeploymentRecord(
        id="dep_high_code",
        build_id="build_high_code",
        bundle_digest="sha256:" + "a" * 64,
        version_id="version-high-code",
        status="READY",
        target=request.target,
        agent_id="ar-existing-code-agent",
        artifact_id="code",
    )

    with pytest.raises(StudioError, match="不能覆盖高代码 Agent") as exc_info:
        await service.deploy_managed_runtime(
            build_id="build_yaml",
            agent_name="yaml-agent",
            manifest="name: yaml-agent\\nframework: codex\\n",
            runtime_name="codex",
            runtime_version="0.147.0",
            manifest_digest="a" * 64,
            request=request,
            replacing=high_code,
        )

    assert exc_info.value.status_code == 409
    assert client.updated == []


@pytest.mark.asyncio
async def test_cloud_service_forwards_bound_model_environment_only_to_deploy_request(
    tmp_path: Path,
) -> None:
    """YAML deployment forwards the transient model env without creating a ZIP."""

    class _Gateway:
        received: dict | None = None

        async def create_managed_runtime_deployment(self, **kwargs):
            self.received = kwargs
            return DeploymentRecord(
                id="dep_yaml",
                build_id="build_yaml",
                bundle_digest="sha256:" + "a" * 64,
                version_id="managed-aaaaaaaaaaaaaaaa",
                status="DEPLOYING",
                target=kwargs["request"].target,
                artifact_id="managed-runtime",
            )

    from ksadk.studio.cloud import CloudDeploymentService

    workspace = Workspace(tmp_path)
    gateway = _Gateway()
    service = CloudDeploymentService(workspace=workspace, gateway=gateway)
    request = DeploymentRequest(
        target=DeploymentTarget(region="pre-online", environment="preproduction")
    )

    await service.deploy_managed_runtime(
        build_id="build_yaml",
        agent_name="yaml-agent",
        manifest="name: yaml-agent\nframework: codex\n",
        runtime_name="codex",
        runtime_version="0.147.0",
        manifest_digest="a" * 64,
        request=request,
        runtime_environment={"OPENAI_MODEL_NAME": "qwen3.7-flash"},
    )

    assert gateway.received is not None
    assert gateway.received["runtime_environment"] == {"OPENAI_MODEL_NAME": "qwen3.7-flash"}


def test_managed_runtime_payload_keeps_model_env_out_of_yaml_contract() -> None:
    request = DeploymentRequest(
        target=DeploymentTarget(region="pre-online", environment="preproduction")
    )

    payload = DirectAgentEngineCloudDeploymentGateway._managed_runtime_payload(
        agent_name="yaml-agent",
        manifest="name: yaml-agent\nframework: codex\n",
        runtime_name="codex",
        runtime_version="0.147.0",
        request=request,
        runtime_environment={
            "OPENAI_API_KEY": "resolved-only-for-request",
            "OPENAI_BASE_URL": "https://model.example.com/v1",
        },
    )

    assert payload["artifact_type"] == "ManagedRuntime"
    assert "CodeConfig" not in payload
    assert payload["managed_runtime_config"] == {
        "runtime_name": "codex",
        "runtime_version": "0.147.0",
        "manifest": "name: yaml-agent\nframework: codex\n",
    }
    assert "runtime_config" not in payload
    assert payload["environment_variables"] == {
        "OPENAI_API_KEY": "resolved-only-for-request",
        "OPENAI_BASE_URL": "https://model.example.com/v1",
    }


@pytest.mark.asyncio
async def test_replacing_managed_runtime_uses_complete_declaration() -> None:
    client = _Client()
    gateway = DirectAgentEngineCloudDeploymentGateway(
        region="pre-online",
        client=client,
        uploader_factory=_Uploader,
        ks3_credentials={"access_key": "test-access", "secret_key": "test-secret"},
    )
    request = DeploymentRequest(
        target=DeploymentTarget(region="pre-online", environment="preproduction")
    )
    deployment = DeploymentRecord(
        id="dep-retry",
        build_id="build-old",
        bundle_digest="sha256:" + "a" * 64,
        version_id="managed-old",
        status="FAILED",
        target=request.target,
        agent_id="ar-studio-1",
    )

    await gateway.replace_managed_runtime_deployment(
        deployment,
        build_id="build-new",
        manifest="name: yaml-agent\nframework: codex\n",
        manifest_digest="b" * 64,
        runtime_name="codex",
        runtime_version="0.147.0",
        request=request,
    )

    assert client.updated == [
        (
            "ar-studio-1",
            {
                "artifact_type": "ManagedRuntime",
                "managed_runtime_config": {
                    "runtime_name": "codex",
                    "runtime_version": "0.147.0",
                    "manifest": "name: yaml-agent\nframework: codex\n",
                },
            },
        )
    ]


@pytest.mark.asyncio
async def test_cloud_chat_is_bound_to_the_deployment_receipt_agent() -> None:
    client = _Client()
    gateway = DirectAgentEngineCloudDeploymentGateway(
        region="pre-online",
        client=client,
        uploader_factory=_Uploader,
        ks3_credentials={"access_key": "test-access", "secret_key": "test-secret"},
    )
    request = DeploymentRequest(
        target=DeploymentTarget(region="pre-online", environment="preproduction")
    )
    deployment = DeploymentRecord(
        id="dep_cloud_chat",
        build_id="build_cloud_chat",
        bundle_digest="sha256:" + "a" * 64,
        version_id="version-cloud-chat",
        status="READY",
        target=request.target,
        agent_id="ar-receipt-bound",
    )

    assert await gateway.list_deployment_chat_sessions(deployment) == {
        "sessions": [{"id": "sess-cloud"}],
        "total": 1,
    }
    assert await gateway.create_deployment_chat_session(deployment) == {
        "session": {"id": "sess-new"}
    }
    assert await gateway.list_deployment_chat_messages(
        deployment, session_id="sess-cloud", after_seq_id=4
    ) == {
        "messages": [{"role": "assistant", "content": "云端回复"}],
        "latest_seq_id": 4,
    }
    assert await gateway.delete_deployment_chat_session(deployment, session_id="sess-cloud") is True
    assert await gateway.send_deployment_chat_message(
        deployment,
        session_id="sess-cloud",
        content="你好",
        model="qwen3-coder-plus",
        tool_approval_mode="risk",
    ) == {"receipt_status": "accepted", "run_id": "run-cloud"}
    assert await gateway.list_deployment_chat_models(deployment) == {
        "models": [{"id": "qwen3-coder-plus"}],
        "current": "qwen3-coder-plus",
    }
    assert client.session_calls == [
        ("ListSessions", {"AgentId": "ar-receipt-bound", "Page": 1, "PageSize": 50}),
        ("CreateSession", {"AgentId": "ar-receipt-bound"}),
        (
            "ListSessionMessages",
            {
                "agent_id": "ar-receipt-bound",
                "session_id": "sess-cloud",
                "after_seq_id": 4,
                "limit": 100,
            },
        ),
        ("DeleteSession", {"SessionId": "sess-cloud"}),
        (
            "RunAgent",
            {
                "AgentId": "ar-receipt-bound",
                "SessionId": "sess-cloud",
                "Message": "你好",
                "Model": "qwen3-coder-plus",
                "ModelOptions": None,
                "ToolApprovalMode": "risk",
            },
        ),
        ("ListAgentModels", {"AgentId": "ar-receipt-bound"}),
    ]


@pytest.mark.asyncio
async def test_cloud_chat_poll_after_delete_returns_empty_terminal_views() -> None:
    gateway = DirectAgentEngineCloudDeploymentGateway(
        region="pre-online",
        client=_DeletedSessionClient(),
        uploader_factory=_Uploader,
        ks3_credentials={"access_key": "test-access", "secret_key": "test-secret"},
    )
    deployment = DeploymentRecord(
        id="dep_deleted_session",
        build_id="build_deleted_session",
        bundle_digest="sha256:" + "a" * 64,
        version_id="version-deleted-session",
        status="READY",
        target=DeploymentTarget(region="pre-online", environment="preproduction"),
        agent_id="ar-receipt-bound",
    )

    assert await gateway.list_deployment_chat_messages(
        deployment, session_id="sess-deleted"
    ) == {"messages": [], "session_deleted": True}
    assert await gateway.list_deployment_chat_events(
        deployment, session_id="sess-deleted"
    ) == {"events": [], "session_deleted": True}


@pytest.mark.asyncio
async def test_cloud_chat_rejects_receipts_without_an_agent_id() -> None:
    gateway = DirectAgentEngineCloudDeploymentGateway(
        region="pre-online",
        client=_Client(),
        uploader_factory=_Uploader,
        ks3_credentials={"access_key": "test-access", "secret_key": "test-secret"},
    )
    deployment = DeploymentRecord(
        id="dep_missing_agent",
        build_id="build_missing_agent",
        bundle_digest="sha256:" + "a" * 64,
        version_id="version-missing-agent",
        status="READY",
        target=DeploymentTarget(region="pre-online", environment="preproduction"),
    )

    with pytest.raises(StudioError, match="缺少云端 Agent 标识") as exc_info:
        await gateway.list_deployment_chat_sessions(deployment)

    assert exc_info.value.status_code == 409
