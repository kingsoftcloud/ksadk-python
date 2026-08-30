from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ksadk.evaluation import (
    EvalCase,
    EvalRunReport,
    EvalRunSpec,
    EvalSetVersion,
    EvaluationConfig,
    TargetKind,
    TargetRef,
    TargetSnapshot,
)
from ksadk.studio.api import create_studio_app
from ksadk.studio.contracts import BuildRecord, BuildStatus
from ksadk.studio.errors import StudioError
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
    assert completed.resource_id == report.spec.id
    assert [event.type for event in service.operations.events(operation.id)] == [
        "operation.queued",
        "operation.started",
        "evaluation.case.started",
        "operation.succeeded",
    ]
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
        run_id = response.json()["resourceId"]
        run = client.get(f"/api/v1/evaluation-runs/{run_id}")
        assert run.status_code == 200, run.text
        assert run.json()["id"] == run_id
        assert run.json()["evalset"] == {"name": "smoke", "caseCount": 1}
        assert run.json()["target"] == {"kind": "a2a", "label": "A2A Agent"}
        assert run.json()["hasReport"] is False
        assert client.get("/api/v1/evaluation-runs").json()["items"][0]["id"] == run_id
        assert client.get("/api/v1/evaluations").json() == {"items": []}
        assert client.post(
            "/api/v1/builds/build-old/evaluations",
            headers={"Idempotency-Key": "obsolete-evaluation-api"},
            json={"suiteRefs": ["smoke.yaml"]},
        ).status_code == 404
        missing = client.get("/api/v1/evaluations/eval_missing")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "EVALUATION_NOT_FOUND"


def test_evaluation_catalog_works_after_local_session_bootstrap(tmp_path: Path):
    """The production Studio shell reads these APIs after the root page sets its cookie."""

    app = create_studio_app(
        tmp_path,
        session_token="evaluation-local-session",
        csrf_token="evaluation-local-csrf",
    )
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        runs = client.get("/api/v1/evaluation-runs")
        catalog = client.get("/api/v1/evaluation-targets")

    assert runs.status_code == 200, runs.text
    assert runs.json() == {"items": []}
    assert catalog.status_code == 200, catalog.text
    assert catalog.json() == {"builds": [], "evalsets": []}


@pytest.mark.asyncio
async def test_evaluation_run_backfills_legacy_metadata_from_report(tmp_path: Path):
    service = StudioService(tmp_path)
    service.drafts.create(agent_id="legacy-agent", name="Legacy Agent")
    report = EvalRunReport(
        spec=EvalRunSpec(
            id="eval-legacy",
            evalset=EvalSetVersion(
                name="legacy-smoke",
                cases=[EvalCase(id="one", input="hello")],
            ),
            target=TargetSnapshot(
                kind=TargetKind.STUDIO_BUILD,
                entrypoint="build:build-legacy",
                revision_digest="sha256:legacy",
                runtime="langgraph",
                metadata={"agentId": "legacy-agent"},
            ),
            config=EvaluationConfig(evaluators=["runtime_budget@v1"]),
        ),
        status="PASSED",
    )
    service.evaluation_storage.write_report(report)
    operation = service.operations.submit(
        kind="EVALUATION",
        resource_id=report.spec.id,
        idempotency_key="legacy-evaluation",
        runner=lambda _operation_id: __import__("asyncio").sleep(0),
    )
    await service.operations.wait(operation.id)

    run = service.get_public_evaluation_run(report.spec.id)

    assert run["evalset"] == {"name": "legacy-smoke", "caseCount": 1}
    assert run["target"] == {"kind": "studio_build", "label": "Legacy Agent"}
    assert run["evaluators"] == ["runtime_budget@v1"]


@pytest.mark.asyncio
async def test_operation_events_support_json_progress_reading(tmp_path: Path):
    service = StudioService(tmp_path)
    operation = service.operations.submit(
        kind="EVALUATION",
        resource_id="eval-progress",
        idempotency_key="evaluation-progress-events",
        runner=lambda _operation_id: __import__("asyncio").sleep(0),
    )
    service.operations.append(
        operation.id,
        "evaluation.case.started",
        {"caseId": "case-1", "index": 1, "total": 2},
    )
    await service.operations.wait(operation.id)
    app = create_studio_app(tmp_path, service=service, security_enabled=False)
    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/operations/{operation.id}/events?after=0",
            headers={"Accept": "application/json"},
        )

    assert response.status_code == 200
    event = next(
        item
        for item in response.json()["items"]
        if item["type"] == "evaluation.case.started"
    )
    assert event["data"] == {"caseId": "case-1", "index": 1, "total": 2}


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


def test_evaluation_file_upload_imports_valid_evalset_into_workspace(tmp_path: Path):
    app = create_studio_app(tmp_path, security_enabled=False)
    content = b"""schemaVersion: ksadk.eval/v1
name: uploaded-smoke
cases:
  - id: one
    input: hello
"""

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/evaluation-files",
            files={"file": ("smoke.yaml", content, "application/yaml")},
        )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["path"].startswith("evaluations/uploads/")
    assert payload["path"].endswith("-smoke.yaml")
    assert payload["name"] == "uploaded-smoke"
    assert payload["caseCount"] == 1
    assert (tmp_path / payload["path"]).read_bytes() == content


def test_evaluation_file_upload_rejects_invalid_evalset(tmp_path: Path):
    app = create_studio_app(tmp_path, security_enabled=False)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/evaluation-files",
            files={"file": ("broken.yaml", b"not: an evalset\n", "application/yaml")},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSUPPORTED_EVALSET_FORMAT"
    assert list((tmp_path / "evaluations" / "uploads").glob("*")) == []


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


def test_evaluation_catalog_does_not_duplicate_sha256_prefix(tmp_path: Path):
    service = StudioService(tmp_path)
    service.builds.save(
        BuildRecord(
            id="build_prefixed",
            agentId="demo-agent",
            sourceRevision=1,
            status=BuildStatus.SUCCEEDED,
            runtimeType="langgraph",
            bundleDigest="sha256:abc123",
            artifactPath=".agentkit/artifacts/build_prefixed/bundle.zip",
            createdAt="2026-08-12T00:00:00Z",
        )
    )

    assert service.evaluation_catalog()["builds"][0]["digest"] == "sha256:abc123"


def test_evaluation_catalog_does_not_expose_mutable_codex_builds(tmp_path: Path):
    service = StudioService(tmp_path)
    codex_build = type(
        "CodexBuild",
        (),
        {
            "id": "build_codex",
            "agent_name": "review-agent",
            "runtime_name": "codex",
            "manifest_sha256": "a" * 64,
            "created_at": datetime.now(timezone.utc),
            "status": "SUCCEEDED",
            "artifact_path": ".agentkit/artifacts/build_codex.zip",
        },
    )()
    service.codex_builds.list = lambda: [codex_build]

    assert service.evaluation_catalog()["builds"] == []
    service.codex_builds.get = lambda _build_id: codex_build
    with pytest.raises(StudioError) as rejected:
        service._normalize_public_evaluation_target(
            TargetRef(kind=TargetKind.STUDIO_BUILD, locator="build_codex")
        )
    assert rejected.value.code == "CODEX_BUILD_NOT_IMMUTABLE"
    assert rejected.value.status_code == 422
