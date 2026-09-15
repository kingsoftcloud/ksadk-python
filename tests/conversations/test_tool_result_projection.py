"""Conversation projection keeps one stable UI identity for a tool call/result pair."""

from __future__ import annotations

from ksadk.conversations.projector import project_conversation_item
from ksadk.events.canonical import ItemCompleted, ItemStarted, SourceRef
from ksadk.events.content import ContentSnapshot, ToolCallContent, ToolResultContent


def test_tool_result_projection_keeps_the_call_id_for_the_renderer() -> None:
    source = SourceRef(framework="codex", native_item_id="native-call-1")
    call = ItemStarted(
        schema_version=2,
        event_id="event-call",
        seq=1,
        timestamp=1.0,
        run_id="run-1",
        scope_id="session-1",
        source=source,
        item_id="tool-call-item",
        item_kind="tool_call",
        initial=ContentSnapshot(
            parts=(
                ToolCallContent(
                    part_id="call-part",
                    call_id="call-1",
                    name="search",
                    arguments={"query": "KsADK"},
                ),
            )
        ),
    )
    result = ItemCompleted(
        schema_version=2,
        event_id="event-result",
        seq=2,
        timestamp=2.0,
        run_id="run-1",
        scope_id="session-1",
        source=source,
        item_id="tool-result-item",
        item_kind="tool_result",
        snapshot=ContentSnapshot(
            parts=(
                ToolResultContent(
                    part_id="result-part",
                    call_id="call-1",
                    result={"hits": 3},
                ),
            )
        ),
    )

    projected_call = project_conversation_item(call, session_id="session-1")
    projected_result = project_conversation_item(result, session_id="session-1")

    assert projected_call.item_id == "tool-call-item"
    assert projected_result.item_id == "tool-result-item"
    assert projected_result.payload["callId"] == "call-1"
    assert projected_result.payload["output"] == {"hits": 3}
