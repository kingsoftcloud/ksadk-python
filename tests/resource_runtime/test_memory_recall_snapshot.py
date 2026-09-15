import time

import pytest
from pydantic import ValidationError

from ksadk.resource_runtime.broker import ResourceBroker
from ksadk.resource_runtime.build_artifacts import restore_resource_build, write_resource_build
from ksadk.resource_runtime.leases import ResourceLeaseRegistry
from ksadk.resource_runtime.process import ResourceWorkerProcess
from ksadk.resource_runtime.snapshots import MemoryRecallPolicy, ResourceSnapshot
from ksadk.resource_runtime.worker import WorkerInitialization
from ksadk.studio.contracts import MemoryRecallSpec
from tests.resource_runtime.test_memory_worker import memory_initialization
from tests.resource_runtime.test_memory_worker import memory_upstream as memory_upstream
from tests.resource_runtime.test_worker_process import initialization


def configured(endpoint, *, top_k=3, max_tokens=100):
    payload = memory_initialization(endpoint).pipe_payload()
    payload["resourceSnapshot"]["bindings"][0]["memoryRecall"] = {
        "topK": top_k,
        "maxTokens": max_tokens,
        "minScore": 0,
    }
    snapshot = ResourceSnapshot.model_validate(payload["resourceSnapshot"])
    payload["scopes"][0]["bindingSnapshotDigest"] = snapshot.digest
    return WorkerInitialization.model_validate(payload)


def test_recall_budget_changes_snapshot_and_survives_offline_restore(tmp_path):
    first = configured("https://example.test").resource_snapshot
    second = configured("https://example.test", max_tokens=42).resource_snapshot
    assert first.digest != second.digest
    assert first.bindings[0].config.digest == second.bindings[0].config.digest
    reference = write_resource_build(tmp_path / "build", second, {})
    restored, _ = restore_resource_build(
        tmp_path / "build",
        reference,
        cache_directory=tmp_path / "cache",
    )
    assert restored.snapshot.bindings[0].memory_recall.max_tokens == 42
    assert restored.snapshot.digest == second.digest


def test_agent_recall_projection_preserves_limits_and_rejects_unsupported_score():
    spec = MemoryRecallSpec(top_k=7, max_tokens=42, min_score=0)
    policy = MemoryRecallPolicy.model_validate(spec.model_dump(exclude={"enabled"}))
    assert policy.top_k == 7 and policy.max_tokens == 42
    with pytest.raises(ValidationError, match="MEMORY_SEARCH_POLICY_UNSUPPORTED"):
        MemoryRecallPolicy.model_validate(MemoryRecallSpec().model_dump(exclude={"enabled"}))


def test_memory_policy_cannot_be_attached_to_knowledge_binding():
    payload = initialization("https://example.test").pipe_payload()["resourceSnapshot"]
    payload["bindings"][0]["memoryRecall"] = {"topK": 3}
    with pytest.raises(ValidationError, match="memory binding"):
        ResourceSnapshot.model_validate(payload)


@pytest.mark.parametrize(
    "top_k,max_tokens,expected_records,expected_calls",
    [
        (2, 0, 0, 0),
        (3, 5, 0, 1),
        (4, 100, 1, 1),
    ],
)
async def test_actual_worker_uses_frozen_budget(
    memory_upstream,
    top_k,
    max_tokens,
    expected_records,
    expected_calls,
):
    endpoint, calls = memory_upstream
    init = configured(endpoint, top_k=top_k, max_tokens=max_tokens)
    registry = ResourceLeaseRegistry()
    lease = registry.issue(init.scopes[0])
    broker = ResourceBroker(registry, "generation-a")
    worker = await ResourceWorkerProcess.start(init)
    broker.register(lease.scope, worker)
    request = {
        "v": 1,
        "requestId": "bounded-memory",
        "operation": "load_memory",
        "deadline": int((time.time() + 10) * 1000),
        "handle": lease.handle,
        "arguments": {"query": "preferences"},
    }
    try:
        denied = await broker.dispatch(
            {
                **request,
                "arguments": {
                    "query": "preferences",
                    "maxTokens": 100000,
                },
            }
        )
        assert denied["error"]["code"] == "RESOURCE_ARGUMENTS_INVALID"
        reply = await broker.dispatch(request)
        assert reply["result"]["status"] == "ok", reply
        assert len(reply["result"]["records"]) == expected_records
        assert len(calls) == expected_calls
        if calls:
            assert calls[0][1]["Limit"] == top_k
    finally:
        await worker.aclose()
