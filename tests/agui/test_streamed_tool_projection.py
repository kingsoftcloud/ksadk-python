"""Canonical tool envelopes may acquire their content after ItemStarted."""

import json

import pytest

from ksadk.events.canonical import ContentSnapshot, ItemCompleted
from ksadk.events.content import ToolCallContent
from tests.agui.test_runtime_adapter import (
    _Adapter,
    _agent_for,
    _common,
    _events,
    _input,
    _run_completed,
    _run_started,
    _tool_call_begin,
)


@pytest.mark.parametrize("empty_start", [True, False])
async def test_tool_call_start_args_end_are_balanced_for_streamed_snapshots(empty_start):
    started = _tool_call_begin("stream-call", "render_a2ui", {}, 2)
    if empty_start:
        started = started.model_copy(update={"initial": None})
    arguments = {"components": [{"id": "root", "component": "Text", "text": "ready"}]}
    completed = ItemCompleted(
        item_id=started.item_id, item_kind="tool_call", snapshot=ContentSnapshot(parts=(
            ToolCallContent(part_id="p", call_id="stream-call", name="render_a2ui",
                            arguments=arguments),
        )), **_common(3),
    )
    adapter = _Adapter()
    adapter.streams["thread-1"] = _events(
        _run_started(seq=1), started, completed, _run_completed(seq=4),
    )
    events = [event async for event in _agent_for(adapter).run(_input())]
    tool_events = [event for event in events if event.type.value.startswith("TOOL_CALL_")]
    assert [event.type.value for event in tool_events] == [
        "TOOL_CALL_START", "TOOL_CALL_ARGS", "TOOL_CALL_END",
    ]
    assert json.loads(tool_events[1].delta) == arguments
