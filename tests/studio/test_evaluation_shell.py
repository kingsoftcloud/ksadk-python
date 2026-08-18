from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ksadk.evaluation import (
    CloudEvalSetCatalogItem,
    EvaluationConfig,
    TargetKind,
    TargetRef,
)
from ksadk.studio.api import create_studio_app
from ksadk.studio.contracts import BuildRecord, BuildStatus
from ksadk.studio.errors import StudioError
from ksadk.studio.service import StudioService


class _CloudEvalClient:
    async def list_datasets(self, *, project_id=None):
        return [
            CloudEvalSetCatalogItem(
                dataset_id="dataset-1",
                name="support",
                project_id=project_id,
                version=4,
                schema_hash="a" * 64,
                content_digest="b" * 64,
                row_count=2,
            )
        ]

    async def read_snapshot(self, dataset_id, version, *, project_id=None):
        raise AssertionError("catalog test must not read a snapshot")

    async def publish_snapshot(self, *args, **kwargs):
        raise AssertionError("catalog test must not publish")


def test_studio_constructs_cloud_eval_client_without_direct_url(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("AGENT_EVAL_BASE_URL", raising=False)

    service = StudioService(tmp_path)

    assert service.cloud_evalsets is not None


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
        assert client.get("/api/v1/evaluations").json() == {"items": []}
        assert client.post(
            "/api/v1/builds/build-old/evaluations",
            headers={"Idempotency-Key": "obsolete-evaluation-api"},
            json={"suiteRefs": ["smoke.yaml"]},
        ).status_code == 404
        missing = client.get("/api/v1/evaluations/eval_missing")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "EVALUATION_NOT_FOUND"


def test_studio_cloud_catalog_exposes_immutable_dataset_versions(tmp_path: Path):
    service = StudioService(tmp_path, cloud_evalset_client=_CloudEvalClient())
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/evaluation-cloud/catalog",
            params={"projectId": "project-1"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["datasetId"] == "dataset-1"
    assert response.json()["items"][0]["version"] == 4


def test_studio_evaluation_contract_accepts_a_cloud_dataset_source():
    from ksadk.evaluation import CloudDatasetRef
    from ksadk.studio.api_contracts import StudioEvaluationCreate

    payload = StudioEvaluationCreate(
        cloud_dataset=CloudDatasetRef(
            provider="agent-eval/evalsmith",
            dataset_id="dataset-1",
            version=4,
            schema_hash="a" * 64,
            content_digest="b" * 64,
        ),
        target=TargetRef(kind=TargetKind.A2A, locator="https://agent.example.test"),
    )

    assert payload.evalset_file is None
    assert payload.cloud_dataset.dataset_id == "dataset-1"


@pytest.mark.asyncio
async def test_studio_remote_dataset_operation_resolves_fixed_snapshot(tmp_path: Path, monkeypatch):
    from ksadk.evaluation import CloudDatasetRef
    from ksadk.evaluation.cloud_converter import evalset_to_dataset_snapshot
    from ksadk.evaluation.evalset import parse_evalset

    evalset = parse_evalset(
        {
            "schemaVersion": "ksadk.eval/v1",
            "name": "remote",
            "cases": [{"id": "one", "input": "hello"}],
        }
    )
    snapshot = evalset_to_dataset_snapshot(evalset)

    class Client(_CloudEvalClient):
        async def read_snapshot(self, dataset_id, version, *, project_id=None):
            assert (dataset_id, version, project_id) == ("dataset-1", 4, None)
            return snapshot

    captured = {}

    async def fake_execute(request, **kwargs):
        captured["request"] = request
        from ksadk.evaluation import EvalRunReport, EvalRunSpec, TargetSnapshot

        return EvalRunReport(
            spec=EvalRunSpec(
                id=kwargs["run_id"],
                evalset=request.evalset,
                target=TargetSnapshot(
                    kind=TargetKind.A2A,
                    entrypoint=request.target.locator,
                    revision_digest="sha256:test",
                ),
                config=request.config,
                cloud_dataset=request.cloud_dataset,
            ),
            status="PASSED",
        )

    monkeypatch.setattr("ksadk.studio.service.execute_evaluation", fake_execute)
    service = StudioService(tmp_path, cloud_evalset_client=Client())
    operation = service.submit_public_evaluation(
        None,
        TargetRef(kind=TargetKind.A2A, locator="https://agent.example.test"),
        EvaluationConfig(),
        idempotency_key="remote-evaluation-1",
        cloud_dataset=CloudDatasetRef(
            provider="agent-eval/evalsmith",
            dataset_id="dataset-1",
            version=4,
            schema_hash=snapshot.schema_hash,
            content_digest=snapshot.content_digest,
        ),
    )
    completed = await service.operations.wait(operation.id)

    assert completed.status == "SUCCEEDED", completed.error
    assert captured["request"].cloud_dataset.dataset_id == "dataset-1"


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
