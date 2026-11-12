"""Tool-call deduplication cannot erase a sibling scope's native call ID."""

import pytest

from ksadk.events.canonical import (
    ContentSnapshot,
    ItemCompleted,
    ItemStarted,
    ItemUpdated,
    RunStarted,
    SourceRef,
)
from ksadk.events.content import TextContent, ToolCallContent, ToolResultContent
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


@pytest.mark.asyncio
@pytest.mark.parametrize("corrected", ["corrected", ""])
async def test_flat_text_projection_folds_snapshots_by_item_identity(monkeypatch, corrected):
    async def events(**kwargs):
        seq = 0

        def envelope():
            nonlocal seq
            seq += 1
            return dict(schema_version=2, event_id=f"e{seq}", seq=seq,
                        timestamp=float(seq), run_id="run", scope_id="root",
                        source=SourceRef(framework="langgraph"))

        yield RunStarted(**envelope(), status="running")
        for item_id in ("first", "second"):
            yield ItemStarted(**envelope(), item_id=item_id, item_kind="message")
            yield ItemUpdated(**envelope(), item_id=item_id, item_kind="message", op="append",
                              update=TextContent(part_id="text", text="same"))
            # Native adapters may repeat full text before completing the item.
            yield ItemUpdated(**envelope(), item_id=item_id, item_kind="message", op="replace",
                              update=TextContent(part_id="text", text="same"))
            yield ItemCompleted(**envelope(), item_id=item_id, item_kind="message",
                                snapshot=ContentSnapshot(parts=(TextContent(part_id="text", text="same"),)))
        yield ItemStarted(**envelope(), item_id="third", item_kind="message")
        yield ItemUpdated(**envelope(), item_id="third", item_kind="message", op="append",
                          update=TextContent(part_id="text", text="draft"))
        yield ItemCompleted(**envelope(), item_id="third", item_kind="message",
                            snapshot=ContentSnapshot(parts=(TextContent(part_id="text", text=corrected),)))

    monkeypatch.setattr(conversation_execution, "iter_runtime_conversation_events", events)
    projected = [event async for event in conversation_execution.iter_runtime_conversation_semantic_events()]
    text = [event for event in projected if event["type"] == "text"]
    assert [(event["delta"], event.get("replace", False)) for event in text] == [
        ("same", False), ("same", False), ("draft", False), ("samesame" + corrected, True),
    ]
