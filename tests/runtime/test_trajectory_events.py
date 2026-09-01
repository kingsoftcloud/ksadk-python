from __future__ import annotations

from typing import Any, AsyncIterator

import pytest

from ksadk.events.canonical import ItemCompleted, ItemStarted, ItemUpdated, RunCompleted
from ksadk.events.content import TextContent
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
            metadata={"invocation_id": "run-1"},
        )
    )
    return adapter, [event async for event in adapter.stream(handle)]


@pytest.mark.asyncio
async def test_runner_emits_canonical_item_lifecycle_before_successful_terminal() -> None:
    adapter, events = await _events_for([{"type": "final", "output": "done"}])

    assert [event.event_type for event in events] == [
        "run.started",
        "item.started",
        "item.completed",
        "run.completed",
    ]
    assert isinstance(events[1], ItemStarted)
    assert isinstance(events[2], ItemCompleted)
    assert isinstance(events[-1], RunCompleted)
    assert events[-1].output_refs[0].item_id == events[2].item_id
    assert adapter.runtime.native_capabilities()["model_call_boundaries"] is False


@pytest.mark.asyncio
async def test_runner_emits_failed_run_without_legacy_turn_fact() -> None:
    _, events = await _events_for([{"type": "error", "error": "boom"}])

    assert [event.event_type for event in events] == ["run.started", "run.failed"]
    assert events[-1].error.code == "runner_failed"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_final_autoclose_preserves_stream_order_and_sequence_order() -> None:
    """A persisted stream must never contain a later seq before an earlier one."""

    _, events = await _events_for(
        [
            {"type": "reasoning_delta", "delta": "分析"},
            {"type": "text_delta", "delta": "草稿"},
            {"type": "final", "output": "结论"},
        ]
    )

    assert [event.seq for event in events] == sorted(event.seq for event in events)
    completed = [event for event in events if isinstance(event, ItemCompleted)]
    assert [event.item_kind for event in completed] == ["reasoning", "message"]
    assert events[-1].event_type == "run.completed"


@pytest.mark.asyncio
async def test_text_deltas_and_final_snapshot_share_one_final_answer_item() -> None:
    """Fallback runner output must stream and then complete the same answer item."""

    _, events = await _events_for(
        [
            {"type": "reasoning_delta", "delta": "分析"},
            {"type": "text_delta", "delta": "你"},
            {"type": "text_delta", "delta": "好"},
            {"type": "final", "output": "你好"},
        ]
    )

    message_starts = [
        event
        for event in events
        if isinstance(event, ItemStarted) and event.item_kind == "message"
    ]
    message_updates = [
        event
        for event in events
        if isinstance(event, ItemUpdated) and event.item_kind == "message"
    ]
    message_completions = [
        event
        for event in events
        if isinstance(event, ItemCompleted) and event.item_kind == "message"
    ]

    assert len(message_starts) == 1
    assert message_starts[0].phase == "final_answer"
    assert [event.update.text for event in message_updates] == ["你", "好"]
    assert len(message_completions) == 1
    assert message_completions[0].item_id == message_starts[0].item_id
    assert message_completions[0].snapshot.parts == (
        TextContent(part_id="text-0", text="你好"),
    )
    assert events[-1].output_refs[0].item_id == message_starts[0].item_id


@pytest.mark.asyncio
async def test_explicit_commentary_remains_distinct_from_final_answer() -> None:
    _, events = await _events_for(
        [
            {"type": "commentary_delta", "delta": "先核对数据"},
            {"type": "text_delta", "delta": "最终结论"},
            {"type": "final", "output": "最终结论"},
        ]
    )

    message_starts = [
        event
        for event in events
        if isinstance(event, ItemStarted) and event.item_kind == "message"
    ]
    assert [event.phase for event in message_starts] == ["commentary", "final_answer"]
    assert len({event.item_id for event in message_starts}) == 2


@pytest.mark.asyncio
async def test_runner_projects_reasoning_tool_usage_and_message_as_v2_items() -> None:
    _, events = await _events_for(
        [
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
            {"type": "final", "output": "结论"},
        ]
    )

    assert {event.event_type for event in events} >= {
        "item.started",
        "item.updated",
        "item.completed",
        "usage.reported",
    }
    projected = [project_trajectory_event(event) for event in events]
    assert {item["recordId"] for item in projected} >= {"tool:call-1"}
    assert any(
        isinstance(event, ItemUpdated)
        and event.item_kind == "reasoning"
        and isinstance(event.update, TextContent)
        and event.update.text == "分析"
        for event in events
    )


@pytest.mark.asyncio
async def test_legacy_model_boundary_chunks_do_not_invent_v2_protocol_facts() -> None:
    """The v2 contract has items, not synthetic turn/step/model-call records."""

    _, events = await _events_for(
        [
            {"type": "step_start", "step_id": "step-1", "step_index": 1},
            {"type": "model_call_begin", "model_call_id": "model-1", "model": "demo"},
            {"type": "text_delta", "delta": "first"},
            {"type": "model_call_begin", "model_call_id": "model-2", "model": "demo"},
            {"type": "text_delta", "delta": "second"},
            {"type": "final", "output": "second"},
        ]
    )

    message_ids = {
        event.item_id
        for event in events
        if isinstance(event, (ItemStarted, ItemUpdated, ItemCompleted))
        and event.item_kind == "message"
    }
    assert len(message_ids) == 1  # one final-answer item, not fake model-call ids
    assert not any(
        event.event_type.startswith(("turn.", "step.", "model.call.")) for event in events
    )
