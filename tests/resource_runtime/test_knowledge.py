import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from ksadk.knowledge_base.client import KnowledgeBaseClient
from ksadk.resource_runtime.contracts import ResourceConfig
from ksadk.resource_runtime.knowledge import BoundKnowledgeService


def service(call, resource_id="kb-a", **policy):
    config = ResourceConfig.model_validate(
        {
            "binding": {
                "id": "binding-a",
                "connectionRef": "connection-a",
                "resource": {"kind": "knowledge-base", "id": resource_id, "region": "region-a"},
            },
            "retrieval": {"topK": 2, "maxChars": 6, **policy},
        }
    )
    client = KnowledgeBaseClient(
        dataset_id=resource_id,
        region="region-a",
        access_key="fake-access",
        secret_key="fake-secret",
    )

    class Upstream:
        def call(self, action, arguments, **kwargs):
            assert action == "RetrieveKnowledge"
            assert arguments["DatasetId"] == resource_id
            return call(arguments)

    client._aicp_client = Upstream()
    return BoundKnowledgeService(config, client)


def test_binding_limits_and_sources_are_preserved():
    calls = []

    def call(arguments):
        calls.append(arguments)
        return json.dumps(
            {
                "RequestId": "request-a",
                "Records": [
                    {
                        "Score": 0.8,
                        "Segment": {
                            "Id": f"segment-{i}",
                            "DocumentId": "doc-a",
                            "Document": {"Name": "Document A"},
                            "Content": "abcd",
                        },
                    }
                    for i in range(3)
                ],
            }
        )

    result = service(call).search({"query": "question", "topK": 999})
    assert calls[0]["RetrievalModel"]["TopK"] == 2
    assert [item.content for item in result.items] == ["abcd", "ab"]
    assert result.items[0].document_id == "doc-a"
    assert result.items[0].score == 0.8
    assert result.truncated and result.request_id == "request-a"
    assert "Document A" in result.text()


@pytest.mark.parametrize(
    "response",
    [
        "not-json",
        {},
        {"RequestId": "request-no-records"},
        {"Code": 200},
        {"Records": [], "Code": False},
        {"Records": [], "Code": 200.0},
        "[]",
        {"Code": 403},
        {"ResponseMetadata": {"Error": {"Code": "denied"}}},
        {"Records": None},
        {"Records": [None]},
    ],
)
def test_failure_is_not_empty(response):
    reply = service(lambda _: response).search({"query": "question"})
    assert reply.status == "failed"
    assert reply.items == ()
    assert reply.error_code == "KNOWLEDGE_RETRIEVAL_FAILED"


def test_true_empty_is_not_failure():
    reply = service(lambda _: {"Records": []}).search({"query": "question"})
    assert reply.status == "empty"
    assert reply.error_code is None


def test_oversized_request_id_does_not_escape_failure_projection():
    reply = service(lambda _: {"Code": 403, "RequestId": "x" * 10000}).search({"query": "q"})
    assert reply.status == "failed"
    assert reply.request_id == ""


@pytest.mark.parametrize(
    "extra", [{"datasetId": "kb-b"}, {"userId": "user-b"}, {"topK": False}, {"query": ""}]
)
def test_model_cannot_override_resource_or_send_invalid_query(extra):
    calls = []
    with pytest.raises(ValidationError):
        service(lambda args: calls.append(args)).search({"query": "question", **extra})
    assert not calls


def test_concurrent_empty_and_failure_remain_request_local():
    entered = threading.Event()
    release = threading.Event()

    def call(arguments):
        if arguments["Query"] == "empty":
            entered.set()
            assert release.wait(5)
            return {"Records": [], "RequestId": "request-empty"}
        return {"Code": 403, "RequestId": "request-failed"}

    bound = service(call)
    with ThreadPoolExecutor(max_workers=2) as pool:
        empty = pool.submit(bound.search, {"query": "empty"})
        assert entered.wait(5)
        failed = pool.submit(bound.search, {"query": "failed"})
        release.set()
        a, b = empty.result(), failed.result()
    assert (a.status, a.request_id) == ("empty", "request-empty")
    assert (b.status, b.request_id) == ("failed", "request-failed")


def test_upstream_secrets_are_not_logged_or_returned(caplog):
    sensitive = "fake-sensitive-knowledge-and-credential"

    def call(_):
        raise RuntimeError(sensitive)

    with caplog.at_level(logging.DEBUG):
        reply = service(call).search({"query": sensitive})
    assert sensitive not in caplog.text
    assert sensitive not in reply.model_dump_json()
    assert reply.status == "failed"
