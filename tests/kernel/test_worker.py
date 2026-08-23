# -*- coding: utf-8 -*-
"""per-session FIFO worker：顺序、active-run 排队、异常分类、并发（Task 6 Step 1）。"""

from __future__ import annotations

import asyncio

import pytest

from ksadk.kernel.errors import (
    AgentKernelError,
    InvalidCommandError,
    StaleFenceError,
)
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


@pytest.mark.parametrize(
    ("requested_model", "approval_mode", "expected_model", "expected_approval"),
    [
        ("qwen3-coder-plus", "ask", "qwen3-coder-plus", "manual"),
        ("not-deployed", "risk", "default-model", "auto_review"),
    ],
)
async def test_turn_model_and_approval_are_bounded_by_deployment_defaults(
    requested_model, approval_mode, expected_model, expected_approval
):
    stack = await kernel_stack()
    lease = await stack.lease()
    await stack.kernel.submit(
        command(
            idempotency_key=f"runtime-options-{approval_mode}",
            payload={
                "content": [{"type": "input_text", "text": "hello"}],
                "runtime_options": {
                    "model": requested_model,
                    "tool_approval_mode": approval_mode,
                    # An untrusted turn must never be able to replace the
                    # deployment-owned sandbox.
                    "sandbox": "full-access",
                },
            },
        ),
        permit=stack.permit("enqueue"),
    )
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(
        stack.store,
        adapter_factory=lambda: stack.adapter,
        start_request_defaults={
            "model": "default-model",
            "allowed_models": ["default-model", "qwen3-coder-plus"],
            "config": {
                "sandbox": "workspace-write",
                "sandbox_read_only": False,
                "approval_mode": "deny_all",
            },
        },
    )

    result = await worker.run_once(AGENT, lease)

    assert result.outcome == "completed"
    request = stack.adapter.start_requests[-1]
    assert request.model == expected_model
    assert request.config["approval_mode"] == expected_approval
    assert request.config["sandbox"] == "workspace-write"
    assert request.config["sandbox_read_only"] is False


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


async def test_same_session_reentrant_ticks_are_serialized():
    """A single owner cannot claim later FIFO rows through concurrent ticks."""

    stack = await kernel_stack()
    stack.adapter.start_delay = 0.05
    lease = await stack.lease("s1", "shared-pod-uid")
    for index in range(5):
        await stack.kernel.submit(
            command(idempotency_key=f"same-{index}", session_id="s1", content=str(index)),
            permit=stack.permit("enqueue", session_id="s1"),
        )
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(stack.store, adapter_factory=lambda: stack.adapter)

    async def tick():
        while True:
            result = await worker.run_once(AGENT, lease, session_id="s1")
            if result.outcome == "completed":
                return
            if result.outcome != "idle":
                raise AssertionError(result)
            await asyncio.sleep(0)

    await asyncio.gather(*(tick() for _ in range(5)))
    messages = await stack.store.list_messages(AGENT, "s1")
    assert [message.accepted_seq for message in messages] == [1, 2, 3, 4, 5]
    assert all(message.status == InboxState.COMPLETED for message in messages)
    assert [call[1] for call in stack.adapter.start_intervals] == sorted(
        call[1] for call in stack.adapter.start_intervals
    )


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


# ------------------------------------------------ Task 7 review P0: stream 消费


def _stream_events(run_id: str):
    from ksadk.events.canonical import RunProgress, RunStarted

    return [
        RunStarted(
            schema_version=2,
            event_id=f"{run_id}-started",
            seq=0,
            timestamp=1780000000.0,
            run_id=run_id,
            scope_id=f"run:{run_id}",
            status="running",
            source=_fake_source(),
        ),
        RunProgress(
            schema_version=2,
            event_id=f"{run_id}-progress",
            seq=0,
            timestamp=1780000000.5,
            run_id=run_id,
            scope_id=f"run:{run_id}",
            status="running",
            progress=0.5,
            source=_fake_source(),
        ),
    ]


def _fake_source():
    from ksadk.events.canonical import SourceRef

    return SourceRef(framework="ksadk")


async def test_enqueue_emits_runtime_event_stream_with_durable_run_id():
    stack = await kernel_stack()
    # adapter 侧 run_id 与 durable run_id 故意不同：事件必须统一用 durable id。
    stack.adapter.handle_run_id = "adapter-run-9"
    stack.adapter.stream_events = _stream_events("adapter-run-9")
    lease = await stack.lease()
    await stack.kernel.submit(
        command(idempotency_key="evt-1"), permit=stack.permit("enqueue")
    )
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(
        stack.store,
        adapter_factory=lambda: stack.adapter,
        session_events=stack.events,
    )
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "completed"
    assert result.run_id not in (None, "adapter-run-9")

    envelopes = await stack.events.read("s1", 0, 50)
    runtime_events = [e for e in envelopes if e.family == "runtime"]
    types = {e.event_type for e in runtime_events}
    # Adapters are allowed to end after progress without emitting their own
    # final fact.  The Kernel owns the public lifecycle and must synthesize a
    # canonical terminal event before marking the durable run completed.
    assert {"run.started", "run.progress", "run.completed"} <= types
    assert all(e.run_id == result.run_id for e in runtime_events)
    assert stack.adapter.streams == ["adapter-run-9"]

    run = await stack.store.load_run(result.run_id)
    assert run is not None and run.state == RunState.COMPLETED
    # 显式记录 durable <-> adapter run id 映射。
    assert run.metadata["runtime_run_id"] == "adapter-run-9"
    # handle + digest 成对持久化，recovery attach 分支可判定。
    handle = RunHandle.model_validate(run.metadata["handle"])
    assert handle.run_id == "adapter-run-9"
    from ksadk.runtime.executor import handle_digest

    assert run.metadata["handle_digest"] == handle_digest(handle)
    assert ("close", "s1") in stack.adapter.calls


async def test_enqueue_uses_deployment_owned_start_request_defaults():
    """Public payload cannot replace manifest-owned model or approval policy."""

    stack = await kernel_stack()
    lease = await stack.lease()
    await stack.kernel.submit(
        command(idempotency_key="manifest-defaults"),
        permit=stack.permit("enqueue"),
    )
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(
        stack.store,
        adapter_factory=lambda: stack.adapter,
        session_events=stack.events,
        start_request_defaults={
            "agent_id": "manifest-agent",
            "model": "manifest-model",
            "config": {
                "approval_mode": "manual",
                "sandbox": "workspace-write",
                "base_instructions": "manifest-owned",
            },
        },
    )
    assert (await worker.run_once(AGENT, lease)).outcome == "completed"
    request = stack.adapter.start_requests[-1]
    assert request.agent_id == "manifest-agent"
    assert request.model == "manifest-model"
    assert request.config == {
        "approval_mode": "manual",
        "sandbox": "workspace-write",
        "base_instructions": "manifest-owned",
    }


async def test_follow_up_enqueue_resumes_native_thread_from_session_log():
    from ksadk.events.canonical import ContinuationCreated
    from ksadk.kernel.worker import AgentKernelWorker

    stack = await kernel_stack()
    stack.adapter.stream_events = [
        ContinuationCreated(
            schema_version=2,
            event_id="continuation-thread-1",
            seq=0,
            timestamp=1780000000.0,
            run_id="adapter-run-1",
            scope_id="thread-scope-1",
            source=_fake_source(),
            continuation_id="continuation-1",
            continuation_kind="thread_resume",
            resumable=True,
            ref={"thread_id": "native-thread-1", "turn_id": "turn-1"},
        )
    ]
    lease = await stack.lease()
    worker = AgentKernelWorker(
        stack.store,
        adapter_factory=lambda: stack.adapter,
        session_events=stack.events,
    )

    await stack.kernel.submit(
        command(idempotency_key="follow-up-first"), permit=stack.permit("enqueue")
    )
    assert (await worker.run_once(AGENT, lease)).outcome == "completed"

    stack.adapter.stream_events = []
    await stack.kernel.submit(
        command(idempotency_key="follow-up-second"), permit=stack.permit("enqueue")
    )
    assert (await worker.run_once(AGENT, lease)).outcome == "completed"
    assert len(stack.adapter.start_requests) == 2
    assert "thread_id" not in stack.adapter.start_requests[0].metadata
    assert stack.adapter.start_requests[1].metadata["thread_id"] == "native-thread-1"


async def test_control_uses_durable_run_id_when_adapter_returns_a_different_id():
    """Adapter 的 runtime run_id 不能让 interrupt 丢失 live handle。"""

    stack = await kernel_stack(
        adapter=FakeAdapter(matrix=matrix_with(cancel=native()))
    )
    stack.adapter.handle_run_id = "runtime-private-run"
    stack.adapter.stream_error = AgentKernelError(
        "persistence_uncertain", "keep run open", retryable=True
    )
    lease = await stack.lease()
    await stack.kernel.submit(
        command(idempotency_key="durable-map-start"), permit=stack.permit("enqueue")
    )
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(stack.store, adapter_factory=lambda: stack.adapter)
    started = await worker.run_once(AGENT, lease)
    assert started.outcome == "retryable_failure"
    active = await stack.store.find_active_run(AGENT, "s1")
    assert active is not None
    assert active.metadata["runtime_run_id"] == "runtime-private-run"

    stack.adapter.stream_error = None
    await stack.kernel.submit(
        command("interrupt", idempotency_key="durable-map-interrupt"),
        permit=stack.permit("interrupt"),
    )
    completed = await worker.run_once(AGENT, lease)
    assert completed.outcome == "completed"
    assert ("cancel", "s1") in stack.adapter.calls


async def test_stream_completes_only_after_natural_end():
    stack = await kernel_stack()
    stack.adapter.stream_events = _stream_events("any")
    lease = await stack.lease()
    await stack.kernel.submit(
        command(idempotency_key="evt-2"), permit=stack.permit("enqueue")
    )
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(
        stack.store,
        adapter_factory=lambda: stack.adapter,
        session_events=stack.events,
    )
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "completed"
    run = await stack.store.load_run(result.run_id)
    assert run is not None and run.state == RunState.COMPLETED


async def test_terminal_event_closes_run_without_waiting_for_provider_eof():
    from ksadk.events.canonical import RunCompleted
    from ksadk.kernel.worker import AgentKernelWorker

    stack = await kernel_stack()
    stack.adapter.stream_events = [
        RunCompleted(
            schema_version=2,
            event_id="provider-terminal-with-open-stream",
            seq=0,
            timestamp=1780000001.0,
            run_id="provider-run",
            scope_id="run:provider-run",
            source=_fake_source(),
            status="completed",
            output_refs=(),
        )
    ]
    stack.adapter.block_after_stream_events = True
    lease = await stack.lease()
    await stack.kernel.submit(
        command(idempotency_key="terminal-open-stream"),
        permit=stack.permit("enqueue"),
    )
    worker = AgentKernelWorker(
        stack.store,
        adapter_factory=lambda: stack.adapter,
        session_events=stack.events,
    )

    result = await worker.run_once(AGENT, lease)
    assert result.run_id is not None

    async def wait_for_terminal() -> None:
        while True:
            run = await stack.store.load_run(result.run_id)
            if run is not None and run.state == RunState.COMPLETED:
                return
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_terminal(), timeout=0.5)
    assert ("close", "s1") in stack.adapter.calls


async def test_stream_retryable_error_keeps_run_open():
    stack = await kernel_stack()
    stack.adapter.stream_error = AgentKernelError(
        "persistence_uncertain", "flush failed", retryable=True
    )
    lease = await stack.lease()
    await stack.kernel.submit(
        command(idempotency_key="evt-3"), permit=stack.permit("enqueue")
    )
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(
        stack.store,
        adapter_factory=lambda: stack.adapter,
        session_events=stack.events,
    )
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "retryable_failure"
    run = await stack.store.find_active_run(AGENT, "s1")
    assert run is not None and run.state == RunState.RUNNING
    for _ in range(10):
        if ("close", "s1") in stack.adapter.calls:
            break
        await asyncio.sleep(0)
    assert ("close", "s1") in stack.adapter.calls
    assert worker.execution_for(result.run_id) is None


async def test_stream_typed_rejection_is_discarded():
    stack = await kernel_stack()
    stack.adapter.stream_error = InvalidCommandError("stream payload invalid")
    lease = await stack.lease()
    await stack.kernel.submit(
        command(idempotency_key="evt-4"), permit=stack.permit("enqueue")
    )
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(
        stack.store,
        adapter_factory=lambda: stack.adapter,
        session_events=stack.events,
    )
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "completed"  # typed rejection 确定性收口
    message = (await stack.store.list_messages(AGENT, "s1"))[0]
    assert message.status == InboxState.DISCARDED
    events = await stack.events.read("s1", 0, 20)
    assert any(e.event_type == "control.command_rejected" for e in events)
