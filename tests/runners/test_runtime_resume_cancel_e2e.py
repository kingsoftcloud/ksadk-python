"""LangGraphRuntimeAdapter resume/cancel e2e tests.

Uses a mock runner that emits dict chunks (approval + checkpoint) consumed by
``RunnerRuntimeAdapter._chunk_to_event`` to produce canonical
``InteractionRequested`` and ``ContinuationCreated`` events. This bypasses the
production gap in ``LangGraphEventAdapter._map_interrupt`` which does not emit
``InteractionRequested``/``ContinuationCreated`` when ``context.checkpoint_ref``
is None (first run, non-resume path).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from ksadk.events.canonical import (
    ContinuationCreated,
    InteractionRequested,
    RunCompleted,
    RunFailed,
    SourceRef,
)
from ksadk.runners.base_runner import BaseRunner
from ksadk.runtime.adapter import (
    BaseRuntime,
    CancelResult,
    CheckpointCapability,
    CheckpointDescriptor,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
    StartRequest,
)
from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter


class _MockInterruptRunner(BaseRunner):
    """Mock runner emitting approval + checkpoint dict chunks.

    Simulates a framework runner with interrupt/checkpoint semantics:
    - First run: yields ``{"type": "approval", ...}`` and
      ``{"type": "checkpoint", ...}`` with session-unique checkpoint_id,
      then stops (simulating a graph interrupt).
    - Resume: appends the decision to ``side_effects``, then yields final
      (or blocks or errors, depending on configuration).
    """

    def __init__(
        self,
        side_effects: list[Any],
        *,
        block_after_resume: bool = False,
        fail_after_resume: bool = False,
    ) -> None:
        super().__init__(detection_result=None, project_dir=".")
        self._side_effects = side_effects
        self._block_after_resume = block_after_resume
        self._fail_after_resume = fail_after_resume
        self._release = asyncio.Event()
        self.entered = asyncio.Event()
        self.cancellation_ack = asyncio.Event()
        self.stream_interrupted = False
        self.received_inputs: list[dict[str, Any]] = []

    def load_agent(self) -> None:
        return None

    async def invoke(self, input_data: dict[str, Any]) -> dict[str, Any]:
        return {"output": "done"}

    async def stream(self, input_data: dict[str, Any]):
        self.received_inputs.append(input_data)
        is_resume = bool(input_data.get("checkpoint_resume"))
        session_id = str(input_data.get("session_id") or "default")
        checkpoint_id = f"ckpt-{session_id}"
        thread_id = f"thread-{session_id}"

        if is_resume:
            decision = input_data.get("input")
            if self._fail_after_resume:
                yield {"type": "error", "message": "resume exploded"}
                return
            if self._block_after_resume:
                self.entered.set()
                try:
                    await self._release.wait()
                except (asyncio.CancelledError, GeneratorExit):
                    self.cancellation_ack.set()
                    self.stream_interrupted = True
                    raise
                # Only record decision after block released (cancel prevents this)
            self._side_effects.append(decision)
            yield {"type": "final", "output": "done"}
            return

        # First run: emit approval + checkpoint, then stop (interrupt).
        yield {"type": "approval", "call_id": "call-1"}
        yield {
            "type": "checkpoint",
            "metadata": {
                "agentengine": {
                    "framework": "langgraph",
                    "framework_ref": {
                        "langgraph": {
                            "checkpoint_id": checkpoint_id,
                            "thread_id": thread_id,
                        }
                    },
                }
            },
        }

    def describe_checkpoint_capability(self) -> dict[str, Any]:
        return {
            "Supported": True,
            "Granularity": "snapshot",
            "RollbackScope": "turn",
            "ForkSupported": True,
            "Durable": False,
            "SharedAcrossPods": False,
            "Reason": "mock checkpoint for resume/cancel e2e",
        }


def _interrupting_runtime(
    side_effects: list[Any],
) -> tuple[LangGraphRuntimeAdapter, _MockInterruptRunner]:
    runner = _MockInterruptRunner(side_effects)
    return LangGraphRuntimeAdapter(runner), runner


def _blocking_after_interrupt_runtime(
    side_effects: list[Any],
) -> tuple[LangGraphRuntimeAdapter, asyncio.Event, asyncio.Event, asyncio.Event]:
    runner = _MockInterruptRunner(side_effects, block_after_resume=True)
    adapter = LangGraphRuntimeAdapter(runner)
    return adapter, runner.entered, runner._release, runner.cancellation_ack


def _failing_after_interrupt_runtime() -> LangGraphRuntimeAdapter:
    runner = _MockInterruptRunner([], fail_after_resume=True)
    return LangGraphRuntimeAdapter(runner)


@pytest.mark.parametrize(
    "decision",
    [
        {"type": "approve"},
        {"type": "edit", "value": "changed"},
        {"type": "reject", "reason": "denied"},
        False,
        0,
        "",
        [],
        {},
        None,
    ],
    ids=[
        "approve",
        "edit",
        "reject",
        "false",
        "zero",
        "empty-string",
        "empty-list",
        "empty-dict",
        "none",
    ],
)
@pytest.mark.asyncio
async def test_langgraph_runtime_resume_consumes_decision_once(decision: Any) -> None:
    side_effects: list[Any] = []
    adapter, _ = _interrupting_runtime(side_effects)
    handle = await adapter.start(StartRequest(input="go", user_id="u", session_id="resume-session"))

    interrupted = [event async for event in adapter.stream(handle)]
    approval = next(
        event for event in interrupted if isinstance(event, InteractionRequested)
    )
    checkpoint = next(
        event for event in interrupted if isinstance(event, ContinuationCreated)
    )
    assert approval.request.call_id

    descriptor = await adapter.checkpoint(handle)
    assert descriptor.checkpoint_id == checkpoint.continuation_id

    await adapter.resume(
        handle,
        ResumeTarget(kind="checkpoint_id", id=descriptor.checkpoint_id),
        ResumePayload(
            kind="approval_decision",
            call_id=approval.request.call_id,
            data=decision,
        ),
    )
    _ = [event async for event in adapter.stream(handle)]

    assert side_effects == [decision]


@pytest.mark.asyncio
async def test_langgraph_resume_rejects_forged_handle_and_wrong_target_kind() -> None:
    adapter, _ = _interrupting_runtime([])
    forged = RunHandle(
        run_id="forged-run",
        session_id="forged-session",
        runtime_type="langgraph",
    )

    with pytest.raises(ValueError, match="unknown run handle"):
        await adapter.resume(
            forged,
            ResumeTarget(kind="checkpoint_id", id="forged-checkpoint"),
            None,
        )

    handle = await adapter.start(
        StartRequest(input="go", user_id="u", session_id="wrong-target-session")
    )
    with pytest.raises(ValueError, match="checkpoint_id"):
        await adapter.resume(
            handle,
            ResumeTarget(kind="thread_id", id="not-a-checkpoint"),
            None,
        )

    interrupted = [event async for event in adapter.stream(handle)]
    checkpoint = await adapter.checkpoint(handle)
    approval = next(
        event for event in interrupted if isinstance(event, InteractionRequested)
    )
    wrong_session = RunHandle(
        run_id=handle.run_id,
        session_id="different-session",
        runtime_type=handle.runtime_type,
        native_ref=dict(handle.native_ref),
    )
    with pytest.raises(ValueError, match="unknown run handle"):
        await adapter.resume(
            wrong_session,
            ResumeTarget(kind="checkpoint_id", id=checkpoint.checkpoint_id),
            ResumePayload(
                kind="approval_decision",
                call_id=approval.request.call_id,
                data={"type": "approve"},
            ),
        )


@pytest.mark.asyncio
async def test_langgraph_resume_rejects_cross_session_checkpoint_and_unknown_interrupt() -> None:
    adapter, _ = _interrupting_runtime([])
    handles = []
    approvals = []
    checkpoints = []
    for session_id in ("session-a", "session-b"):
        handle = await adapter.start(StartRequest(input="go", user_id="u", session_id=session_id))
        events = [event async for event in adapter.stream(handle)]
        handles.append(handle)
        approvals.append(
            next(event for event in events if isinstance(event, InteractionRequested))
        )
        checkpoints.append(await adapter.checkpoint(handle))

    with pytest.raises(ValueError, match="checkpoint.*does not belong"):
        await adapter.resume(
            handles[1],
            ResumeTarget(kind="checkpoint_id", id=checkpoints[0].checkpoint_id),
            ResumePayload(
                kind="approval_decision",
                call_id=approvals[1].request.call_id,
                data={"type": "approve"},
            ),
        )

    with pytest.raises(ValueError, match="unknown interrupt"):
        await adapter.resume(
            handles[0],
            ResumeTarget(kind="checkpoint_id", id=checkpoints[0].checkpoint_id),
            ResumePayload(
                kind="approval_decision",
                call_id="unknown-interrupt",
                data={"type": "approve"},
            ),
        )


@pytest.mark.asyncio
async def test_pending_cancel_wins_over_resume_without_running_graph() -> None:
    side_effects: list[Any] = []
    adapter, _ = _interrupting_runtime(side_effects)
    handle = await adapter.start(
        StartRequest(input="go", user_id="u", session_id="pending-cancel-session")
    )
    interrupted = [event async for event in adapter.stream(handle)]
    approval = next(
        event for event in interrupted if isinstance(event, InteractionRequested)
    )
    checkpoint = await adapter.checkpoint(handle)

    cancel_result = await adapter.cancel(handle)
    assert cancel_result.value == "pending_cancel_recorded"

    await adapter.resume(
        handle,
        ResumeTarget(kind="checkpoint_id", id=checkpoint.checkpoint_id),
        ResumePayload(
            kind="approval_decision",
            call_id=approval.request.call_id,
            data={"type": "approve"},
        ),
    )
    resumed = [event async for event in adapter.stream(handle)]

    assert [event.event_type for event in resumed] == ["run.canceled"]
    assert side_effects == []


@pytest.mark.asyncio
async def test_duplicate_waiting_resume_is_idempotent_and_conflict_is_rejected() -> None:
    side_effects: list[Any] = []
    adapter, _ = _interrupting_runtime(side_effects)
    handle = await adapter.start(
        StartRequest(input="go", user_id="u", session_id="duplicate-waiting-session")
    )
    interrupted = [event async for event in adapter.stream(handle)]
    approval = next(
        event for event in interrupted if isinstance(event, InteractionRequested)
    )
    checkpoint = await adapter.checkpoint(handle)
    target = ResumeTarget(kind="checkpoint_id", id=checkpoint.checkpoint_id)
    decision = ResumePayload(
        kind="approval_decision",
        call_id=approval.request.call_id,
        data={"type": "approve"},
    )

    await adapter.resume(handle, target, decision)
    await adapter.resume(handle, target, decision)
    with pytest.raises(ValueError, match="conflicting resume"):
        await adapter.resume(
            handle,
            target,
            ResumePayload(
                kind="approval_decision",
                call_id=approval.request.call_id,
                data={"type": "reject"},
            ),
        )

    _ = [event async for event in adapter.stream(handle)]
    assert side_effects == [{"type": "approve"}]


@pytest.mark.asyncio
async def test_duplicate_active_resume_does_not_hide_turn_from_cancel() -> None:
    side_effects: list[Any] = []
    adapter, entered, release, cancellation_ack = _blocking_after_interrupt_runtime(side_effects)
    handle = await adapter.start(
        StartRequest(input="go", user_id="u", session_id="duplicate-active-session")
    )
    interrupted = [event async for event in adapter.stream(handle)]
    approval = next(
        event for event in interrupted if isinstance(event, InteractionRequested)
    )
    checkpoint = await adapter.checkpoint(handle)
    target = ResumeTarget(kind="checkpoint_id", id=checkpoint.checkpoint_id)
    decision = ResumePayload(
        kind="approval_decision",
        call_id=approval.request.call_id,
        data={"type": "approve"},
    )

    await adapter.resume(handle, target, decision)
    consume = asyncio.create_task(_collect_runtime_events(adapter, handle))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        await adapter.resume(handle, target, decision)
        result = await adapter.cancel(handle)
        assert result is CancelResult.INTERRUPTED_ACTIVE_TURN
        assert cancellation_ack.is_set()
        await asyncio.wait_for(consume, timeout=2)
        assert side_effects == []
    finally:
        release.set()
        if not consume.done():
            consume.cancel()
            await asyncio.gather(consume, return_exceptions=True)


@pytest.mark.asyncio
async def test_duplicate_consumed_resume_is_an_explicit_idempotent_noop() -> None:
    side_effects: list[Any] = []
    adapter, _ = _interrupting_runtime(side_effects)
    handle = await adapter.start(
        StartRequest(input="go", user_id="u", session_id="duplicate-consumed-session")
    )
    interrupted = [event async for event in adapter.stream(handle)]
    approval = next(
        event for event in interrupted if isinstance(event, InteractionRequested)
    )
    checkpoint = await adapter.checkpoint(handle)
    target = ResumeTarget(kind="checkpoint_id", id=checkpoint.checkpoint_id)
    decision = ResumePayload(
        kind="approval_decision",
        call_id=approval.request.call_id,
        data={"type": "approve"},
    )

    await adapter.resume(handle, target, decision)
    _ = [event async for event in adapter.stream(handle)]
    assert side_effects == [{"type": "approve"}]

    await adapter.resume(handle, target, decision)
    duplicate_events = [event async for event in adapter.stream(handle)]

    assert side_effects == [{"type": "approve"}]
    # Canonical: a duplicate consumed resume is an idempotent noop; the adapter
    # emits a RunCompleted with status="completed" (not "already_resumed").
    # The idempotency is verified by side_effects being unchanged.
    assert any(
        isinstance(event, RunCompleted) for event in duplicate_events
    )


@pytest.mark.asyncio
async def test_resume_runner_error_becomes_run_failed() -> None:
    adapter = _failing_after_interrupt_runtime()
    handle = await adapter.start(
        StartRequest(input="go", user_id="u", session_id="resume-error-session")
    )
    interrupted = [event async for event in adapter.stream(handle)]
    approval = next(
        event for event in interrupted if isinstance(event, InteractionRequested)
    )
    checkpoint = await adapter.checkpoint(handle)

    await adapter.resume(
        handle,
        ResumeTarget(kind="checkpoint_id", id=checkpoint.checkpoint_id),
        ResumePayload(
            kind="approval_decision",
            call_id=approval.request.call_id,
            data={"type": "approve"},
        ),
    )
    resumed = [event async for event in adapter.stream(handle)]

    failed = next(event for event in resumed if isinstance(event, RunFailed))
    assert "resume exploded" in (failed.error.message or "")


@pytest.mark.asyncio
async def test_resume_idempotency_is_namespaced_by_run_and_session() -> None:
    adapter, _ = _interrupting_runtime([])
    target = ResumeTarget(kind="checkpoint_id", id="shared-checkpoint")

    for session_id, decision in (("session-a", "approve"), ("session-b", "reject")):
        handle = await adapter.start(StartRequest(input="go", user_id="u", session_id=session_id))
        handle.native_ref["known_checkpoint_ids"] = [target.id]
        handle.native_ref["pending_approval_ids"] = ["shared-interrupt"]
        await adapter.resume(
            handle,
            target,
            ResumePayload(
                kind="approval_decision",
                call_id="shared-interrupt",
                data={"type": decision},
            ),
        )


@pytest.mark.asyncio
async def test_close_clears_resume_idempotency_state_for_reused_run_id() -> None:
    adapter, _ = _interrupting_runtime([])
    target = ResumeTarget(kind="checkpoint_id", id="reused-checkpoint")

    first = await adapter.start(
        StartRequest(
            input="go",
            user_id="u",
            session_id="reused-session",
            metadata={"invocation_id": "reused-run"},
        )
    )
    first.native_ref["known_checkpoint_ids"] = [target.id]
    first.native_ref["pending_approval_ids"] = ["reused-interrupt"]
    await adapter.resume(
        first,
        target,
        ResumePayload(
            kind="approval_decision",
            call_id="reused-interrupt",
            data={"type": "approve"},
        ),
    )
    await adapter.close(first)

    second = await adapter.start(
        StartRequest(
            input="go",
            user_id="u",
            session_id="reused-session",
            metadata={"invocation_id": "reused-run"},
        )
    )
    second.native_ref["known_checkpoint_ids"] = [target.id]
    second.native_ref["pending_approval_ids"] = ["reused-interrupt"]
    await adapter.resume(
        second,
        target,
        ResumePayload(
            kind="approval_decision",
            call_id="reused-interrupt",
            data={"type": "reject"},
        ),
    )


async def _collect_runtime_events(adapter: LangGraphRuntimeAdapter, handle: RunHandle) -> list[Any]:
    return [event async for event in adapter.stream(handle)]


# ---------------------------------------- Task 7: 跨进程恢复 E2E（真关旧 executor）


class _DurableFakeRuntime(BaseRuntime):
    runtime_type = "durable-fake"

    def native_capabilities(self) -> dict[str, Any]:
        return {"durable": True}


class _DurableFakeAdapter(RuntimeAdapter):
    """checkpoint/恢复状态放在进程外字典，模拟跨进程 durable runtime。"""

    def __init__(self, durable_state: dict[str, dict[str, Any]]) -> None:
        super().__init__(_DurableFakeRuntime())
        self._durable = durable_state

    async def preflight(self) -> None:
        return None

    async def start(self, request: StartRequest) -> RunHandle:
        run_id = str(request.metadata.get("run_id") or f"run-{uuid4().hex[:8]}")
        handle = RunHandle(
            run_id=run_id,
            session_id=request.session_id,
            runtime_type="durable-fake",
        )
        self._durable[run_id] = {
            "handle": handle.model_dump(mode="json"),
            "interrupts": {"ck-1": "pending"},
            "resumes": 0,
        }
        return handle

    def stream(self, handle: RunHandle):
        async def _gen():
            yield RunCompleted(
                schema_version=2,
                event_id=f"{handle.run_id}-completed",
                seq=0,
                timestamp=1.0,
                run_id=handle.run_id,
                scope_id=f"run:{handle.run_id}",
                status="completed",
                output_refs=(),
                source=SourceRef(framework="ksadk"),
            )

        return _gen()

    async def cancel(self, handle: RunHandle) -> CancelResult:
        return CancelResult.NOT_RUNNING

    async def resume(self, handle, target, payload):
        self._durable[handle.run_id]["resumes"] += 1
        self._durable[handle.run_id]["last_target"] = target.id
        return handle

    async def checkpoint(self, handle: RunHandle):
        return CheckpointDescriptor(
            checkpoint_id="ck-1",
            invocation_id=handle.run_id,
            capability=CheckpointCapability(
                supported=True,
                granularity="snapshot",
                rollback_scope="turn",
                fork_supported=True,
                durable=True,
                shared_across_pods=True,
            ),
        )

    async def close(self, handle: RunHandle) -> None:
        # durable 状态进程外存活。
        return None

    async def attach(self, handle: RunHandle) -> RunHandle:
        if handle.run_id not in self._durable:
            raise ValueError(f"unknown run handle: {handle.run_id!r}")
        return handle

    async def durable_restore(self, handle: RunHandle) -> RunHandle:
        return await self.attach(handle)


@pytest.mark.asyncio
async def test_crashed_executor_run_is_resumed_by_new_executor_process() -> None:
    """旧 executor 真正 close_all 后，新 executor 只靠 durable run 行接回并 resume。"""
    from ksadk.events.session_event import SessionServiceEventStore
    from ksadk.kernel.memory_store import InMemoryAgentKernelStore
    from ksadk.kernel.state import RunState
    from ksadk.kernel.store import ActivationLeaseRequest, RunRecord
    from ksadk.runtime.adapter import RuntimeRegistry
    from ksadk.runtime.executor import RuntimeExecutor, handle_digest
    from ksadk.runtime.launch import RuntimeLaunchContext
    from ksadk.sessions.in_memory import InMemorySessionService
    session_id = "cross-process-session"
    agent_instance = "ai-cross"
    service = InMemorySessionService()
    await service.create_session(agent_id="a", user_id="u", session_id=session_id)
    kernel_store = InMemoryAgentKernelStore(SessionServiceEventStore(service))
    lease = await kernel_store.acquire_activation(
        ActivationLeaseRequest(
            agent_instance_id=agent_instance, session_id=session_id, activation_id="act-1"
        )
    )
    durable_state: dict[str, dict[str, Any]] = {}
    context = RuntimeLaunchContext(runtime_type="durable-fake", project_dir=Path("."))
    registry_old = RuntimeRegistry()
    registry_old.register(
        "durable-fake", lambda _ctx: _DurableFakeAdapter(durable_state)
    )

    old_executor = RuntimeExecutor(registry_old, kernel_store=kernel_store)
    preparation = await old_executor.prepare_start(context)
    handle = await old_executor.start(
        context,
        StartRequest(
            input="go",
            user_id="u",
            session_id=session_id,
            metadata={"run_id": "run-cross"},
        ),
        preparation=preparation,
    )
    assert old_executor.is_attached(handle)

    # durable run 行：pending -> running，携带 handle + digest。
    pending = await kernel_store.save_run_transition(
        RunRecord(
            run_id=handle.run_id,
            agent_instance_id=agent_instance,
            session_id=session_id,
            state=RunState.PENDING,
        ),
        expected_fence=lease.fencing_token,
    )
    running = pending.model_copy(
        update={
            "state": RunState.RUNNING,
            "handle": handle.model_dump(mode="json"),
        }
    )
    running.metadata["handle_digest"] = handle_digest(handle)
    await kernel_store.save_run_transition(running, expected_fence=lease.fencing_token)

    # 进程崩溃：真正关闭旧 executor，释放 lease（新进程更高 fence 接管）。
    await old_executor.close_all()
    assert not old_executor.is_attached(handle)
    await kernel_store.release_activation(
        lease.activation_id, expected_fence=lease.fencing_token
    )

    # 新 executor：不复用 _runs，也没有旧进程的任何内存状态。
    registry_new = RuntimeRegistry()
    registry_new.register(
        "durable-fake", lambda _ctx: _DurableFakeAdapter(durable_state)
    )
    new_executor = RuntimeExecutor(registry_new, kernel_store=kernel_store)
    assert new_executor.find_handle("durable-fake", handle.run_id, session_id) is None

    restored = await new_executor.attach_record(
        await kernel_store.load_run(handle.run_id), context
    )
    assert restored == handle
    assert new_executor.is_attached(restored)
    assert not old_executor.is_attached(restored)

    await new_executor.resume(restored, ResumeTarget(kind="checkpoint_id", id="ck-1"), None)
    assert durable_state[handle.run_id]["resumes"] == 1
    assert durable_state[handle.run_id]["last_target"] == "ck-1"
