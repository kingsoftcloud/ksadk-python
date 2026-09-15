import asyncio
import time

import pytest

from ksadk.resource_runtime.leases import LeaseRejected, ResourceLeaseRegistry
from ksadk.resource_runtime.supervisor import ResourceSupervisor
from tests.resource_runtime.test_broker import scope
from tests.resource_runtime.test_worker_process import initialization


def resolve(supervisor, resources):
    lease = resources.leases[0]
    return supervisor._registry.resolve(
        lease.handle, operation=lease.scope.allowed_operations[0], generation_id="generation-a"
    )


async def allow(scopes):
    return True


async def test_renewal_rotates_handles_without_restarting_or_widening():
    supervisor = ResourceSupervisor("generation-a")
    try:
        current = await supervisor.activate(initialization("https://resources.example.test"))
        observed = []

        async def validate(scopes):
            observed.extend(scopes)
            return True

        renewed = await supervisor.renew(current, revalidate=validate)
        assert observed == [current.leases[0].scope]
        assert renewed.worker_pid == current.worker_pid
        assert renewed.socket_path == current.socket_path
        assert renewed.leases[0].handle != current.leases[0].handle
        with pytest.raises(LeaseRejected):
            resolve(supervisor, current)
        assert resolve(supervisor, renewed) == current.leases[0].scope
        status = await supervisor.runtime_status(agent_id="agent-a", activation_id="activation-a")
        assert status["items"][0]["runtimeState"] == "available"
    finally:
        await supervisor.aclose()


@pytest.mark.parametrize("outcome", [False, "truthy", "exception"])
async def test_failed_revalidation_revokes_existing_activation(outcome):
    supervisor = ResourceSupervisor("generation-a")
    try:
        current = await supervisor.activate(initialization("https://resources.example.test"))

        async def validate(scopes):
            if outcome == "exception":
                raise RuntimeError("fixture failure")
            return outcome

        with pytest.raises((PermissionError, RuntimeError)):
            await supervisor.renew(current, revalidate=validate)
        with pytest.raises(LeaseRejected):
            resolve(supervisor, current)
        assert (
            await supervisor.runtime_status(agent_id="agent-a", activation_id="activation-a")
            is None
        )
    finally:
        await supervisor.aclose()


async def test_stop_during_revalidation_does_not_wait_or_restore_handles():
    supervisor = ResourceSupervisor("generation-a")
    entered, release = asyncio.Event(), asyncio.Event()
    try:
        current = await supervisor.activate(initialization("https://resources.example.test"))

        async def validate(scopes):
            entered.set()
            await release.wait()
            return True

        pending = asyncio.create_task(supervisor.renew(current, revalidate=validate))
        await entered.wait()
        await asyncio.wait_for(supervisor.deactivate(current.activation_id), 2)
        release.set()
        with pytest.raises(ValueError, match="STALE"):
            await pending
        with pytest.raises(LeaseRejected):
            resolve(supervisor, current)
    finally:
        release.set()
        await supervisor.aclose()


async def test_concurrent_renewal_loser_cannot_invalidate_winner():
    supervisor = ResourceSupervisor("generation-a")
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    try:
        current = await supervisor.activate(initialization("https://resources.example.test"))

        async def validate(index, scopes):
            entered[index].set()
            await release[index].wait()
            return True

        first = asyncio.create_task(supervisor.renew(current, revalidate=lambda s: validate(0, s)))
        second = asyncio.create_task(supervisor.renew(current, revalidate=lambda s: validate(1, s)))
        await asyncio.gather(*(event.wait() for event in entered))
        release[0].set()
        renewed = await first
        release[1].set()
        with pytest.raises(ValueError, match="STALE"):
            await second
        assert resolve(supervisor, renewed) == current.leases[0].scope
    finally:
        for event in release:
            event.set()
        await supervisor.aclose()


def test_batch_rotation_is_atomic_when_one_handle_has_expired():
    clock = [0.0]
    registry = ResourceLeaseRegistry(clock=lambda: clock[0])
    first = registry.issue(scope(), lifetime=10)
    second = registry.issue(scope().model_copy(update={"binding_id": "second"}), lifetime=1)
    clock[0] = 2
    with pytest.raises(LeaseRejected):
        registry.renew_many((first.handle, second.handle))
    assert (
        registry.resolve(
            first.handle,
            operation=first.scope.allowed_operations[0],
            generation_id=first.scope.generation_id,
        )
        == first.scope
    )
    assert set(registry._leases) == {first.handle}


async def test_expiry_during_revalidation_cannot_revive_activation():
    supervisor = ResourceSupervisor("generation-a")
    try:
        current = await supervisor.activate(initialization("https://resources.example.test"))

        async def validate(scopes):
            supervisor._registry._clock = lambda: float("inf")
            return True

        with pytest.raises(LeaseRejected):
            await supervisor.renew(current, revalidate=validate)
        assert (
            await supervisor.runtime_status(agent_id="agent-a", activation_id="activation-a")
            is None
        )
        assert not supervisor._registry._leases
    finally:
        await supervisor.aclose()


async def test_cancelled_revalidation_revokes_before_return():
    supervisor = ResourceSupervisor("generation-a")
    entered = asyncio.Event()
    try:
        current = await supervisor.activate(initialization("https://resources.example.test"))

        async def validate(scopes):
            entered.set()
            await asyncio.Future()

        pending = asyncio.create_task(supervisor.renew(current, revalidate=validate))
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        with pytest.raises(LeaseRejected):
            resolve(supervisor, current)
        assert (
            await supervisor.runtime_status(agent_id="agent-a", activation_id="activation-a")
            is None
        )
    finally:
        await supervisor.aclose()


async def test_expired_activation_releases_worker_without_another_request(monkeypatch):
    supervisor = ResourceSupervisor("generation-a", max_workers=1)
    issue = supervisor._registry.issue
    monkeypatch.setattr(supervisor._registry, "issue", lambda scope: issue(scope, lifetime=1))
    try:
        current = await supervisor.activate(initialization("https://resources.example.test"))
        worker = supervisor._running[current.activation_id].worker
        await asyncio.wait_for(worker.wait_stopped(), timeout=3)
        await asyncio.wait_for(asyncio.gather(*tuple(supervisor._monitors)), timeout=2)
        assert not supervisor._running
        assert not supervisor._broker._activations
        with pytest.raises(LeaseRejected):
            resolve(supervisor, current)
        assert await supervisor.runtime_status(
            agent_id="agent-a", activation_id=current.activation_id
        ) is None
        # Released capacity can serve a new activation; old identity remains fenced.
        from ksadk.resource_runtime.worker import WorkerInitialization

        payload = initialization("https://resources.example.test").pipe_payload()
        payload["scopes"][0]["activationId"] = "new-activation"
        replacement = await supervisor.activate(WorkerInitialization.model_validate(payload))
        assert resolve(supervisor, replacement).activation_id == "new-activation"
    finally:
        await supervisor.aclose()


async def test_renewed_lease_survives_old_deadline_then_reclaims_worker(monkeypatch):
    supervisor = ResourceSupervisor("generation-a")
    issue = supervisor._registry.issue
    monkeypatch.setattr(supervisor._registry, "issue", lambda scope: issue(scope, lifetime=1))
    try:
        current = await supervisor.activate(initialization("https://resources.example.test"))
        worker = supervisor._running[current.activation_id].worker
        await asyncio.sleep(0)  # let the monitor observe the original expiry
        renewed = await supervisor.renew(current, revalidate=allow, lifetime=3)
        await asyncio.sleep(max(0, current.leases[0].expires_at - time.monotonic()) + 0.05)
        assert worker.is_running
        assert resolve(supervisor, renewed) == current.leases[0].scope
        with pytest.raises(LeaseRejected):
            resolve(supervisor, current)
        await asyncio.wait_for(worker.wait_stopped(), timeout=3)
        await asyncio.wait_for(asyncio.gather(*tuple(supervisor._monitors)), timeout=2)
        assert not supervisor._running
    finally:
        await supervisor.aclose()


async def test_shorter_renewal_wakes_existing_long_expiry_wait():
    supervisor = ResourceSupervisor("generation-a")
    try:
        current = await supervisor.activate(initialization("https://resources.example.test"))
        worker = supervisor._running[current.activation_id].worker
        await asyncio.sleep(0)
        await supervisor.renew(current, revalidate=allow, lifetime=1)
        await asyncio.wait_for(worker.wait_stopped(), timeout=3)
        await asyncio.wait_for(asyncio.gather(*tuple(supervisor._monitors)), timeout=2)
        assert not supervisor._running
    finally:
        await supervisor.aclose()


def test_remaining_lifetime_observes_current_handles_without_renewing():
    now = [10.0]
    registry = ResourceLeaseRegistry(clock=lambda: now[0])
    first = registry.issue(scope(), lifetime=1)
    second = registry.issue(scope().model_copy(update={"binding_id": "second"}), lifetime=10)
    handles = (first.handle, second.handle, "missing")
    assert registry.remaining_lifetime(handles) == 10
    now[0] = 12.0
    assert registry.remaining_lifetime(handles) == 8
    renewed = registry.renew(second.handle, lifetime=20)
    assert registry.remaining_lifetime(handles) == 0
    assert registry.remaining_lifetime((renewed.handle,)) == 20
    registry.revoke_binding("activation-a", "second")
    assert registry.remaining_lifetime((renewed.handle,)) == 0
    assert registry.remaining_lifetime(()) == 0
