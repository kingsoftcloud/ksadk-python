# -*- coding: utf-8 -*-
"""Interaction 回包分发（Phase 1 Task 6 Step 2）。

断言 Worker 不再走静态 ``submit_interaction -> adapter.submit`` 空回包映射：
- 权威 InteractionRecord 的绑定 provider 收到完整 response；
- provider 接受后才写 ``interaction.resolved``（同 fence）并恢复 stream 消费；
- provider unavailable / capability 不一致时 typed
  ``runtime_interaction_unavailable`` rejection，Interaction 保持 pending；
- control lookup 永远按 durable run id（adapter 私有 run id 不串号）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from ksadk.interaction.contracts import InteractionRecord
from ksadk.kernel.contracts import ActivationWriteGuard
from ksadk.kernel.errors import AgentKernelError
from ksadk.kernel.state import InboxState, RunState
from ksadk.runtime.adapter import (
    CancelResult,
    PauseResult,
    ResumePayload,
    ResumeTarget,
    RunHandle,
)
from tests.kernel.control_harness import (
    AGENT,
    FakeAdapter,
    command,
    default_matrix,
    kernel_stack,
    native,
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class CodexLikeAdapter(FakeAdapter):
    """runtime_type=codex 的 stub：记录 live submit 的完整 payload。"""

    def __init__(self) -> None:
        super().__init__(
            matrix=default_matrix().model_copy(
                update={
                    "submit_interaction": native(),
                    "resume": native(),
                }
            )
        )
        self.submits: list[tuple[RunHandle, ResumePayload]] = []
        self._live_handle = RunHandle(
            run_id="codex-thread-7", session_id="s1", runtime_type="codex"
        )

    async def start(self, request) -> RunHandle:
        self.calls.append(("start", request.session_id))
        self.streams = []
        return self._live_handle

    async def submit(self, handle: RunHandle, payload: ResumePayload) -> None:
        self.submits.append((handle, payload))


class LangGraphLikeAdapter(FakeAdapter):
    """runtime_type=langgraph 的 stub：记录 durable resume 的 target/payload。"""

    def __init__(self) -> None:
        super().__init__(
            matrix=default_matrix().model_copy(
                update={"resume": native(), "submit_interaction": native()}
            )
        )
        self.resumes: list[tuple[RunHandle, ResumeTarget, ResumePayload | None]] = []
        self.resumed_handle = RunHandle(
            run_id="lg-resumed", session_id="s1", runtime_type="langgraph"
        )

    async def start(self, request) -> RunHandle:
        self.calls.append(("start", request.session_id))
        self.streams = []
        return RunHandle(run_id="lg-run-1", session_id="s1", runtime_type="langgraph")

    async def resume(self, handle, target, payload) -> RunHandle:
        self.resumes.append((handle, target, payload))
        return self.resumed_handle

    async def cancel(self, handle) -> CancelResult:  # pragma: no cover
        return CancelResult.NOT_RUNNING

    async def pause(self, handle) -> PauseResult:  # pragma: no cover
        return PauseResult.NOT_SUPPORTED


class BlockingCodexAdapter(CodexLikeAdapter):
    """A live Codex turn that waits for an approval while its stream stays open."""

    def __init__(self) -> None:
        super().__init__()
        self.interaction_seen = asyncio.Event()
        self.response_received = asyncio.Event()
        self.stream_finished = asyncio.Event()

    def stream(self, handle: RunHandle):
        from ksadk.events.canonical import (
            ApprovalRequest,
            InteractionRequested,
            RunCompleted,
            SourceRef,
        )

        self.streams.append(handle.run_id)

        async def _gen():
            yield InteractionRequested(
                schema_version=2,
                event_id="codex-approval-1",
                seq=0,
                timestamp=1.0,
                run_id=handle.run_id,
                scope_id=f"run:{handle.run_id}",
                source=SourceRef(framework="codex"),
                interaction_id="approval-1",
                interaction_kind="approval",
                request=ApprovalRequest(call_id="call-approval-1", kind="tool"),
            )
            self.interaction_seen.set()
            await self.response_received.wait()
            # Signal that the original blocked generator resumed before it
            # hands the terminal fact to the worker.  The worker deliberately
            # stops requesting frames after a canonical terminal event because
            # app-server transports can keep their channel open across turns;
            # code placed after this yield is therefore not observable.
            self.stream_finished.set()
            yield RunCompleted(
                schema_version=2,
                event_id="codex-completed-1",
                seq=0,
                timestamp=2.0,
                run_id=handle.run_id,
                scope_id=f"run:{handle.run_id}",
                source=SourceRef(framework="codex"),
                status="completed",
                output_refs=(),
            )

        return _gen()

    async def submit(self, handle: RunHandle, payload: ResumePayload) -> None:
        await super().submit(handle, payload)
        self.response_received.set()


class InterruptingApprovalCodexAdapter(BlockingCodexAdapter):
    """Mirror Codex's real approval sequence: request, interrupted, resume."""

    def stream(self, handle: RunHandle):
        from ksadk.events.canonical import (
            ApprovalRequest,
            InteractionRequested,
            RunCompleted,
            RunInterrupted,
            SourceRef,
        )

        self.streams.append(handle.run_id)

        async def _gen():
            source = SourceRef(framework="codex")
            yield InteractionRequested(
                schema_version=2,
                event_id="codex-approval-interrupt-1",
                seq=0,
                timestamp=1.0,
                run_id=handle.run_id,
                scope_id=f"run:{handle.run_id}",
                source=source,
                interaction_id="approval-interrupt-1",
                interaction_kind="approval",
                request=ApprovalRequest(
                    call_id="call-approval-interrupt-1", kind="tool"
                ),
            )
            yield RunInterrupted(
                schema_version=2,
                event_id="codex-run-interrupted-1",
                seq=0,
                timestamp=1.1,
                run_id=handle.run_id,
                scope_id=f"run:{handle.run_id}",
                source=source,
                status="interrupted",
                reason="Codex requires user interaction",
                interaction_id="approval-interrupt-1",
                continuation_id="thread-continuation-1",
            )
            self.interaction_seen.set()
            await self.response_received.wait()
            self.stream_finished.set()
            yield RunCompleted(
                schema_version=2,
                event_id="codex-completed-after-interrupt-1",
                seq=0,
                timestamp=2.0,
                run_id=handle.run_id,
                scope_id=f"run:{handle.run_id}",
                source=source,
                status="completed",
                output_refs=(),
            )

        return _gen()


async def _seed_active_run(stack, adapter):
    """enqueue 一个 run 并让 stream 以 retryable 错误停住（run 保持 RUNNING，
    execution 保留在 worker 内）。返回 durable run id。"""

    adapter.stream_error = AgentKernelError(
        "persistence_uncertain", "keep run open", retryable=True
    )
    lease = await stack.lease()
    await stack.kernel.submit(
        command(idempotency_key="seed-run"), permit=stack.permit("enqueue")
    )
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(
        stack.store,
        adapter_factory=lambda: stack.adapter,
        session_events=stack.events,
    )
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "retryable_failure"
    active = await stack.store.find_active_run(AGENT, "s1")
    assert active is not None and active.state == RunState.RUNNING
    # The same activation owns the live adapter/handle.  A fresh worker has no
    # authority to fabricate an attach; takeover is covered by recovery tests.
    return active.run_id, worker


async def _request_interaction(
    stack,
    lease,
    *,
    interaction_id: str,
    provider_id: str,
    run_id: str,
    native_target: dict,
) -> InteractionRecord:
    record = InteractionRecord(
        interaction_id=interaction_id,
        tenant_id="tenant-1",
        agent_instance_id=AGENT,
        session_id="s1",
        run_id=run_id,
        kind="approval",
        request_schema={"type": "object"},
        created_at=_now_iso(),
        provider_id=provider_id,
        native_target=native_target,
    )
    return await stack.store.request(
        record,
        guard=ActivationWriteGuard(
            activation_id=lease.activation_id, fencing_token=lease.fencing_token
        ),
    )


# ------------------------------------------------------------------ dispatch


async def test_submit_interaction_dispatches_to_bound_provider_with_full_response():
    stack = await kernel_stack(adapter=CodexLikeAdapter())
    adapter = stack.adapter
    lease = await stack.lease()
    run_id, worker = await _seed_active_run(stack, adapter)
    await _request_interaction(
        stack,
        lease,
        interaction_id="it-9",
        provider_id="codex",
        run_id=run_id,
        native_target={"call_id": "call-9", "thread_id": "codex-thread-7"},
    )

    await stack.kernel.submit(
        command(
            "submit_interaction",
            idempotency_key="resolve-1",
            payload={
                "run_id": run_id,
                "interaction_id": "it-9",
                "token_ref": "tok-9",
                "response": {"comment": "looks good", "ttl": "session"},
                "action": "approve",
                "expected_revision": 1,
            },
        ),
        permit=stack.permit("submit_interaction"),
    )
    adapter.stream_error = None  # 回包送达后 stream 自然结束
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "completed"

    # 原生回包到达 activation 持有的同一 adapter：原 call_id + 完整 response。
    assert len(adapter.submits) == 1
    handle, payload = adapter.submits[0]
    assert handle.run_id == "codex-thread-7"
    assert payload.call_id == "call-9"
    assert payload.kind == "approval_decision"
    assert payload.data["comment"] == "looks good"
    assert payload.data["decision"] == "approve"

    # provider 接受后才写 InteractionResolved，同 fence 恢复 stream 消费。
    record = await stack.store.get("it-9")
    assert record is not None and record.status == "resolved"
    events = await stack.events.read("s1", 0, 200)
    resolved = [e for e in events if e.event_type == "interaction.resolved"]
    assert len(resolved) == 1 and resolved[0].run_id == run_id
    assert len(adapter.streams) >= 1  # 回包后 stream 继续被消费


async def test_submit_interaction_rejects_record_provider_mismatched_to_active_execution():
    """回包只能交给 activation 当前持有的 framework provider。

    InteractionRecord 是 durable state，但它不能成为跨 framework dispatch 的
    授权：恢复/迁移时若 record 的 provider 与当前 ActiveExecution 不一致，
    Worker 必须拒绝，而不能以 Codex handle 去调用 LangGraph resume（或反过来）。
    """

    stack = await kernel_stack(adapter=CodexLikeAdapter())
    adapter = stack.adapter
    lease = await stack.lease()
    run_id, worker = await _seed_active_run(stack, adapter)
    await _request_interaction(
        stack,
        lease,
        interaction_id="it-provider-mismatch",
        provider_id="langgraph",
        run_id=run_id,
        native_target={"checkpoint_id": "ckpt-1", "thread_id": "lg-thread"},
    )

    await stack.kernel.submit(
        command(
            "submit_interaction",
            idempotency_key="resolve-provider-mismatch",
            payload={
                "run_id": run_id,
                "interaction_id": "it-provider-mismatch",
                "token_ref": "tok-provider-mismatch",
                "response": {"approved": True},
                "action": "approve",
                "expected_revision": 1,
            },
        ),
        permit=stack.permit("submit_interaction"),
    )

    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "completed"
    assert adapter.submits == []
    record = await stack.store.get("it-provider-mismatch")
    assert record is not None and record.status == "pending"
    events = await stack.events.read("s1", 0, 200)
    rejected = [e for e in events if e.event_type == "control.command_rejected"]
    assert any(
        e.payload["reason"] == "runtime_interaction_unavailable" for e in rejected
    )


async def test_unavailable_provider_is_typed_rejection_and_keeps_interaction_pending():
    stack = await kernel_stack(adapter=LangGraphLikeAdapter())
    adapter = stack.adapter
    lease = await stack.lease()
    run_id, worker = await _seed_active_run(stack, adapter)
    # ADK provider 诚实 unavailable：生产 adapter 无法保留 native 身份。
    await _request_interaction(
        stack,
        lease,
        interaction_id="it-adk",
        provider_id="adk",
        run_id=run_id,
        native_target={"invocation_id": "inv-1"},
    )

    await stack.kernel.submit(
        command(
            "submit_interaction",
            idempotency_key="resolve-adk",
            payload={
                "run_id": run_id,
                "interaction_id": "it-adk",
                "token_ref": "tok-adk",
                "response": {"ok": True},
            },
        ),
        permit=stack.permit("submit_interaction"),
    )
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "completed"  # typed rejection 确定性收口

    message = next(
        message
        for message in await stack.store.list_messages(AGENT, "s1")
        if message.idempotency_key == "resolve-adk"
    )
    assert message.status == InboxState.DISCARDED
    events = await stack.events.read("s1", 0, 200)
    rejected = [
        e for e in events if e.event_type == "control.command_rejected"
    ]
    assert any(e.payload["reason"] == "runtime_interaction_unavailable" for e in rejected)
    # Interaction 绝不被标 resolved。
    record = await stack.store.get("it-adk")
    assert record is not None and record.status == "pending"
    assert not [e for e in events if e.event_type == "interaction.resolved"]
    # adapter 没有收到任何回包 / resume。
    assert adapter.resumes == []


async def test_capability_mismatch_is_typed_rejection_not_silent_replay():
    """provider mode 声明与 adapter 真实 capability 不一致时 fail closed。

    langgraph provider 需要 ``resume``（durable_resume）；该 stub 声明
    submit_interaction 可用（ingress 放行）但 resume 不可用——provider 必须
    typed 拒绝，而不是退化为 submit/replay。
    """

    class NoResumeAdapter(LangGraphLikeAdapter):
        def __init__(self) -> None:
            super().__init__()
            self._matrix = default_matrix().model_copy(
                update={"submit_interaction": native()}
            )

    stack = await kernel_stack(adapter=NoResumeAdapter())
    adapter = stack.adapter
    lease = await stack.lease()
    run_id, worker = await _seed_active_run(stack, adapter)
    await _request_interaction(
        stack,
        lease,
        interaction_id="it-cap",
        provider_id="langgraph",
        run_id=run_id,
        native_target={"checkpoint_id": "ckpt-1", "call_id": "call-cap"},
    )

    await stack.kernel.submit(
        command(
            "submit_interaction",
            idempotency_key="resolve-cap",
            payload={
                "run_id": run_id,
                "interaction_id": "it-cap",
                "token_ref": "tok-cap",
                "response": {"ok": True},
            },
        ),
        permit=stack.permit("submit_interaction"),
    )
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "completed"
    events = await stack.events.read("s1", 0, 200)
    rejected = [e for e in events if e.event_type == "control.command_rejected"]
    assert any(
        e.payload["reason"] == "runtime_interaction_unavailable" for e in rejected
    )
    record = await stack.store.get("it-cap")
    assert record is not None and record.status == "pending"
    assert adapter.resumes == []


async def test_unknown_interaction_is_typed_rejection():
    stack = await kernel_stack(adapter=CodexLikeAdapter())
    adapter = stack.adapter
    lease = await stack.lease()
    run_id, worker = await _seed_active_run(stack, adapter)

    await stack.kernel.submit(
        command(
            "submit_interaction",
            idempotency_key="resolve-unknown",
            payload={
                "run_id": run_id,
                "interaction_id": "it-missing",
                "token_ref": "tok-x",
                "response": {"ok": True},
            },
        ),
        permit=stack.permit("submit_interaction"),
    )
    result = await worker.run_once(AGENT, lease)
    assert result.outcome == "completed"
    assert adapter.submits == []
    message = next(
        message
        for message in await stack.store.list_messages(AGENT, "s1")
        if message.idempotency_key == "resolve-unknown"
    )
    assert message.status == InboxState.DISCARDED


async def test_live_interaction_does_not_block_worker_and_resumes_the_same_stream():
    """A blocked native stream must not prevent its matching response command.

    The interaction event is persisted once through InteractionLedger, then
    the worker acknowledges the enqueue and returns to polling.  The later
    response reaches the same Codex adapter/handle and lets the existing
    stream emit its terminal event.
    """

    stack = await kernel_stack(adapter=BlockingCodexAdapter())
    adapter = stack.adapter
    lease = await stack.lease()
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(
        stack.store,
        adapter_factory=lambda: adapter,
        session_events=stack.events,
    )
    await stack.kernel.submit(
        command(idempotency_key="live-interaction-start"),
        permit=stack.permit("enqueue"),
    )
    start_task = asyncio.create_task(worker.run_once(AGENT, lease))
    await asyncio.wait_for(adapter.interaction_seen.wait(), timeout=0.2)
    # Regression target: before the background-stream change, this task waits
    # here forever and no submit_interaction command can be consumed.
    started = await asyncio.wait_for(start_task, timeout=0.2)
    assert started.outcome == "completed"
    assert started.run_id is not None

    record = await stack.store.get(
        "approval-1",
        tenant_id="tenant-1",
        agent_instance_id=AGENT,
        session_id="s1",
        run_id=started.run_id,
    )
    assert record is not None
    assert record.status == "pending"
    assert record.provider_id == "codex"
    assert record.native_target == {"call_id": "call-approval-1"}

    await stack.kernel.submit(
        command(
            "submit_interaction",
            idempotency_key="live-interaction-response",
            payload={
                "run_id": started.run_id,
                "interaction_id": "approval-1",
                "token_ref": "server-permit-ref",
                "response": {"comment": "approved"},
                "action": "approve",
                "expected_revision": 1,
            },
        ),
        permit=stack.permit("submit_interaction"),
    )
    resolved = await worker.run_once(AGENT, lease)
    assert resolved.outcome == "completed"
    await asyncio.wait_for(adapter.stream_finished.wait(), timeout=0.2)

    stored = await stack.store.get(
        "approval-1",
        tenant_id="tenant-1",
        agent_instance_id=AGENT,
        session_id="s1",
        run_id=started.run_id,
    )
    assert stored is not None and stored.status == "resolved"
    run = await stack.store.load_run(started.run_id)
    assert run is not None and run.state == RunState.COMPLETED
    events = await stack.events.read("s1", 0, 100)
    assert [
        event.event_type for event in events if event.family == "interaction"
    ] == ["interaction.requested", "interaction.resolved"]
    assert not [
        event
        for event in events
        if event.family == "runtime" and event.event_type == "interaction.requested"
    ]


async def test_interaction_interruption_keeps_waiting_run_and_live_execution():
    """A native approval interruption is a wait marker, not a terminal run.

    Codex emits ``InteractionRequested`` immediately followed by
    ``RunInterrupted`` before its JSON-RPC callback blocks.  Production has
    enough scheduling latency for both events to be consumed before the user
    can answer, so the worker must keep the durable run WAITING and preserve
    the same process-local adapter/handle for the later response.
    """

    stack = await kernel_stack(adapter=InterruptingApprovalCodexAdapter())
    adapter = stack.adapter
    lease = await stack.lease()
    from ksadk.kernel.worker import AgentKernelWorker

    worker = AgentKernelWorker(
        stack.store,
        adapter_factory=lambda: adapter,
        session_events=stack.events,
    )
    await stack.kernel.submit(
        command(idempotency_key="interrupting-interaction-start"),
        permit=stack.permit("enqueue"),
    )
    started = await worker.run_once(AGENT, lease)
    assert started.run_id is not None

    # The adapter has advanced past its companion RunInterrupted frame and is
    # now genuinely blocked on the native approval callback.
    await asyncio.wait_for(adapter.interaction_seen.wait(), timeout=0.2)

    run = await stack.store.load_run(started.run_id)
    assert run is not None and run.state == RunState.WAITING
    assert worker.execution_for(started.run_id) is not None
    assert ("close", "s1") not in adapter.calls
    events = await stack.events.read("s1", 0, 100)
    assert not any(event.event_type == "run.interrupted" for event in events)

    await stack.kernel.submit(
        command(
            "submit_interaction",
            idempotency_key="interrupting-interaction-response",
            payload={
                "run_id": started.run_id,
                "interaction_id": "approval-interrupt-1",
                "token_ref": "server-permit-ref",
                "response": {"decision": "approve"},
                "action": "approve",
                "expected_revision": 1,
            },
        ),
        permit=stack.permit("submit_interaction"),
    )
    resolved = await worker.run_once(AGENT, lease)
    assert resolved.outcome == "completed"
    await asyncio.wait_for(adapter.stream_finished.wait(), timeout=0.2)

    for _ in range(20):
        run = await stack.store.load_run(started.run_id)
        if run is not None and run.state == RunState.COMPLETED:
            break
        await asyncio.sleep(0)
    assert run is not None and run.state == RunState.COMPLETED
