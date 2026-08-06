from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ksadk.events.runtime_event import EventType
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime.adapter import (
    CancelResult,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    StartRequest,
)
from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter


class _InterruptState(TypedDict, total=False):
    prompt: str
    decision: Any


def _interrupting_runtime(
    side_effects: list[Any],
) -> tuple[LangGraphRuntimeAdapter, LangGraphRunner]:
    def approval_node(state: _InterruptState) -> dict[str, Any]:
        decision = interrupt({"question": "continue?"})
        side_effects.append(decision)
        return {"decision": decision}

    graph = StateGraph(_InterruptState)
    graph.add_node("approval", approval_node)
    graph.add_edge(START, "approval")
    graph.add_edge("approval", END)

    runner = LangGraphRunner(
        SimpleNamespace(entry_point="src/agent.py", agent_variable="root_agent"),
        ".",
    )
    runner._agent = graph.compile(checkpointer=InMemorySaver())
    return LangGraphRuntimeAdapter(runner), runner


def _blocking_after_interrupt_runtime(
    side_effects: list[Any],
) -> tuple[LangGraphRuntimeAdapter, asyncio.Event, asyncio.Event, asyncio.Event]:
    entered = asyncio.Event()
    release = asyncio.Event()
    cancellation_ack = asyncio.Event()

    def approval_node(state: _InterruptState) -> dict[str, Any]:
        return {"decision": interrupt({"question": "continue?"})}

    async def blocking_node(state: _InterruptState) -> dict[str, Any]:
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancellation_ack.set()
            raise
        side_effects.append(state["decision"])
        return {}

    graph = StateGraph(_InterruptState)
    graph.add_node("approval", approval_node)
    graph.add_node("blocking", blocking_node)
    graph.add_edge(START, "approval")
    graph.add_edge("approval", "blocking")
    graph.add_edge("blocking", END)

    runner = LangGraphRunner(
        SimpleNamespace(entry_point="src/agent.py", agent_variable="root_agent"),
        ".",
    )
    runner._agent = graph.compile(checkpointer=InMemorySaver())
    return LangGraphRuntimeAdapter(runner), entered, release, cancellation_ack


def _failing_after_interrupt_runtime() -> LangGraphRuntimeAdapter:
    def approval_node(state: _InterruptState) -> dict[str, Any]:
        return {"decision": interrupt({"question": "continue?"})}

    def failing_node(state: _InterruptState) -> dict[str, Any]:
        raise RuntimeError("resume exploded")

    graph = StateGraph(_InterruptState)
    graph.add_node("approval", approval_node)
    graph.add_node("failing", failing_node)
    graph.add_edge(START, "approval")
    graph.add_edge("approval", "failing")
    graph.add_edge("failing", END)

    runner = LangGraphRunner(
        SimpleNamespace(entry_point="src/agent.py", agent_variable="root_agent"),
        ".",
    )
    runner._agent = graph.compile(checkpointer=InMemorySaver())
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
        event for event in interrupted if event.event_type == EventType.APPROVAL_REQUESTED
    )
    checkpoint = next(
        event for event in interrupted if event.event_type == EventType.CHECKPOINT_CREATED
    )
    assert approval.payload["call_id"]

    descriptor = await adapter.checkpoint(handle)
    assert descriptor.checkpoint_id == checkpoint.payload["checkpoint_id"]

    await adapter.resume(
        handle,
        ResumeTarget(kind="checkpoint_id", id=descriptor.checkpoint_id),
        ResumePayload(
            kind="approval_decision",
            call_id=approval.payload["call_id"],
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
        event for event in interrupted if event.event_type == EventType.APPROVAL_REQUESTED
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
                call_id=approval.payload["call_id"],
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
            next(event for event in events if event.event_type == EventType.APPROVAL_REQUESTED)
        )
        checkpoints.append(await adapter.checkpoint(handle))

    with pytest.raises(ValueError, match="checkpoint.*does not belong"):
        await adapter.resume(
            handles[1],
            ResumeTarget(kind="checkpoint_id", id=checkpoints[0].checkpoint_id),
            ResumePayload(
                kind="approval_decision",
                call_id=approvals[1].payload["call_id"],
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
        event for event in interrupted if event.event_type == EventType.APPROVAL_REQUESTED
    )
    checkpoint = await adapter.checkpoint(handle)

    cancel_result = await adapter.cancel(handle)
    assert cancel_result.value == "pending_cancel_recorded"

    await adapter.resume(
        handle,
        ResumeTarget(kind="checkpoint_id", id=checkpoint.checkpoint_id),
        ResumePayload(
            kind="approval_decision",
            call_id=approval.payload["call_id"],
            data={"type": "approve"},
        ),
    )
    resumed = [event async for event in adapter.stream(handle)]

    assert [event.event_type for event in resumed] == [EventType.RUN_CANCELED]
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
        event for event in interrupted if event.event_type == EventType.APPROVAL_REQUESTED
    )
    checkpoint = await adapter.checkpoint(handle)
    target = ResumeTarget(kind="checkpoint_id", id=checkpoint.checkpoint_id)
    decision = ResumePayload(
        kind="approval_decision",
        call_id=approval.payload["call_id"],
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
                call_id=approval.payload["call_id"],
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
        event for event in interrupted if event.event_type == EventType.APPROVAL_REQUESTED
    )
    checkpoint = await adapter.checkpoint(handle)
    target = ResumeTarget(kind="checkpoint_id", id=checkpoint.checkpoint_id)
    decision = ResumePayload(
        kind="approval_decision",
        call_id=approval.payload["call_id"],
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
        event for event in interrupted if event.event_type == EventType.APPROVAL_REQUESTED
    )
    checkpoint = await adapter.checkpoint(handle)
    target = ResumeTarget(kind="checkpoint_id", id=checkpoint.checkpoint_id)
    decision = ResumePayload(
        kind="approval_decision",
        call_id=approval.payload["call_id"],
        data={"type": "approve"},
    )

    await adapter.resume(handle, target, decision)
    _ = [event async for event in adapter.stream(handle)]
    assert side_effects == [{"type": "approve"}]

    await adapter.resume(handle, target, decision)
    duplicate_events = [event async for event in adapter.stream(handle)]

    assert side_effects == [{"type": "approve"}]
    assert any(
        event.event_type == EventType.RUN_COMPLETED and event.payload["status"] == "already_resumed"
        for event in duplicate_events
    )


@pytest.mark.asyncio
async def test_resume_runner_error_becomes_run_failed() -> None:
    adapter = _failing_after_interrupt_runtime()
    handle = await adapter.start(
        StartRequest(input="go", user_id="u", session_id="resume-error-session")
    )
    interrupted = [event async for event in adapter.stream(handle)]
    approval = next(
        event for event in interrupted if event.event_type == EventType.APPROVAL_REQUESTED
    )
    checkpoint = await adapter.checkpoint(handle)

    await adapter.resume(
        handle,
        ResumeTarget(kind="checkpoint_id", id=checkpoint.checkpoint_id),
        ResumePayload(
            kind="approval_decision",
            call_id=approval.payload["call_id"],
            data={"type": "approve"},
        ),
    )
    resumed = [event async for event in adapter.stream(handle)]

    failed = next(event for event in resumed if event.event_type == EventType.RUN_FAILED)
    assert "resume exploded" in failed.payload["error"]


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
