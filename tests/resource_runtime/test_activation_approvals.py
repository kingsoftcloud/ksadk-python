import asyncio
import time

import pytest

from ksadk.resource_runtime.operation_ledger import OperationLedger
from ksadk.resource_runtime.supervisor import ResourceSupervisor
from ksadk.resource_runtime.worker import WorkerInitialization
from tests.resource_runtime.test_memory_worker import memory_initialization
from tests.resource_runtime.test_memory_worker import memory_upstream as memory_upstream


def write_initialization(endpoint, *, user="user-a", activation="activation-a"):
    payload = memory_initialization(endpoint, user=user, activation=activation).pipe_payload()
    payload["scopes"][0]["allowedOperations"].append("save_memory")
    return WorkerInitialization.model_validate(payload)


def request(active):
    return {
        "v": 1,
        "requestId": "same-call-id",
        "handle": active.leases[0].handle,
        "deadline": int((time.time() + 10) * 1000),
        "operation": "save_memory",
        "arguments": {"content": "fixture preference"},
    }


class Approval:
    def __init__(self, user):
        self.user = user
        self.calls = []

    async def authorize(self, request, scope):
        self.calls.append(scope.identity.memory_subject_ref)
        assert scope.identity.memory_subject_ref == self.user
        return "host-event-" + self.user


async def test_supervisor_routes_each_activation_to_its_own_approval(memory_upstream, tmp_path):
    endpoint, calls = memory_upstream
    supervisor = ResourceSupervisor(
        "generation-a", operation_ledger=OperationLedger(tmp_path / "ledger")
    )
    owners = [Approval("user-a"), Approval("user-b")]
    try:
        active = []
        for owner in owners:
            active.append(
                await supervisor.activate(
                    write_initialization(endpoint, user=owner.user, activation="run-" + owner.user),
                    write_authorizer=owner,
                )
            )
        responses = await asyncio.gather(
            *(supervisor._broker.dispatch(request(item)) for item in active)
        )
        assert all(item["result"]["status"] == "accepted_pending" for item in responses)
        assert [owner.calls for owner in owners] == [["user-a"], ["user-b"]]
        assert len({body["AgentUserId"] for _, body in calls}) == 2

        readonly = await supervisor.activate(
            write_initialization(endpoint, user="user-c", activation="run-c")
        )
        denied = await supervisor._broker.dispatch(request(readonly))
        assert denied["error"]["code"] == "RESOURCE_APPROVAL_REQUIRED"
        assert len(calls) == 2
        await supervisor.deactivate(active[0].activation_id)
        assert active[0].activation_id not in supervisor._approvals.owners
        assert active[1].activation_id in supervisor._approvals.owners
    finally:
        await supervisor.aclose()
    assert not supervisor._approvals.owners


async def test_stopping_activation_during_approval_never_dispatches(memory_upstream, tmp_path):
    endpoint, calls = memory_upstream
    supervisor = ResourceSupervisor(
        "generation-a", operation_ledger=OperationLedger(tmp_path / "ledger")
    )
    started, release = asyncio.Event(), asyncio.Event()

    class Delayed(Approval):
        async def authorize(self, request, scope):
            started.set()
            await release.wait()
            return await super().authorize(request, scope)

    try:
        active = await supervisor.activate(
            write_initialization(endpoint), write_authorizer=Delayed("user-a")
        )
        pending = asyncio.create_task(supervisor._broker.dispatch(request(active)))
        await started.wait()
        stopping = asyncio.create_task(supervisor.deactivate(active.activation_id))
        # Wait until the stop has actually revoked authority, not merely been scheduled.
        for _ in range(100):
            if active.activation_id not in supervisor._approvals.owners:
                break
            await asyncio.sleep(0.001)
        assert active.activation_id not in supervisor._approvals.owners
        # Stopping must finish even if the approval UI never responds.
        await asyncio.wait_for(stopping, timeout=2)
        assert "error" in await pending
        assert not release.is_set()
        assert active.activation_id not in supervisor._broker._activations
        assert not calls
    finally:
        release.set()
        await supervisor.aclose()


async def test_approval_requires_ledger_before_worker_start(memory_upstream):
    endpoint, _ = memory_upstream
    supervisor = ResourceSupervisor("generation-a")
    try:
        with pytest.raises(ValueError, match="durable"):
            await supervisor.activate(
                write_initialization(endpoint), write_authorizer=Approval("user-a")
            )
        assert not supervisor._running
        assert not supervisor._approvals.owners
    finally:
        await supervisor.aclose()


async def test_failed_worker_start_discards_approval_owner(tmp_path, monkeypatch):
    from ksadk.resource_runtime.process import ResourceWorkerProcess

    async def failed_start(initialization):
        raise RuntimeError("fixture startup failure")

    monkeypatch.setattr(ResourceWorkerProcess, "start", failed_start)
    supervisor = ResourceSupervisor(
        "generation-a", operation_ledger=OperationLedger(tmp_path / "ledger")
    )
    try:
        with pytest.raises(RuntimeError, match="startup failure"):
            await supervisor.activate(
                write_initialization("https://example.test"), write_authorizer=Approval("user-a")
            )
        assert not supervisor._approvals.owners
        assert not supervisor._running
    finally:
        await supervisor.aclose()
