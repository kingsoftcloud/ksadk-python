import asyncio
import time

import pytest

from ksadk.resource_runtime.broker import ResourceBroker
from ksadk.resource_runtime.leases import ResourceLeaseRegistry
from ksadk.resource_runtime.operation_ledger import OperationLedger
from tests.resource_runtime.test_broker import WaitingWorker, Worker, scope, setup


class HostApproval:
    async def authorize(self, request, scope):
        # Fixture for a trusted host lookup, not an IPC approval flag.
        if request.arguments != {"content": "approved preference"}:
            return None
        return "checkpoint-event-a"


def configured(tmp_path, worker, authorizer=None):
    broker, request = setup(
        worker,
        write_authorizer=authorizer or HostApproval(),
        operation_ledger=OperationLedger(tmp_path / "ledger"),
    )
    request.update(operation="save_memory", arguments={"content": "approved preference"})
    return broker, request


async def test_recovered_broker_does_not_repeat_write(tmp_path):
    worker = Worker()
    broker, request = configured(tmp_path, worker)
    first = await broker.dispatch(request)
    assert len(worker.calls) == 1
    assert first["operationReceipt"]["state"] == "unknown"
    registry = ResourceLeaseRegistry()
    next_scope = scope().model_copy(
        update={
            "activation_id": "activation-after-restart",
            "generation_id": "generation-after-restart",
        }
    )
    lease = registry.issue(next_scope)
    recovered = ResourceBroker(
        registry,
        next_scope.generation_id,
        write_authorizer=HostApproval(),
        operation_ledger=OperationLedger(tmp_path / "ledger"),
    )
    recovered.register(next_scope, worker)
    next_request = {**request, "handle": lease.handle}
    result = await recovered.dispatch({**next_request, "requestId": "new-call-id"})
    assert result["result"]["replayed"] is False
    assert result["result"]["operationId"] == first["operationReceipt"]["operationId"]
    assert len(worker.calls) == 1


async def test_unapproved_parameters_never_claim_or_dispatch(tmp_path):
    worker = Worker()
    broker, request = configured(tmp_path, worker)
    result = await broker.dispatch({**request, "arguments": {"content": "other", "approved": True}})
    assert result["error"]["code"] == "RESOURCE_ARGUMENTS_INVALID"
    assert not worker.calls
    assert "result" in await broker.dispatch(request)
    assert len(worker.calls) == 1


async def test_concurrent_duplicates_dispatch_once(tmp_path):
    worker = Worker()
    broker, request = configured(tmp_path, worker)
    replies = await asyncio.gather(*(broker.dispatch(request) for _ in range(4)))
    assert len(worker.calls) == 1
    assert sum("operationReceipt" in reply for reply in replies) == 1


async def test_timeout_and_lost_worker_remain_unknown(tmp_path):
    worker = WaitingWorker()
    broker, request = configured(tmp_path, worker)
    result = await broker.dispatch({**request, "deadline": int((time.time() + 0.05) * 1000)})
    assert result["error"]["code"] == "RESOURCE_WRITE_UNKNOWN"
    assert worker.calls == 1
    worker.release.set()
    await worker.completed.wait()
    result = await broker.dispatch(request)
    assert result["result"]["status"] == "unknown"
    assert worker.calls == 1


async def test_revocation_during_host_approval_prevents_write(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    class DelayedApproval(HostApproval):
        async def authorize(self, request, scope):
            started.set()
            await release.wait()
            return await super().authorize(request, scope)

    worker = Worker()
    broker, request = configured(tmp_path, worker, DelayedApproval())
    task = asyncio.create_task(broker.dispatch(request))
    await started.wait()
    broker.revoke_generation()
    release.set()
    assert (await task)["error"]["code"] == "RESOURCE_SCOPE_INVALID"
    assert not worker.calls


async def test_caller_cannot_mutate_arguments_while_approval_waits(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    class DelayedApproval(HostApproval):
        async def authorize(self, request, scope):
            decision = await super().authorize(request, scope)
            started.set()
            await release.wait()
            return decision

    worker = Worker()
    broker, request = configured(tmp_path, worker, DelayedApproval())
    task = asyncio.create_task(broker.dispatch(request))
    await started.wait()
    request["arguments"]["content"] = "changed after approval"
    release.set()
    await task
    assert worker.calls[0][0].arguments == {"content": "approved preference"}


async def test_caller_cancellation_does_not_cancel_or_repeat_dispatched_write(tmp_path):
    worker = WaitingWorker()
    broker, request = configured(tmp_path, worker)
    pending = asyncio.create_task(broker.dispatch(request))
    await worker.started.wait()
    underlying = tuple(broker._activations["activation-a"].tasks)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert all(not task.done() for task in underlying)
    assert worker.calls == 1
    worker.release.set()
    await asyncio.gather(*underlying)
    replay = await broker.dispatch(request)
    assert replay["result"]["status"] == "unknown"
    assert worker.calls == 1


async def test_retired_inflight_write_stays_unknown_after_recovery(tmp_path):
    worker = WaitingWorker()
    broker, request = configured(tmp_path, worker)
    pending = asyncio.create_task(broker.dispatch(request))
    await worker.started.wait()
    # Model an owner that has stopped its transport with no response. The durable
    # claim already exists; canceling the waiter must not permit replay on restart.
    await asyncio.wait_for(broker.retire_activation("activation-a"), timeout=1)
    assert (await pending)["error"]["code"] == "RESOURCE_WRITE_UNKNOWN"
    assert "activation-a" not in broker._activations
    recovered_worker = Worker()
    recovered, retry = configured(tmp_path, recovered_worker)
    result = await recovered.dispatch(retry)
    assert result["result"]["status"] == "unknown"
    assert result["result"]["replayed"] is False
    assert not recovered_worker.calls
