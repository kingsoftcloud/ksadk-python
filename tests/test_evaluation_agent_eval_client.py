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
    assert captured["json"]["Columns"][1]["TextSchema"]["items"]["type"] == "object"  # type: ignore[index]
    assert result.dataset_id == "dataset-001"
    assert result.dataset_version == 1


@pytest.mark.asyncio
async def test_agent_eval_client_reads_an_explicit_dataset_version_from_describe_pages():
    snapshot = _snapshot()
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/DescribeEvaluationSet")
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["DatasetId"] == "dataset-001"
        assert payload["DatasetVersion"] == 4
        page = payload["Page"]
        return httpx.Response(
            200,
            json={
                "Code": 0,
                "Data": {
                    "Name": snapshot.name,
                    "Description": snapshot.description,
                    "Columns": [
                        {
                            "Key": column.name,
                            "Name": column.name,
                            "ValueType": (
                                "Array<Object>"
                                if column.value_type == "Array"
                                else column.value_type
                            ),
                            "Required": column.required,
                            "Description": column.description,
                            "TextSchema": column.text_schema
                            or (
                                {"type": "string", "title": column.name}
                                if column.value_type == "String"
                                else None
                            ),
                        }
                        for column in snapshot.columns
                    ],
                    "Items": (
                        [{"DataItemId": "row-001", "Row": snapshot.rows[0].values}]
                        if page == 1
                        else []
                    ),
                    "CurrentVersion": 4,
                    "RowCount": len(snapshot.rows),
                    "Total": len(snapshot.rows),
                    "Page": page,
                    "PageSize": payload["PageSize"],
                    "HasMore": page == 1,
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
    assert [request["Page"] for request in requests] == [1, 2]


@pytest.mark.asyncio
async def test_agent_eval_client_falls_back_to_global_ksyun_account(monkeypatch):
    snapshot = _snapshot()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-ksc-account-id"] == "account-from-global-config"
        return httpx.Response(
            200,
            json={
                "Code": 0,
                "Data": {
                    "DatasetId": "dataset-001",
                    "DatasetVersion": 1,
                    "SchemaHash": snapshot.schema_hash,
                    "ContentDigest": snapshot.content_digest,
                    "RowCount": 1,
                },
            },
        )

    monkeypatch.delenv("AGENT_EVAL_ACCOUNT_ID", raising=False)
    monkeypatch.setenv("KSYUN_ACCOUNT_ID", "account-from-global-config")

    from ksadk.evaluation.agent_eval_client import AgentEvalCloudDatasetClient

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = AgentEvalCloudDatasetClient(
            "https://agent-eval.example",
            http_client=http_client,
        )
        await client.publish_snapshot(
            snapshot,
            dataset_id="dataset-001",
            base_version=None,
            idempotency_key="publish-001",
        )


@pytest.mark.asyncio
async def test_agent_eval_client_lists_dataset_versions_without_current_execution_read():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/ListEvaluationSet")
        payload = json.loads(request.content)
        assert payload["ProjectId"] == "project-001"
        assert payload["DatasetType"] == "Manual"
        assert "Column" not in payload
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


@pytest.mark.asyncio
async def test_agent_eval_client_catalog_reads_all_list_pages_before_filtering():
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        page = payload.get("Page", 1)
        if page == 1:
            return httpx.Response(
                200,
                json={
                    "Code": 0,
                    "Data": {
                        "Items": [{"DatasetId": "general-1", "CurrentVersion": 0}],
                        "Page": 1,
                        "PageSize": 1,
                        "Total": 2,
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "Code": 0,
                "Data": {
                    "Items": [
                        {
                            "DatasetId": "dataset-002",
                            "Name": "support",
                            "ProjectId": "project-001",
                            "Version": 2,
                            "SchemaHash": "a" * 64,
                            "ContentDigest": "b" * 64,
                            "RowCount": 1,
                        }
                    ],
                    "Page": 2,
                    "PageSize": 1,
                    "Total": 2,
                },
            },
        )

    from ksadk.evaluation.agent_eval_client import AgentEvalCloudDatasetClient

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = AgentEvalCloudDatasetClient("https://agent-eval.example", http_client=http_client)
        items = await client.list_datasets(project_id="project-001")

    assert [payload["Page"] for payload in requests] == [1, 2]
    assert [item.dataset_id for item in items] == ["dataset-002"]


@pytest.mark.asyncio
async def test_agent_eval_client_uses_kop_by_default(monkeypatch):
    snapshot = _snapshot()
    calls: list[tuple[str, dict[str, object]]] = []

    class FakeKOPClient:
        def post_action(self, action: str, payload: dict[str, object]):
            calls.append((action, payload))
            return {
                "DatasetId": "dataset-001",
                "DatasetVersion": 1,
                "SchemaHash": snapshot.schema_hash,
                "ContentDigest": snapshot.content_digest,
                "RowCount": 1,
            }

    monkeypatch.delenv("AGENT_EVAL_BASE_URL", raising=False)

    from ksadk.evaluation.agent_eval_client import AgentEvalCloudDatasetClient

    client = AgentEvalCloudDatasetClient(kop_client=FakeKOPClient())
    result = await client.publish_snapshot(
        snapshot,
        dataset_id="dataset-001",
        base_version=None,
        idempotency_key="publish-001",
    )

    assert client.uses_kop is True
    assert calls[0][0] == "PublishEvaluationSetSnapshot"
    assert calls[0][1]["DatasetId"] == "dataset-001"
    assert calls[0][1]["IdempotencyKey"] == "publish-001"
    assert result.dataset_version == 1


def test_agent_eval_base_url_selects_direct_http(monkeypatch):
    monkeypatch.setenv("AGENT_EVAL_BASE_URL", "http://localhost:8000/")

    from ksadk.evaluation.agent_eval_client import AgentEvalCloudDatasetClient

    client = AgentEvalCloudDatasetClient()

    assert client.uses_kop is False


@pytest.mark.asyncio
async def test_agent_eval_catalog_recovers_digest_from_current_version_when_list_omits_hashes(
    monkeypatch,
):
    snapshot = _snapshot()

    class FakeKOPClient:
        def post_action(self, action: str, payload: dict[str, object]):
            assert action == "ListEvaluationSet"
            assert payload == {
                "DatasetType": "Manual",
                "ProjectId": "project-001",
                "Page": 1,
                "PageSize": 100,
            }
            return {
                "Items": [
                    {
                        "DatasetId": "dataset-001",
                        "Name": "support",
                        "CurrentVersion": 4,
                        "RowCount": 1,
                    }
                ]
            }

    monkeypatch.delenv("AGENT_EVAL_BASE_URL", raising=False)

    from ksadk.evaluation.agent_eval_client import AgentEvalCloudDatasetClient

    client = AgentEvalCloudDatasetClient(kop_client=FakeKOPClient())

    async def fake_read(dataset_id: str, version: int, *, project_id: str | None = None):
        assert (dataset_id, version, project_id) == ("dataset-001", 4, "project-001")
        return snapshot

    monkeypatch.setattr(client, "read_snapshot", fake_read)
    items = await client.list_datasets(project_id="project-001")

    assert len(items) == 1
    assert items[0].version == 4
    assert items[0].schema_hash == snapshot.schema_hash
    assert items[0].content_digest == snapshot.content_digest


@pytest.mark.asyncio
async def test_agent_eval_catalog_uses_each_item_project_for_snapshot_recovery(monkeypatch):
    snapshot = _snapshot()

    class FakeKOPClient:
        def post_action(self, action: str, payload: dict[str, object]):
            assert action == "ListEvaluationSet"
            return {
                "Items": [
                    {
                        "DatasetId": "dataset-001",
                        "Name": "support",
                        "ProjectId": "project-from-item",
                        "CurrentVersion": 4,
                    }
                ]
            }

    monkeypatch.delenv("AGENT_EVAL_BASE_URL", raising=False)

    from ksadk.evaluation.agent_eval_client import AgentEvalCloudDatasetClient

    client = AgentEvalCloudDatasetClient(kop_client=FakeKOPClient())

    async def fake_read(dataset_id: str, version: int, *, project_id: str | None = None):
        assert (dataset_id, version, project_id) == ("dataset-001", 4, "project-from-item")
        return snapshot

    monkeypatch.setattr(client, "read_snapshot", fake_read)
    items = await client.list_datasets()

    assert [item.dataset_id for item in items] == ["dataset-001"]
