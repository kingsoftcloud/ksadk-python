import asyncio
import time

import pytest

from ksadk.knowledge_base.client import KnowledgeBaseClient
from ksadk.resource_runtime.broker import ResourceBroker
from ksadk.resource_runtime.contracts import ResourceConfig
from ksadk.resource_runtime.knowledge import BoundKnowledgeService
from ksadk.resource_runtime.leases import ResourceLeaseRegistry, ResourceScope


def scope():
    return ResourceScope.model_validate(
        {
            "profileDigest": "sha256:" + "a" * 64,
            "generationId": "generation-a",
            "activationId": "activation-a",
            "buildDigest": "sha256:" + "b" * 64,
            "bindingSnapshotDigest": "sha256:" + "c" * 64,
            "bindingId": "binding-a",
            "identity": {
                "tenantRef": "tenant-a",
                "resourcePrincipalRef": "account-a",
                "actorRef": "actor-a",
                "memorySubjectRef": "user-a",
                "agentId": "agent-a",
                "sessionRef": "session-a",
            },
            "allowedOperations": ["search_knowledge_base", "save_memory"],
        }
    )


def setup(worker, **kwargs):
    registry = ResourceLeaseRegistry()
    lease = registry.issue(scope())
    broker = ResourceBroker(registry, "generation-a", **kwargs)
    broker.register(lease.scope, worker)
    request = {
        "v": 1,
        "requestId": "request-a",
        "deadline": int((time.time() + 10) * 1000),
        "handle": lease.handle,
        "operation": "search_knowledge_base",
        "arguments": {"query": "question"},
    }
    return broker, request


class Worker:
    def __init__(self):
        self.calls = []

    async def invoke(self, request, scope):
        self.calls.append((request, scope))
        return {"status": "empty"}


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"handle": "invalid" * 8}, "RESOURCE_SCOPE_INVALID"),
        ({"operation": "load_memory"}, "RESOURCE_SCOPE_INVALID"),
        ({"operation": "save_memory"}, "RESOURCE_APPROVAL_REQUIRED"),
        ({"operation": "eval"}, "RESOURCE_REQUEST_INVALID"),
        ({"deadline": 1}, "RESOURCE_DEADLINE_EXCEEDED"),
        ({"userId": "user-b"}, "RESOURCE_REQUEST_INVALID"),
    ],
)
async def test_rejection_happens_before_worker(changes, code):
    worker = Worker()
    broker, request = setup(worker)
    result = await broker.dispatch({**request, **changes})
    assert result["error"]["code"] == code
    assert worker.calls == []


async def test_revoked_lease_never_reaches_worker():
    worker = Worker()
    broker, request = setup(worker)
    broker.revoke_activation("activation-a")
    result = await broker.dispatch(request)
    assert result["error"]["code"] == "RESOURCE_SCOPE_INVALID"
    assert not worker.calls


async def test_actual_bound_service_receives_only_frozen_resource():
    calls = []

    class Upstream:
        def call(self, action, arguments, **kwargs):
            calls.append(arguments)
            return {"Records": [], "RequestId": "upstream-a"}

    config = ResourceConfig.model_validate(
        {
            "binding": {
                "id": "binding-a",
                "connectionRef": "connection-a",
                "resource": {"kind": "knowledge-base", "id": "kb-a", "region": "region-a"},
            },
        }
    )
    client = KnowledgeBaseClient(
        dataset_id="kb-a", region="region-a", access_key="fake-access", secret_key="fake-secret"
    )
    client._aicp_client = Upstream()
    service = BoundKnowledgeService(config, client)

    class ServiceWorker:
        async def invoke(self, request, scope):
            assert scope.identity.memory_subject_ref == "user-a"
            reply = await asyncio.to_thread(service.search, request.arguments)
            return reply.model_dump(by_alias=True, mode="json")

    broker, request = setup(ServiceWorker())
    result = await broker.dispatch(request)
    assert result["result"]["status"] == "empty"
    assert result["result"]["requestId"] == "upstream-a"
    assert result["requestId"] == "request-a"
    assert calls[0]["DatasetId"] == "kb-a"
    result = await broker.dispatch({**request, "arguments": {"query": "q", "datasetId": "kb-b"}})
    assert "error" in result
    assert len(calls) == 1


class WaitingWorker:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.completed = asyncio.Event()
        self.calls = 0

    async def invoke(self, request, scope):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        self.completed.set()
        return {"status": "empty"}


async def test_timeout_retains_capacity_until_underlying_work_finishes():
    worker = WaitingWorker()
    broker, request = setup(worker, max_inflight=1)
    short = {**request, "deadline": int((time.time() + 0.02) * 1000)}
    result = await broker.dispatch(short)
    assert worker.started.is_set()
    assert result["error"]["code"] == "RESOURCE_DEADLINE_EXCEEDED"
    assert (await broker.dispatch(request))["error"]["code"] == "RESOURCE_BUSY"
    worker.release.set()
    await worker.completed.wait()
    await asyncio.sleep(0)  # deliver the task completion callback
    assert (await broker.dispatch(request))["result"]["status"] == "empty"


async def test_retirement_cancels_waiting_and_queued_calls_without_returning_content():
    worker = WaitingWorker()
    broker, request = setup(worker)
    first = asyncio.create_task(broker.dispatch(request))
    await worker.started.wait()
    second = asyncio.create_task(broker.dispatch({**request, "requestId": "queued"}))
    await asyncio.sleep(0)
    await asyncio.wait_for(broker.retire_activation("activation-a"), timeout=1)
    replies = await asyncio.gather(first, second)
    assert all(reply["error"]["code"] == "RESOURCE_ACTIVATION_UNAVAILABLE" for reply in replies)
    assert worker.calls == 1
    assert not worker.completed.is_set()
    assert "activation-a" not in broker._activations


async def test_revoke_during_request_discards_returned_content():
    worker = WaitingWorker()
    broker, request = setup(worker)
    pending = asyncio.create_task(broker.dispatch(request))
    await worker.started.wait()
    broker.revoke_generation()
    worker.release.set()
    result = await pending
    assert result["error"]["code"] == "RESOURCE_SCOPE_INVALID"
    assert "result" not in result


async def test_raw_worker_error_is_not_returned():
    class BrokenWorker:
        async def invoke(self, request, scope):
            raise RuntimeError("fake-secret-private-url")

    broker, request = setup(BrokenWorker())
    result = await broker.dispatch(request)
    assert result["error"]["code"] == "RESOURCE_WORKER_FAILED"
    assert "fake-secret" not in str(result)


async def test_queued_request_is_reauthorized_after_revocation():
    worker = WaitingWorker()
    broker, request = setup(worker)
    first = asyncio.create_task(broker.dispatch(request))
    await worker.started.wait()
    second = asyncio.create_task(broker.dispatch({**request, "requestId": "request-b"}))
    await asyncio.sleep(0)
    broker.revoke_activation("activation-a")
    worker.release.set()
    results = await asyncio.gather(first, second)
    assert all(reply["error"]["code"] == "RESOURCE_SCOPE_INVALID" for reply in results)
    assert worker.calls == 1


async def test_forged_tool_identity_is_rejected_before_worker():
    worker = Worker()
    broker, request = setup(worker)
    response = await broker.dispatch({**request, "arguments": {"query": "q", "userId": "forged"}})
    assert response["error"]["code"] == "RESOURCE_ARGUMENTS_INVALID"
    assert worker.calls == []


async def test_distant_deadline_is_bounded_before_worker():
    worker = Worker()
    broker, request = setup(worker)
    await broker.dispatch({**request, "deadline": int((time.time() + 86400) * 1000)})
    assert worker.calls[0][0].deadline <= int((time.time() + 60) * 1000)


async def test_inventory_check_never_calls_worker_or_grants_write():
    worker = Worker()
    broker, request = setup(worker)
    for operation in ("search_knowledge_base", "save_memory"):
        checked = await broker.dispatch({
            **request, "checkOnly": True, "operation": operation, "arguments": {},
        })
        assert checked["result"] == {"available": True}
    assert worker.calls == []
    denied = await broker.dispatch({
        **request, "operation": "save_memory", "arguments": {"content": "no grant"},
    })
    assert denied["error"]["code"] == "RESOURCE_APPROVAL_REQUIRED"
    assert worker.calls == []
    broker.revoke_activation("activation-a")
    stale = await broker.dispatch({**request, "checkOnly": True, "arguments": {}})
    assert stale["error"]["code"] == "RESOURCE_SCOPE_INVALID"


@pytest.mark.parametrize("changes,code", [
    ({"checkOnly": "true"}, "RESOURCE_REQUEST_INVALID"),
    ({"arguments": {"query": "must not run"}}, "RESOURCE_ARGUMENTS_INVALID"),
    ({"operation": "load_memory"}, "RESOURCE_SCOPE_INVALID"),
    ({"deadline": 1}, "RESOURCE_DEADLINE_EXCEEDED"),
])
async def test_inventory_probe_checks_request_and_scope(changes, code):
    worker = Worker()
    broker, request = setup(worker)
    rejected = await broker.dispatch({
        **request, "checkOnly": True, "arguments": {}, **changes,
    })
    assert rejected["error"]["code"] == code
    assert worker.calls == []


async def test_inventory_probe_rejects_stopped_worker_before_monitor_cleanup():
    worker = Worker()
    worker.is_running = False
    broker, request = setup(worker)
    result = await broker.dispatch({**request, "checkOnly": True, "arguments": {}})
    assert result["error"]["code"] == "RESOURCE_ACTIVATION_UNAVAILABLE"
    assert worker.calls == []
