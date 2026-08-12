from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ksadk.evaluation import EvaluationConfig, TargetKind, TargetRef
from ksadk.studio.api import create_studio_app
from ksadk.studio.contracts import BuildRecord, BuildStatus
from ksadk.studio.service import StudioService


@pytest.mark.asyncio
async def test_studio_public_evaluation_shell_uses_shared_executor(tmp_path: Path):
    (tmp_path / "smoke.yaml").write_text(
        """schemaVersion: ksadk.eval/v1
name: smoke
cases:
  - id: one
    input: hello
""",
        encoding="utf-8",
    )
    agent_dir = tmp_path / "local-agent"
    agent_dir.mkdir()
    (agent_dir / "agentengine.yaml").write_text(
        "name: studio-local\nframework: langgraph\nentry_point: agent.py\nagent_variable: graph\n",
        encoding="utf-8",
    )
    (agent_dir / "agent.py").write_text(
        "\n".join(
            (
                "from langgraph.graph import END, START, StateGraph",
                "def answer(_state):",
                "    return {'output': 'studio local answer'}",
                "builder = StateGraph(dict)",
                "builder.add_node('answer', answer)",
                "builder.add_edge(START, 'answer')",
                "builder.add_edge('answer', END)",
                "graph = builder.compile()",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    service = StudioService(tmp_path)

    operation = service.submit_public_evaluation(
        "smoke.yaml",
        TargetRef(kind=TargetKind.LOCAL_SOURCE, locator=str(agent_dir)),
        EvaluationConfig(),
        idempotency_key="evaluation-shell-1",
    )
    completed = await service.operations.wait(operation.id)

    assert completed.status == "SUCCEEDED", completed.error
    report = service.list_public_evaluations()[0]
    assert report.case_runs[0].target_run.output == "studio local answer"


def test_studio_public_evaluation_http_shell(tmp_path: Path):
    (tmp_path / "smoke.yaml").write_text(
        """schemaVersion: ksadk.eval/v1
name: smoke
cases:
  - id: one
    input: hello
""",
        encoding="utf-8",
    )
    app = create_studio_app(tmp_path, security_enabled=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/evaluations",
            headers={"Idempotency-Key": "evaluation-http-shell-1"},
            json={
                "evalsetFile": "smoke.yaml",
                "target": {
                    "kind": "a2a",
                    "locator": "https://agent.example.invalid/card",
                },
            },
        )
        assert response.status_code == 202, response.text
        assert response.json()["kind"] == "EVALUATION"
        assert client.get("/api/v1/evaluations").json() == {"items": []}


def test_evaluation_catalog_lists_successful_builds_and_valid_evalsets(tmp_path: Path):
    (tmp_path / "evaluations").mkdir()
    (tmp_path / "evaluations" / "smoke.yaml").write_text(
        """schemaVersion: ksadk.eval/v1
name: smoke
cases:
  - id: one
    input: hello
""",
        encoding="utf-8",
    )
    (tmp_path / "evaluations" / "broken.yaml").write_text("not: an evalset\n", encoding="utf-8")
    app = create_studio_app(tmp_path, security_enabled=False)
    with TestClient(app) as client:
        catalog = client.get("/api/v1/evaluation-targets")
        assert catalog.status_code == 200, catalog.text
        payload = catalog.json()
        assert payload["builds"] == []
        assert payload["evalsets"] == [
            {
                "path": "evaluations/smoke.yaml",
                "name": "smoke",
                "caseCount": 1,
                "contentDigest": payload["evalsets"][0]["contentDigest"],
            }
        ]


def test_evaluation_catalog_exposes_studio_build_metadata(tmp_path: Path):
    service = StudioService(tmp_path)
    service.builds.save(
        BuildRecord(
            id="build_demo",
            agentId="demo-agent",
            sourceRevision=1,
            status=BuildStatus.SUCCEEDED,
            runtimeType="langgraph",
            bundleDigest="abc123",
            artifactPath=".agentkit/artifacts/build_demo/bundle.zip",
            createdAt="2026-08-12T00:00:00Z",
        )
    )
    app = create_studio_app(tmp_path, security_enabled=False)
    with TestClient(app) as client:
        builds = client.get("/api/v1/evaluation-targets").json()["builds"]
        assert builds == [
            {
                "id": "build_demo",
                "agentId": "demo-agent",
                "runtime": "langgraph",
                "digest": "sha256:abc123",
                "createdAt": "2026-08-12T00:00:00Z",
            }
        ]
