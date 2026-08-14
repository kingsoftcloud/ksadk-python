from __future__ import annotations

import json

import httpx
import pytest

from ksadk.evaluation.cloud_converter import evalset_to_dataset_snapshot
from ksadk.evaluation.evalset import parse_evalset


def _snapshot():
    return evalset_to_dataset_snapshot(
        parse_evalset(
            {
                "schemaVersion": "ksadk.eval/v1",
                "name": "support-regression",
                "cases": [{"id": "case-001", "turns": [{"input": "hello"}]}],
            }
        )
    )


@pytest.mark.asyncio
async def test_agent_eval_client_publishes_snapshot_to_evalsmith_backed_api():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["headers"] = dict(request.headers)
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "Code": 0,
                "Data": {
                    "DatasetId": "dataset-001",
                    "DatasetVersion": 1,
                    "SchemaHash": _snapshot().schema_hash,
                    "ContentDigest": _snapshot().content_digest,
                    "RowCount": 1,
                    "ProjectId": "project-001",
                    "IdempotencyKey": "publish-001",
                },
            },
        )

    from ksadk.evaluation.agent_eval_client import AgentEvalCloudDatasetClient

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = AgentEvalCloudDatasetClient(
            "https://agent-eval.example",
            api_token="secret",
            account_id="account-001",
            http_client=http_client,
        )
        result = await client.publish_snapshot(
            _snapshot(),
            dataset_id=None,
            base_version=None,
            idempotency_key="publish-001",
        )

    assert captured["path"] == "/agentengine/eval/api/v1/PublishEvaluationSetSnapshot"
    assert captured["headers"]["authorization"] == "Bearer secret"  # type: ignore[index]
    assert captured["headers"]["x-ksc-account-id"] == "account-001"  # type: ignore[index]
    assert captured["json"]["SchemaHash"] == _snapshot().schema_hash  # type: ignore[index]
    assert captured["json"]["Columns"][0]["Name"] == "case_id"  # type: ignore[index]
    assert result.dataset_id == "dataset-001"
    assert result.dataset_version == 1


@pytest.mark.asyncio
async def test_agent_eval_client_reads_an_explicit_dataset_version():
    snapshot = _snapshot()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/DescribeEvaluationSetSnapshot")
        payload = json.loads(request.content)
        assert payload["DatasetId"] == "dataset-001"
        assert payload["Version"] == 4
        return httpx.Response(
            200,
            json={
                "Code": 0,
                "Data": {
                    "Name": snapshot.name,
                    "Columns": [column.model_dump(mode="json", by_alias=True) for column in snapshot.columns],
                    "Rows": [{"Values": row.values} for row in snapshot.rows],
                    "ContentDigest": snapshot.content_digest,
                    "SchemaHash": snapshot.schema_hash,
                    "RowCount": len(snapshot.rows),
                },
            },
        )

    from ksadk.evaluation.agent_eval_client import AgentEvalCloudDatasetClient

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = AgentEvalCloudDatasetClient(
            "https://agent-eval.example",
            http_client=http_client,
        )
        restored = await client.read_snapshot("dataset-001", 4, project_id="project-001")

    assert restored.content_digest == snapshot.content_digest
    assert restored.schema_hash == snapshot.schema_hash
    assert restored.rows[0].values["case_id"] == "case-001"


@pytest.mark.asyncio
async def test_agent_eval_client_lists_dataset_versions_without_current_execution_read():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/ListEvaluationSet")
        payload = json.loads(request.content)
        assert payload["ProjectId"] == "project-001"
        return httpx.Response(
            200,
            json={
                "Code": 0,
                "Data": {
                    "Items": [
                        {
                            "DatasetId": "dataset-001",
                            "Name": "support",
                            "ProjectId": "project-001",
                            "Version": 4,
                            "SchemaHash": "a" * 64,
                            "ContentDigest": "b" * 64,
                            "RowCount": 3,
                        }
                    ]
                },
            },
        )

    from ksadk.evaluation.agent_eval_client import AgentEvalCloudDatasetClient

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = AgentEvalCloudDatasetClient(
            "https://agent-eval.example",
            http_client=http_client,
        )
        items = await client.list_datasets(project_id="project-001")

    assert items[0].dataset_id == "dataset-001"
    assert items[0].version == 4
    assert items[0].row_count == 3
