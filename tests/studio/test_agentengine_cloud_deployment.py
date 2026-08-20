"""Studio deploys an admitted Bundle through the existing Agent creation Action."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from ksadk.studio.builder import AgentBundleBuilder
from ksadk.studio.cloud import AgentEngineCloudDeploymentGateway, CloudDeploymentService
from ksadk.studio.contracts import (
    AgentDraft,
    AgentMetadata,
    AgentSpec,
    DeploymentRequest,
    DeploymentTarget,
    Instructions,
    ModelSpec,
    NetworkPolicy,
    RuntimeRef,
    SecuritySpec,
)
from ksadk.studio.workspace import Workspace


@pytest.mark.asyncio
async def test_agentengine_gateway_admits_then_reuses_create_agent_product() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/ListAgentRuntimeProfiles"):
            return httpx.Response(
                200,
                json={"Code": 0, "Data": {"RuntimeProfiles": [{
                    "RuntimeProfileId": "langgraph-v1", "RuntimeFamily": "langgraph"
                }]}},
            )
        if request.url.path.endswith("/CreateAgentArtifact"):
            assert b"SourceArchive" in request.content
            return httpx.Response(
                200,
                json={"Code": 0, "Data": {"AgentArtifactId": "aa-1", "RuntimeFamily": "langgraph"}},
            )
        if request.url.path.endswith("/CreateAgentProduct"):
            body = json.loads(request.content)
            assert body["AgentArtifactId"] == "aa-1"
            assert "CodeConfig" not in body
            return httpx.Response(
                200,
                json={"Code": 0, "Data": {"AgentId": "ar-1", "InstanceId": "instance-1"}},
            )
        if request.url.path.endswith("/FetchAgentInstanceStatus"):
            return httpx.Response(200, json={"Code": 0, "Data": {"Status": "RUNNING"}})
        raise AssertionError(request.url.path)

    gateway = AgentEngineCloudDeploymentGateway(
        base_url="https://gateway.example.test",
        control_plane_token="short-lived-user-token",
        account_id="account-1",
        region="cn-beijing-6",
        transport=httpx.MockTransport(handler),
    )
    bundle = b"bundle-bytes"
    digest = f"sha256:{hashlib.sha256(bundle).hexdigest()}"
    artifact_ref = await gateway.upload_bundle(
        bundle=bundle,
        bundle_digest=digest,
        provenance={"agentId": "demo-agent", "sourceRevision": 3, "runtimeType": "langgraph"},
    )
    artifact_id = await gateway.create_version(
        agent_id="demo-agent",
        bundle_uri=artifact_ref,
        bundle_digest=digest,
        provenance={},
    )
    deployment = await gateway.create_deployment(
        build_id="build-1",
        version_id=artifact_id,
        bundle_digest=digest,
        request=DeploymentRequest(target=DeploymentTarget(region="cn-beijing-6", environment="development")),
    )
    ready = await gateway.get_deployment_status(deployment)

    assert deployment.instance_id == "instance-1"
    assert ready.status == "READY"
    assert [item.url.path.rsplit("/", 1)[-1] for item in seen] == [
        "ListAgentRuntimeProfiles",
        "CreateAgentArtifact",
        "CreateAgentProduct",
        "FetchAgentInstanceStatus",
    ]


@pytest.mark.asyncio
async def test_cloud_service_submits_the_built_bundle_runtime_family(tmp_path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    source = workspace.root / "runtime"
    source.mkdir()
    (source / "agent.py").write_text("graph = object()\n", encoding="utf-8")
    build = AgentBundleBuilder(workspace).build(
        AgentDraft(
            metadata=AgentMetadata(id="studio-graph", name="Studio Graph"),
            spec=AgentSpec(
                instructions=Instructions(system="Be concise", task="Reply OK"),
                model=ModelSpec(
                    model="glm-5.1",
                    endpoint_url="https://model.example.com/v1/chat/completions",
                    credential_ref="env://MODEL_API_KEY",
                ),
                runtime=RuntimeRef(
                    type="langgraph",
                    project_path="runtime",
                    entry_point="agent.py",
                    agent_variable="graph",
                ),
                security=SecuritySpec(network=NetworkPolicy(allowed_hosts=["model.example.com"])),
            ),
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ListAgentRuntimeProfiles"):
            return httpx.Response(200, json={"Code": 0, "Data": {"RuntimeProfiles": [{
                "RuntimeProfileId": "langgraph-v1", "RuntimeFamily": "langgraph"
            }]}})
        if request.url.path.endswith("/CreateAgentArtifact"):
            return httpx.Response(200, json={"Code": 0, "Data": {
                "AgentArtifactId": "aa-graph", "RuntimeFamily": "langgraph"
            }})
        if request.url.path.endswith("/CreateAgentProduct"):
            return httpx.Response(200, json={"Code": 0, "Data": {
                "AgentId": "ar-graph", "InstanceId": "instance-graph"
            }})
        raise AssertionError(request.url.path)

    gateway = AgentEngineCloudDeploymentGateway(
        base_url="https://gateway.example.test",
        control_plane_token="short-lived-user-token",
        account_id="account-1",
        region="cn-beijing-6",
        transport=httpx.MockTransport(handler),
    )
    result = await CloudDeploymentService(workspace, gateway=gateway).deploy(
        build.id,
        DeploymentRequest(target=DeploymentTarget(region="cn-beijing-6", environment="development")),
    )

    assert result.instance_id == "instance-graph"
