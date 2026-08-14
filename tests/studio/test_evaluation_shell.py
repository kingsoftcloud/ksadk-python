from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ksadk.evaluation import EvaluationConfig, TargetKind, TargetRef
from ksadk.studio.api import create_studio_app
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
    service = StudioService(tmp_path)

    operation = service.submit_public_evaluation(
        "smoke.yaml",
        TargetRef(kind=TargetKind.LOCAL_SOURCE, locator=str(tmp_path)),
        EvaluationConfig(),
        idempotency_key="evaluation-shell-1",
    )
    completed = await service.operations.wait(operation.id)

    assert completed.status == "FAILED"
    assert completed.error["code"] == "EVALUATION_EXECUTOR_UNAVAILABLE"


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
