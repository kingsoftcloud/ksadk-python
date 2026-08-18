from __future__ import annotations

from typing import Any, AsyncIterator

import pytest

from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.observability.trajectory import project_trajectory_event
from ksadk.runners.base_runner import BaseRunner
from ksadk.runtime.adapter import StartRequest
from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter


class _TrajectoryRunner(BaseRunner):
    def __init__(self, chunks: list[Any]) -> None:
        super().__init__(detection_result=None, project_dir=".")
        self._chunks = chunks

    def load_agent(self) -> None:
        return None

    async def invoke(self, input_data: dict[str, Any]) -> dict[str, Any]:
        return {"output": "done"}

    async def stream(self, input_data: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        for chunk in self._chunks:
            yield chunk


async def _events_for(chunks: list[dict[str, Any]]):
    adapter = RunnerRuntimeAdapter(_TrajectoryRunner(chunks), runtime_type="fixture")
    handle = await adapter.start(
        StartRequest(
            input="hello",
            user_id="user-1",
            session_id="session-1",
            agent_id="agent-1",
            metadata={"invocation_id": "run-1", "trace_id": "a" * 32},
        )
    )
    return adapter, [event async for event in adapter.stream(handle)]


@pytest.mark.asyncio
async def test_runner_emits_turn_before_successful_run_terminal() -> None:
    adapter, events = await _events_for([{"type": "final", "output": "done"}])

    assert [event.event_type for event in events[:2]] == [
        EventType.RUN_STARTED,
        EventType.TURN_STARTED,
    ]
    assert [event.event_type for event in events[-2:]] == [
        EventType.TURN_COMPLETED,
        EventType.RUN_COMPLETED,
    ]
    assert events[-2].payload["status"] == "completed"
    assert {event.trace_id for event in events} == {"a" * 32}
    assert len({event.turn_id for event in events}) == 1
    assert adapter.runtime.native_capabilities()["model_call_boundaries"] is False


@pytest.mark.asyncio
async def test_runner_emits_failed_turn_before_failed_run() -> None:
    _, events = await _events_for([{"type": "error", "error": "boom"}])

    assert [event.event_type for event in events[-2:]] == [
        EventType.TURN_COMPLETED,
        EventType.RUN_FAILED,
    ]
    assert events[-2].payload["status"] == "failed"


@pytest.mark.asyncio
async def test_runner_maps_langgraph_model_boundary_chunks() -> None:
    _, events = await _events_for(
        [
            {"type": "step_start", "step_id": "step-model-1", "step_index": 1},
            {
                "type": "model_call_begin",
                "step_id": "step-model-1",
                "model_call_id": "model-1",
                "model": "demo-model",
            },
            {
                "type": "model_call_first_token",
                "step_id": "step-model-1",
                "model_call_id": "model-1",
                "ttft_ms": 7,
            },
            {
                "type": "model_call_end",
                "step_id": "step-model-1",
                "model_call_id": "model-1",
                "status": "completed",
                "duration_ms": 12,
            },
            {
                "type": "step_end",
                "step_id": "step-model-1",
                "step_index": 1,
                "status": "completed",
                "duration_ms": 12,
            },
            {"type": "final", "output": "done"},
        ]
    )

    trajectory = [
        event
        for event in events
        if event.event_type
        in {
            EventType.STEP_STARTED,
            EventType.MODEL_CALL_BEGIN,
            EventType.MODEL_CALL_FIRST_TOKEN,
            EventType.MODEL_CALL_END,
            EventType.STEP_COMPLETED,
        }
    ]
    assert [event.event_type for event in trajectory] == [
        EventType.STEP_STARTED,
        EventType.MODEL_CALL_BEGIN,
        EventType.MODEL_CALL_FIRST_TOKEN,
        EventType.MODEL_CALL_END,
        EventType.STEP_COMPLETED,
    ]
    assert {event.step_id for event in trajectory} == {"step-model-1"}
    assert trajectory[2].payload["ttft_ms"] == 7


@pytest.mark.asyncio
async def test_runner_propagates_current_step_to_message_tool_and_usage_events() -> None:
    _, events = await _events_for(
        [
            {"type": "step_start", "step_id": "step-model-1", "step_index": 1},
            {
                "type": "model_call_begin",
                "step_id": "step-model-1",
                "model_call_id": "model-1",
                "model": "demo-model",
            },
            {"type": "reasoning_delta", "delta": "分析"},
            {"type": "text_delta", "delta": "结论"},
            {"type": "tool_start", "call_id": "call-1", "name": "search"},
            {
                "type": "tool_end",
                "call_id": "call-1",
                "name": "search",
                "output": "done",
            },
            {
                "type": "usage",
                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            },
            {
                "type": "step_end",
                "step_id": "step-model-1",
                "step_index": 1,
                "status": "completed",
                "duration_ms": 12,
            },
            {"type": "final", "output": "结论"},
        ]
    )

    scoped = [
        event
        for event in events
        if event.event_type
        in {
            EventType.REASONING_DELTA,
            EventType.TEXT_DELTA,
            EventType.TEXT_COMPLETED,
            EventType.TOOL_CALL_BEGIN,
            EventType.TOOL_CALL_END,
            EventType.USAGE_REPORTED,
        }
    ]

    assert scoped
    assert {event.step_id for event in scoped} == {"step-model-1"}
    record_ids = {project_trajectory_event(event)["recordId"] for event in scoped}
    assert "assistant:model-1" in record_ids
    assert "tool:call-1" in record_ids


@pytest.mark.asyncio
async def test_runner_separates_multiple_model_calls_in_one_step() -> None:
    _, events = await _events_for(
        [
            {"type": "step_start", "step_id": "step-1", "step_index": 1},
            {
                "type": "model_call_begin",
                "step_id": "step-1",
                "model_call_id": "model-1",
                "model": "demo-model",
            },
            {"type": "text_delta", "delta": "first"},
            {
                "type": "model_call_begin",
                "step_id": "step-1",
                "model_call_id": "model-2",
                "model": "demo-model",
            },
            {"type": "text_delta", "delta": "second"},
            {"type": "final", "output": "second"},
        ]
    )

    assistant_ids = {
        project_trajectory_event(event)["recordId"]
        for event in events
        if event.event_type in {EventType.MODEL_CALL_BEGIN, EventType.TEXT_DELTA}
    }
    assert assistant_ids == {"assistant:model-1", "assistant:model-2"}


@pytest.mark.asyncio
async def test_runner_preserves_native_runtime_event_correlation_fields() -> None:
    native = RuntimeEvent.create(
        EventType.TEXT_DELTA,
        agent_id="native-agent",
        user_id="native-user",
        session_id="native-session",
        invocation_id="native-run",
        seq_id=7,
        event_id="native-event",
        turn_id="native-turn",
        step_id="native-step",
        parent_event_id="native-parent",
        trace_id="b" * 32,
        span_id="c" * 16,
        phase="commentary",
        payload={"text": "hello"},
    )
    adapter = RunnerRuntimeAdapter(_TrajectoryRunner([native]), runtime_type="fixture")
    handle = await adapter.start(
        StartRequest(
            input="hello",
            user_id="user-1",
            session_id="session-1",
            agent_id="agent-1",
            metadata={"invocation_id": "run-1", "trace_id": "b" * 32},
        )
    )

    events = [event async for event in adapter.stream(handle)]
    rebound = next(event for event in events if event.event_id == "native-event")

    assert rebound.turn_id == "native-turn"
    assert rebound.step_id == "native-step"
    assert rebound.parent_event_id == "native-parent"
    assert rebound.trace_id == "b" * 32
    assert rebound.span_id == "c" * 16


@pytest.mark.asyncio
async def test_runner_propagates_native_step_to_following_message_events() -> None:
    native_events = [
        RuntimeEvent.create(
            event_type,
            agent_id="native-agent",
            user_id="native-user",
            session_id="native-session",
            invocation_id="native-run",
            seq_id=index,
            event_id=f"native-{index}",
            turn_id="native-turn",
            step_id="native-step" if event_type == EventType.STEP_STARTED else None,
            payload=payload,
        )
        for index, (event_type, payload) in enumerate(
            [
                (EventType.STEP_STARTED, {"step_index": 1}),
                (EventType.REASONING_DELTA, {"text": "分析"}),
                (EventType.TEXT_DELTA, {"text": "答"}),
                (EventType.TEXT_COMPLETED, {"text": "答案"}),
            ],
            start=1,
        )
    ]
    adapter = RunnerRuntimeAdapter(_TrajectoryRunner(native_events), runtime_type="fixture")
    handle = await adapter.start(
        StartRequest(
            input="hello",
            user_id="user-1",
            session_id="session-1",
            agent_id="agent-1",
            metadata={"invocation_id": "run-1", "trace_id": "b" * 32},
        )
    )

    events = [event async for event in adapter.stream(handle)]
    messages = [
        event
        for event in events
        if event.event_type
        in {EventType.REASONING_DELTA, EventType.TEXT_DELTA, EventType.TEXT_COMPLETED}
    ]

    assert {event.step_id for event in messages} == {"native-step"}
