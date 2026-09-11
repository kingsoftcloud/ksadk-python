from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ksadk.events.session_event import SessionServiceEventStore
from ksadk.harness.managed_runtime import managed_harness_capabilities
from ksadk.kernel.bootstrap import AgentKernelRuntime
from ksadk.kernel.errors import InvalidCommandError, StaleFenceError
from ksadk.kernel.memory_store import InMemoryAgentKernelStore
from ksadk.kernel.recovery import RecoveryCoordinator, durable_handle_digest
from ksadk.kernel.state import RunState
from ksadk.kernel.store import ActivationLeaseRequest, RunRecord
from ksadk.runtime import RunHandle
from ksadk.sessions.in_memory import InMemorySessionService


async def two_waiting_runs():
    sessions = InMemorySessionService()
    events = SessionServiceEventStore(sessions)
    store = InMemoryAgentKernelStore(events)
    leases = {}
    for session_id in ("one", "two"):
        await sessions.create_session(agent_id="same-build", user_id="owner", session_id=session_id)
        leases[session_id] = await store.acquire_activation(
            ActivationLeaseRequest(
                agent_instance_id="same-build",
                session_id=session_id,
                activation_id="activation-" + session_id,
                lease_ttl_seconds=60,
            )
        )
        handle = RunHandle(
            run_id="run-" + session_id,
            session_id=session_id,
            runtime_type="harness",
            native_ref={"thread_id": "thread-" + session_id},
        ).model_dump(mode="json")
        record = RunRecord(
            run_id="run-" + session_id,
            session_id=session_id,
            agent_instance_id="same-build",
            state=RunState.PENDING,
            metadata={"handle": handle, "handle_digest": durable_handle_digest(handle)},
        )
        for state in (RunState.PENDING, RunState.RUNNING, RunState.WAITING):
            record = await store.save_run_transition(
                record.model_copy(update={"state": state}), expected_fence=1
            )
    return store, events, leases


@pytest.mark.asyncio
async def test_same_build_recovery_attaches_each_sessions_original_run():
    store, events, leases = await two_waiting_runs()
    restored, adopted = [], []

    async def restore(handle):
        restored.append(handle.run_id)
        return handle

    recovery = RecoveryCoordinator(
        store,
        events,
        lambda: managed_harness_capabilities(durable=True),
        adapter_factory=lambda: SimpleNamespace(durable_restore=restore),
        execution_sink=lambda **values: adopted.append(values["durable_run_id"]),
    )
    for session_id in ("one", "two"):
        report = await recovery.recover("same-build", leases[session_id], session_id=session_id)
        assert report.run_id == "run-" + session_id and report.outcome == "attached"
    assert restored == adopted == ["run-one", "run-two"]
    for session_id in leases:
        facts = await events.read(session_id, 0, 100)
        recovered = [e for e in facts if e.event_type == "control.recovery_decided"]
        assert len(recovered) == 1
        assert recovered[0].payload["activation_id"] == "activation-" + session_id


@pytest.mark.asyncio
async def test_recovery_rejects_another_sessions_run_or_equal_fence_lease():
    store, events, leases = await two_waiting_runs()
    factory = AsyncMock()
    recovery = RecoveryCoordinator(
        store, events, lambda: managed_harness_capabilities(durable=True), adapter_factory=factory
    )
    assert leases["one"].fencing_token == leases["two"].fencing_token
    with pytest.raises(InvalidCommandError, match="recovery scope"):
        await recovery.recover("same-build", leases["one"], run_id="run-two", session_id="one")
    with pytest.raises(StaleFenceError, match="session lease"):
        await recovery.recover("same-build", leases["one"], session_id="two")
    factory.assert_not_called()
    assert (await store.load_run("run-two")).state == RunState.WAITING


@pytest.mark.asyncio
async def test_interrupted_fallback_only_settles_the_requested_session():
    store, events, leases = await two_waiting_runs()
    recovery = RecoveryCoordinator(
        store, events, lambda: managed_harness_capabilities(durable=True)
    )
    with pytest.raises(StaleFenceError, match="session lease"):
        await recovery.settle_interrupted("same-build", leases["one"], session_id="two")
    report = await recovery.settle_interrupted("same-build", leases["two"], session_id="two")
    assert report.run_id == "run-two" and report.outcome == "interrupted"
    assert (await store.load_run("run-one")).state == RunState.WAITING


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_runtime_preserves_session_for_recovery_and_fallback(fails):
    coordinator = SimpleNamespace(
        recover=AsyncMock(side_effect=RuntimeError("fixture") if fails else None),
        settle_interrupted=AsyncMock(),
    )
    runtime = SimpleNamespace(
        config=SimpleNamespace(agent_instance_id="build"), recovery=coordinator
    )
    lease = SimpleNamespace(activation_id="activation-two")
    assert await AgentKernelRuntime._recover_safely(runtime, lease, "two") is None
    coordinator.recover.assert_awaited_once_with("build", lease, session_id="two")
    if fails:
        coordinator.settle_interrupted.assert_awaited_once_with("build", lease, session_id="two")
    else:
        coordinator.settle_interrupted.assert_not_called()
