"""Studio cloud delivery stays on the existing signed Code deployment path."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

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
from ksadk.studio.workspace import Workspace


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
            "deployment": {"agent_kernel_ready": self.kernel_ready},
        }


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

    assert _Uploader.calls == [
        (bundle, f"studio-bundles/studio-graph/{archive_sha}/bundle.zip")
    ]
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

    assert (await gateway.get_deployment_status(deployment)).status == "READY"
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
                security=SecuritySpec(
                    network=NetworkPolicy(allowed_hosts=["model.example.test"])
                ),
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
async def test_managed_runtime_status_uses_normal_runtime_readiness_not_kernel() -> None:
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
            "runtime_config": {
                "name": "codex",
                "version": "0.144.4",
                "manifest": "name: yaml-agent\nframework: codex\n",
            },
            "region": "pre-online",
            "resources": {"cpu": 2, "memory": "4Gi"},
            "scaling": {"min_replicas": 1, "max_replicas": 1, "concurrency": 20},
            "auth_type": "ApiKey",
        }
    ]
    assert deployment.artifact_id == "managed-runtime"
    assert deployment.bundle_uri is None


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
    assert gateway.received["runtime_environment"] == {
        "OPENAI_MODEL_NAME": "qwen3.7-flash"
    }


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
    assert payload["runtime_config"] == {
        "name": "codex",
        "version": "0.147.0",
        "manifest": "name: yaml-agent\nframework: codex\n",
    }
    assert payload["environment_variables"] == {
        "OPENAI_API_KEY": "resolved-only-for-request",
        "OPENAI_BASE_URL": "https://model.example.com/v1",
    }
