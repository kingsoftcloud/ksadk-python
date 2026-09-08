"""Tool-call deduplication cannot erase a sibling scope's native call ID."""

import pytest

from ksadk.events.canonical import (
    ContentSnapshot,
    ItemCompleted,
    ItemStarted,
    RunStarted,
    SourceRef,
)
from ksadk.events.content import ToolCallContent, ToolResultContent
from ksadk.runtime import conversation_execution


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_has_call", [True, False])
async def test_same_native_call_id_in_sibling_scopes_keeps_both_tools(
    monkeypatch, initial_has_call
):
    async def events(**kwargs):
        seq = 0

        def envelope(scope):
            nonlocal seq
            seq += 1
            return dict(
                schema_version=2,
                event_id=f"e{seq}",
                seq=seq,
                timestamp=float(seq),
                run_id="run",
                scope_id=scope,
                source=SourceRef(framework="ksadk"),
            )

        yield RunStarted(**envelope("root"), status="running")
        for scope, name in [("child-a", "read_file"), ("child-b", "delete_file")]:
            snapshot = ContentSnapshot(
                parts=(ToolCallContent(part_id="call", call_id="call_1", name=name, arguments={}),)
            )
            yield ItemStarted(
                **envelope(scope),
                item_id="tool",
                item_kind="tool_call",
                initial=snapshot if initial_has_call else None,
            )
            yield ItemCompleted(
                **envelope(scope),
                item_id="tool",
                item_kind="tool_call",
                snapshot=snapshot,
            )
        # Delayed results arrive after both scopes registered their identical native ID.
        for scope in ["child-a", "child-b"]:
            snapshot = ContentSnapshot(
                parts=(ToolResultContent(part_id="result", call_id="call_1", result=scope),)
            )
            yield ItemStarted(
                **envelope(scope),
                item_id="result",
                item_kind="tool_result",
                initial=None,
            )
            yield ItemCompleted(
                **envelope(scope),
                item_id="result",
                item_kind="tool_result",
                snapshot=snapshot,
            )

    monkeypatch.setattr(conversation_execution, "iter_runtime_conversation_events", events)
    projected = [
        event async for event in conversation_execution.iter_runtime_conversation_semantic_events()
    ]
    calls = [event for event in projected if event["type"] == "tool_call"]
    results = [event for event in projected if event["type"] == "tool_result"]
    assert [event["name"] for event in calls] == ["read_file", "delete_file"]
    assert [(event["name"], event["output"]) for event in results] == [
        ("read_file", "child-a"),
        ("delete_file", "child-b"),
    ]
