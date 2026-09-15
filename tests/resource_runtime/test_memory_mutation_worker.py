import asyncio
import threading
import time

from ksadk.resource_runtime.broker import ResourceBroker
from ksadk.resource_runtime.leases import ResourceLeaseRegistry
from ksadk.resource_runtime.operation_ledger import OperationLedger
from ksadk.resource_runtime.process import ResourceWorkerProcess
from tests.resource_runtime.test_memory_worker import memory_initialization
from tests.resource_runtime.test_memory_worker import memory_upstream as memory_upstream
from tests.resource_runtime.test_worker_process import freeze_initialization


async def test_scoped_worker_mutations_require_approval_and_deduplicate(
    tmp_path, memory_upstream, monkeypatch
):
    endpoint, calls = memory_upstream
    payload = memory_initialization(endpoint).pipe_payload()
    payload["scopes"][0]["allowedOperations"] = ["load_memory", "update_memory", "delete_memory"]
    init = freeze_initialization(payload)

    class HostApproval:
        approved = False

        async def authorize(self, request, scope):
            return "event-" + request.operation if self.approved else None

    owner = HostApproval()
    registry = ResourceLeaseRegistry()
    ledger = OperationLedger(tmp_path / "ledger")
    broker = ResourceBroker(
        registry, "generation-a", write_authorizer=owner, operation_ledger=ledger
    )
    worker = await ResourceWorkerProcess.start(init)
    lease = registry.issue(init.scopes[0])
    broker.register(lease.scope, worker)

    async def invoke(operation, arguments):
        return await broker.dispatch(
            {
                "v": 1,
                "requestId": "transport-request",
                "handle": lease.handle,
                "deadline": int(time.time() * 1000) + 10000,
                "operation": operation,
                "arguments": arguments,
            }
        )

    try:
        update = {"memoryId": "server-real-id", "content": "new preference"}
        assert (await invoke("update_memory", update))["error"][
            "code"
        ] == "RESOURCE_APPROVAL_REQUIRED"
        assert calls == []
        assert (await invoke("load_memory", {"query": "preference"}))["result"]["status"] == "ok"
        owner.approved = True
        changed = await invoke("update_memory", update)
        assert changed["result"]["newMemoryId"] == "updated-real-id"
        assert changed["operationReceipt"]["state"] == "succeeded"
        assert (await invoke("update_memory", update))["result"]["replayed"] is False
        assert len(calls) == 2
        removed = await invoke("delete_memory", {"memoryId": "updated-real-id"})
        assert removed["operationReceipt"]["state"] == "succeeded"
        assert (await invoke("delete_memory", {"memoryId": "updated-real-id"}))["result"][
            "replayed"
        ] is False
        assert len(calls) == 3
        partition = lease.scope.identity.memory_partition(init.bindings[0].config.binding.resource)
        assert all(body["AgentUserId"] == partition for _, body in calls)
        assert all(body["MemoryCollectionId"] == "memory-selected" for _, body in calls)
        reopened = OperationLedger(tmp_path / "ledger")
        receipt = reopened.lookup(
            removed["result"]["operationId"],
            authority_digest=broker._authority(lease.scope),
            operation="delete_memory",
        )
        assert receipt.state == "succeeded"
        await worker.aclose()
        registry.revoke_activation(lease.scope.activation_id)
        payload["scopes"][0]["activationId"] = "resumed-activation"
        resumed = freeze_initialization(payload)
        registry = ResourceLeaseRegistry()
        broker = ResourceBroker(
            registry,
            "generation-a",
            write_authorizer=owner,
            operation_ledger=reopened,
        )
        worker = await ResourceWorkerProcess.start(resumed)
        lease = registry.issue(resumed.scopes[0])
        broker.register(lease.scope, worker)
        restored = await invoke("update_memory", update)
        assert restored["result"] == {**changed["result"], "replayed": False}
        assert (await invoke("delete_memory", {"memoryId": "updated-real-id"}))["result"] == {
            **removed["result"],
            "replayed": False,
        }
        assert len(calls) == 3
        entered, release = threading.Event(), threading.Event()
        original = reopened.memory_mutation_result

        def delayed_result(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return original(*args, **kwargs)

        monkeypatch.setattr(reopened, "memory_mutation_result", delayed_result)
        pending = asyncio.create_task(invoke("update_memory", update))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            registry.revoke_activation(lease.scope.activation_id)
        finally:
            release.set()
        denied = await pending
        assert denied["error"]["code"] == "RESOURCE_SCOPE_INVALID"
        assert "updated-real-id" not in str(denied)
        assert len(calls) == 3
    finally:
        await worker.aclose()
