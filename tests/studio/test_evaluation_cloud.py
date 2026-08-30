from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ksadk.studio.api import create_studio_app
from ksadk.studio.builder import AgentBundleBuilder
from ksadk.studio.cloud import (
    CloudDeploymentService,
    InMemoryCloudGateway,
    UnavailableCloudGateway,
)
from ksadk.studio.contracts import (
    AgentDraft,
    AgentMetadata,
    AgentSpec,
    DeploymentRequest,
    DeploymentTarget,
    Instructions,
    ModelSpec,
    NetworkPolicy,
    SecuritySpec,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.service import StudioService
from ksadk.studio.workspace import Workspace


def _workspace_and_build(tmp_path: Path):
    workspace = Workspace(tmp_path)
    workspace.initialize()
    draft = AgentDraft(
        metadata=AgentMetadata(id="demo-agent", name="Demo"),
        spec=AgentSpec(
            instructions=Instructions(system="Only say OK"),
            model=ModelSpec(
                model="glm-5.1",
                endpoint_url="https://model.example.com/v1/chat/completions",
                credential_ref="env://MODEL_API_KEY",
            ),
            security=SecuritySpec(network=NetworkPolicy(allowed_hosts=["model.example.com"])),
        ),
    )
    return workspace, AgentBundleBuilder(workspace).build(draft)


@pytest.mark.asyncio
async def test_cloud_deploy_uploads_exact_local_bundle_digest(tmp_path: Path):
    workspace, build = _workspace_and_build(tmp_path)
    gateway = InMemoryCloudGateway()
    service = CloudDeploymentService(workspace, gateway=gateway)
    request = DeploymentRequest(
        target=DeploymentTarget(
            region="cn-beijing-6",
            environment="development",
        )
    )

    deployment = await service.deploy(build.id, request)

    assert deployment.status == "READY"
    assert deployment.bundle_digest == build.bundle_digest
    assert gateway.uploads[0]["bundle_digest"] == build.bundle_digest
    assert gateway.versions[0]["bundle_digest"] == build.bundle_digest
    assert "archiveSha256" in gateway.versions[0]["provenance"]
    assert "build" not in gateway.versions[0]


@pytest.mark.asyncio
async def test_unconfigured_cloud_deployment_fails_explicitly(tmp_path: Path):
    workspace, build = _workspace_and_build(tmp_path)
    service = CloudDeploymentService(
        workspace,
        gateway=UnavailableCloudGateway(),
    )

    with pytest.raises(StudioError) as captured:
        await service.deploy(
            build.id,
            DeploymentRequest(
                target=DeploymentTarget(
                    region="cn-beijing-6",
                    environment="development",
                )
            ),
        )

    assert captured.value.code == "CLOUD_BUNDLE_DEPLOYMENT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_cloud_rollback_redeploys_historical_immutable_build(tmp_path: Path):
    workspace, first = _workspace_and_build(tmp_path)
    second_draft = AgentDraft(
        metadata=AgentMetadata(
            id="demo-agent",
            name="Demo",
            revision=2,
        ),
        spec=AgentSpec(
            instructions=Instructions(system="Changed instruction"),
            model=ModelSpec(
                model="glm-5.1",
                endpoint_url="https://model.example.com/v1/chat/completions",
                credential_ref="env://MODEL_API_KEY",
            ),
            security=SecuritySpec(network=NetworkPolicy(allowed_hosts=["model.example.com"])),
        ),
    )
    second = AgentBundleBuilder(workspace).build(second_draft)
    gateway = InMemoryCloudGateway()
    service = CloudDeploymentService(workspace, gateway=gateway)
    request = DeploymentRequest(
        target=DeploymentTarget(
            region="cn-beijing-6",
            environment="development",
        )
    )
    deployed = await service.deploy(second.id, request)

    rolled_back = await service.rollback(
        deployed.id,
        target_build_id=first.id,
    )

    assert rolled_back.build_id == first.id
    assert rolled_back.bundle_digest == first.bundle_digest
    assert rolled_back.bundle_digest != second.bundle_digest


@pytest.mark.asyncio
async def test_studio_service_rejects_cross_agent_high_code_rollback(tmp_path: Path):
    """The API service must not trust a browser-supplied target Build id."""

    workspace, deployed_build = _workspace_and_build(tmp_path)
    other_build = AgentBundleBuilder(workspace).build(
        AgentDraft(
            metadata=AgentMetadata(id="other-agent", name="Other Agent"),
            spec=AgentSpec(
                instructions=Instructions(system="Only say OTHER"),
                model=ModelSpec(
                    model="glm-5.1",
                    endpoint_url="https://model.example.com/v1/chat/completions",
                    credential_ref="env://MODEL_API_KEY",
                ),
                security=SecuritySpec(network=NetworkPolicy(allowed_hosts=["model.example.com"])),
            ),
        )
    )
    gateway = InMemoryCloudGateway()
    studio = StudioService(tmp_path, cloud_gateway=gateway)
    request = DeploymentRequest(
        target=DeploymentTarget(
            region="cn-beijing-6",
            environment="development",
        )
    )
    deployment = await studio.cloud.deploy(deployed_build.id, request)

    app = create_studio_app(tmp_path, service=studio, security_enabled=False)
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/deployments/{deployment.id}:rollback",
            headers={"Idempotency-Key": "cross-agent-rollback"},
            json={"targetBuildId": other_build.id},
        )

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "DEPLOYMENT_ROLLBACK_AGENT_MISMATCH"
    assert error["message"] == "高代码 Agent 只能回滚到同一 Agent 的 Build"
    assert error["details"] == {
        "deploymentId": deployment.id,
        "targetBuildId": other_build.id,
    }
    assert error["requestId"].startswith("req_")
    assert len(gateway.deployments) == 1
