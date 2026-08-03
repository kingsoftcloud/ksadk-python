from __future__ import annotations

from pathlib import Path

import pytest

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
    RunEvent,
    RunRecord,
    RunStatus,
    SecuritySpec,
    Usage,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.evaluation import EvaluationRunner
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
            security=SecuritySpec(
                network=NetworkPolicy(allowed_hosts=["model.example.com"])
            ),
        ),
    )
    return workspace, AgentBundleBuilder(workspace).build(draft)


class FakeEventStore:
    def events(self, run_id):
        if run_id == "run_tool":
            return [
                RunEvent(
                    id=1,
                    run_id=run_id,
                    type="tool.requested",
                    data={"tool": "builtin.echo"},
                )
            ]
        return []


class FakeRuntime:
    def __init__(self):
        self.event_store = FakeEventStore()
        self.calls = 0

    async def run(self, build_id, user_input, *, session_id):
        self.calls += 1
        output = "AGENTKIT_OK" if "ok" in user_input.lower() else '{"ok":true}'
        return RunRecord(
            id="run_tool" if self.calls == 1 else f"run_{self.calls}",
            build_id=build_id,
            agent_id="demo-agent",
            session_id=session_id,
            trace_id=f"trace_{self.calls}",
            status=RunStatus.COMPLETED,
            input=user_input,
            output=output,
            usage=Usage(input_tokens=3, output_tokens=2, total_tokens=5),
            duration_ms=10,
        )


@pytest.mark.asyncio
async def test_evaluation_runs_assertions_and_persists_result(tmp_path: Path):
    workspace, build = _workspace_and_build(tmp_path)
    suite = workspace.root / "agents/demo-agent/evaluations/smoke.yaml"
    suite.parent.mkdir(parents=True, exist_ok=True)
    suite.write_text(
        """
apiVersion: agentkit.ksyun.com/v1alpha1
kind: EvaluationSuite
metadata:
  name: smoke
cases:
  - id: answer
    input: say ok
    assertions:
      - type: contains
        value: AGENTKIT_OK
      - type: maxLatencyMs
        value: 100
      - type: toolCalled
        value: builtin.echo
  - id: json
    input: return json
    assertions:
      - type: jsonSchema
        value:
          type: object
          required: [ok]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    runner = EvaluationRunner(workspace, runtime=FakeRuntime())

    result = await runner.run(build.id, ["evaluations/smoke.yaml"])

    assert result.status == "COMPLETED"
    assert result.total == 2
    assert result.passed == 2
    assert result.pass_rate == 1
    assert runner.get(result.id) == result


@pytest.mark.asyncio
async def test_evaluation_reports_failed_assertion(tmp_path: Path):
    workspace, build = _workspace_and_build(tmp_path)
    suite = workspace.root / "agents/demo-agent/evaluations/fail.yaml"
    suite.parent.mkdir(parents=True, exist_ok=True)
    suite.write_text(
        """
apiVersion: agentkit.ksyun.com/v1alpha1
kind: EvaluationSuite
metadata:
  name: fail
cases:
  - id: wrong
    input: say ok
    assertions:
      - type: equals
        value: OTHER
""".strip()
        + "\n",
        encoding="utf-8",
    )

    result = await EvaluationRunner(workspace, runtime=FakeRuntime()).run(
        build.id,
        ["evaluations/fail.yaml"],
    )

    assert result.status == "FAILED"
    assert result.failed == 1
    assert not result.results[0].assertions[0].passed


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
async def test_unconfigured_cloud_admission_fails_explicitly(tmp_path: Path):
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

    assert captured.value.code == "CLOUD_BUNDLE_ADMISSION_UNAVAILABLE"


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
            security=SecuritySpec(
                network=NetworkPolicy(allowed_hosts=["model.example.com"])
            ),
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
