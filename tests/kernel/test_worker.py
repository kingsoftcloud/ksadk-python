# -*- coding: utf-8 -*-
"""per-session FIFO worker：顺序、active-run 排队、异常分类、并发（Task 6 Step 1）。"""

from __future__ import annotations

import asyncio

import pytest

from ksadk.kernel.errors import AgentKernelError, StaleFenceError
from ksadk.kernel.state import InboxState, RunState
from ksadk.kernel.store import RunRecord
from ksadk.runtime.adapter import RunHandle
from tests.kernel.control_harness import (
    AGENT,
    FakeAdapter,
    command,
    default_matrix,
    kernel_stack,
    native,
)


def matrix_with(**supported) -> object:
    update = {name: native() for name in supported}
    return default_matrix().model_copy(update=update)


async def drain(stack, lease, session_id: str = "s1", limit: int = 50):
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(stack.store, adapter_factory=lambda: stack.adapter)
    results = []
    for _ in range(limit):
        result = await worker.run_once(AGENT, lease)
        results.append(result)
        if result.outcome == "idle":
            break
    return results, worker


async def test_fifo_order_is_stable_within_session():
    stack = await kernel_stack()
    lease = await stack.lease()
    for index, text in enumerate(["first", "second", "third"]):
        await stack.kernel.submit(
            command(idempotency_key=f"f{index}", content=text),
            permit=stack.permit("enqueue"),
        )
    results, _ = await drain(stack, lease)
    assert [r.outcome for r in results] == ["completed", "completed", "completed", "idle"]
    starts = [c for c in stack.adapter.calls if c[0] == "start"]
    assert len(starts) == 3
    for message in await stack.store.list_messages(AGENT, "s1"):
        assert message.status == InboxState.COMPLETED


async def test_enqueue_stays_queued_while_run_is_active():
    stack = await kernel_stack()
    lease = await stack.lease()
    # 直接在 store 里放一个 active run。
    run = await stack.store.save_run_transition(
        RunRecord(
            run_id="run-active",
            agent_instance_id=AGENT,
            session_id="s1",
            state=RunState.PENDING,
        ),
        expected_fence=lease.fencing_token,
    )
    run = await stack.store.save_run_transition(
        run.model_copy(update={"state": RunState.RUNNING}),
        expected_fence=lease.fencing_token,
    )
    await stack.kernel.submit(
        command(idempotency_key="wait-1"), permit=stack.permit("enqueue")
    )
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(stack.store, adapter_factory=lambda: stack.adapter)
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "idle"
    pending = await stack.store.list_messages(AGENT, "s1")
    assert [m.idempotency_key for m in pending] == ["wait-1"]
    assert pending[0].status == InboxState.ACCEPTED
    # run 终结后 enqueue 才被执行。
    await stack.store.save_run_transition(
        run.model_copy(update={"state": RunState.COMPLETED}),
        expected_fence=lease.fencing_token,
    )
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "completed"
    assert stack.adapter.calls.count(("start", "s1")) == 1


async def test_control_verb_acts_on_active_run_with_live_handle():
    stack = await kernel_stack(
        adapter=FakeAdapter(matrix=matrix_with(cancel=native()))
    )
    lease = await stack.lease()
    seeded = await stack.store.save_run_transition(
        RunRecord(
            run_id="run-x",
            agent_instance_id=AGENT,
            session_id="s1",
            state=RunState.PENDING,
        ),
        expected_fence=lease.fencing_token,
    )
    await stack.store.save_run_transition(
        seeded.model_copy(update={"state": RunState.RUNNING}),
        expected_fence=lease.fencing_token,
    )
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(stack.store, adapter_factory=lambda: stack.adapter)
    worker.attach_handle("run-x", RunHandle(run_id="run-x", session_id="s1", runtime_type="fake"))
    await stack.kernel.submit(
        command("interrupt", idempotency_key="stop-1"), permit=stack.permit("interrupt")
    )
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "completed"
    assert ("cancel", "s1") in stack.adapter.calls
    run = await stack.store.load_run("run-x")
    assert run.state == RunState.CANCELLED


async def test_control_verb_without_active_run_is_typed_rejection():
    stack = await kernel_stack(
        adapter=FakeAdapter(matrix=matrix_with(cancel=native()))
    )
    lease = await stack.lease()
    await stack.kernel.submit(
        command("interrupt", idempotency_key="stop-2"), permit=stack.permit("interrupt")
    )
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(stack.store, adapter_factory=lambda: stack.adapter)
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "completed"  # typed rejection 是确定性收口
    message = (await stack.store.list_messages(AGENT, "s1"))[0]
    assert message.status == InboxState.DISCARDED
    events = await stack.events.read("s1", 0, 20)
    assert any(e.event_type == "control.command_rejected" for e in events)


async def test_retryable_failure_keeps_claim_open():
    stack = await kernel_stack()
    lease = await stack.lease()
    stack.adapter.start_error = AgentKernelError(
        "persistence_uncertain", "flush failed", retryable=True
    )
    await stack.kernel.submit(
        command(idempotency_key="retry-1"), permit=stack.permit("enqueue")
    )
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(stack.store, adapter_factory=lambda: stack.adapter)
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "retryable_failure"
    message = (await stack.store.list_messages(AGENT, "s1"))[0]
    assert message.status == InboxState.CLAIMED
    # 恢复后可以重试成功（同 owner 对自己的 claimed 消息可重入）。
    stack.adapter.start_error = None
    recovered = await worker.run_once(AGENT, lease)
    assert recovered.outcome == "completed"
    assert recovered.message_id == message.message_id


async def test_unknown_exception_is_terminal_and_never_acked():
    stack = await kernel_stack()
    lease = await stack.lease()
    stack.adapter.start_error = RuntimeError("boom")
    await stack.kernel.submit(
        command(idempotency_key="bad-1"), permit=stack.permit("enqueue")
    )
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(stack.store, adapter_factory=lambda: stack.adapter)
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "terminal_failure"
    message = (await stack.store.list_messages(AGENT, "s1"))[0]
    assert message.status == InboxState.CLAIMED  # 未被 ack 为成功


async def test_distinct_sessions_overlap_and_keep_per_session_order():
    stack = await kernel_stack()
    stack.adapter.start_delay = 0.1
    lease1 = await stack.lease("s1", "act-s1")
    lease2 = await stack.lease("s2", "act-s2")
    for index in range(2):
        await stack.kernel.submit(
            command(idempotency_key=f"s1-{index}", session_id="s1", content=f"a{index}"),
            permit=stack.permit("enqueue", session_id="s1"),
        )
        await stack.kernel.submit(
            command(idempotency_key=f"s2-{index}", session_id="s2", content=f"b{index}"),
            permit=stack.permit("enqueue", session_id="s2"),
        )
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(stack.store, adapter_factory=lambda: stack.adapter)

    async def drive(session_lease):
        completed = 0
        while completed < 2:
            result = await worker.run_once(AGENT, session_lease)
            if result.outcome in ("completed", "claimed"):
                completed += 1
            elif result.outcome != "idle":
                raise AssertionError(result)
        return completed

    await asyncio.gather(drive(lease1), drive(lease2))

    intervals = sorted(stack.adapter.start_intervals, key=lambda item: item[1])
    assert len(intervals) == 4
    per_session = {}
    for session, entered, exited in intervals:
        per_session.setdefault(session, []).append((entered, exited))
    for session, spans in per_session.items():
        # 同 session 顺序稳定：无重叠且按时间递增。
        assert spans[0][1] <= spans[1][0], session
    # 不同 session 至少两个 adapter start 调用重叠。
    s1 = per_session["s1"][0]
    s2 = per_session["s2"][0]
    assert s1[0] < s2[1] and s2[0] < s1[1]


async def test_worker_requires_lease_to_claim():
    stack = await kernel_stack()
    await stack.kernel.submit(
        command(idempotency_key="lease-1"), permit=stack.permit("enqueue")
    )
    stale = await stack.lease("s1", "act-stale")
    from ksadk.kernel.contracts import ActivationLease
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(stack.store, adapter_factory=lambda: stack.adapter)
    forged = ActivationLease(
        agent_instance_id=AGENT,
        activation_id="act-stale",
        fencing_token=stale.fencing_token + 99,
        lease_expires_at=stale.lease_expires_at,
        bundle_digest=stale.bundle_digest,
        runtime_type=stale.runtime_type,
        capability_digest=stale.capability_digest,
    )
    with pytest.raises(StaleFenceError):
        await worker.run_once(AGENT, forged)
