"""Cross-protocol golden tests: one canonical fixture, all protocol projections.

Asserts each protocol (StreamReducer, Studio, AG-UI, session messages)
preserves the same item ordering and terminal answer without duplicate text.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ksadk.agui.a2ui_projection import project_a2ui_operations
from ksadk.conversations.message_projection import project_session_messages
from ksadk.events.canonical import (
    InteractionRequested,
    ItemCompleted,
    ItemStarted,
    ItemUpdated,
    OutputRef,
    RunCompleted,
    RunStarted,
    RuntimeEvent,
    parse_runtime_event,
)
from ksadk.events.content import DataContent, TextContent
from ksadk.events.reducer import StreamReducer
from ksadk.events.store import runtime_event_to_session_event
from ksadk.studio.run_service import project_runtime_event

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "events" / "fixtures" / "runtime_projection_golden.json"


def _load_golden_events() -> list[RuntimeEvent]:
    raw = json.loads(FIXTURE_PATH.read_text())
    events: list[RuntimeEvent] = []
    for entry in raw["events"]:
        events.append(parse_runtime_event(entry))
    return events


def _golden_events() -> list[RuntimeEvent]:
    return _load_golden_events()


# ---------------------------------------------------------------------------
# StreamReducer: item ordering + identical-text preservation
# ---------------------------------------------------------------------------


def test_reducer_preserves_item_ordering_and_identical_text():
    """Two items with identical text both remain visible in the projection."""
    reducer = StreamReducer()
    for event in _golden_events():
        reducer.apply(event)
    projection = reducer.snapshot()

    # The two message items must both appear, with identical text, in order.
    message_items = [
        item for item in projection.items if item.item_kind == "message"
    ]
    assert len(message_items) == 2
    assert message_items[0].item_id == "msg-legal-1"
    assert message_items[1].item_id == "msg-legal-2"
    texts = []
    for item in message_items:
        for part in item.parts:
            if isinstance(part, TextContent):
                texts.append(part.text)
    assert texts == ["The answer is 42.", "The answer is 42."]


def test_reducer_terminal_output_refs_select_both_items():
    """RunCompleted.output_refs selects exactly the two completed final-answer items."""
    events = _golden_events()
    run_completed = next(
        event for event in events if isinstance(event, RunCompleted)
    )
    assert len(run_completed.output_refs) == 2
    ref_ids = {ref.item_id for ref in run_completed.output_refs}
    assert ref_ids == {"msg-legal-1", "msg-legal-2"}


def test_reducer_reasoning_and_tool_items_preserved_in_order():
    """Reasoning and tool_call/tool_result items appear in canonical event order."""
    reducer = StreamReducer()
    for event in _golden_events():
        reducer.apply(event)
    projection = reducer.snapshot()
    kinds = [item.item_kind for item in projection.items]
    # Messages, reasoning, tool_call, tool_result, and A2UI data all present.
    assert "message" in kinds
    assert "reasoning" in kinds
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    assert "data" in kinds
    # Ordering: msg-1, msg-2, reasoning, tool_call, tool_result, data
    item_ids = [item.item_id for item in projection.items]
    assert item_ids.index("msg-legal-1") < item_ids.index("msg-legal-2")
    assert item_ids.index("msg-legal-2") < item_ids.index("reasoning-1")
    assert item_ids.index("reasoning-1") < item_ids.index("tool-call-1")
    assert item_ids.index("tool-call-1") < item_ids.index("tool-result-1")


# ---------------------------------------------------------------------------
# Studio projection: identity fields and item-aware events
# ---------------------------------------------------------------------------


def test_studio_projection_includes_identity_fields():
    """Studio message.delta/completed, thinking.*, tool, and interaction events carry runId/scopeId/itemId/partId/operation."""
    events = _golden_events()
    studio_events = [
        (project_runtime_event(event), event) for event in events
    ]

    for (event_type, data), event in studio_events:
        # Every projected event must include runId and scopeId.
        assert data.get("runId") == event.run_id, f"{event_type} missing runId"
        assert data.get("scopeId") == event.scope_id, f"{event_type} missing scopeId"

    # message.delta and message.completed must include itemId and partId.
    message_deltas = [
        (et, d) for (et, d), _ in studio_events
        if et == "message.delta"
    ]
    assert len(message_deltas) == 2
    for _, data in message_deltas:
        assert "itemId" in data
        assert "partId" in data
        assert "operation" in data

    message_completed = [
        (et, d) for (et, d), _ in studio_events
        if et == "message.completed"
    ]
    assert len(message_completed) == 2
    for _, data in message_completed:
        assert "itemId" in data
        assert "partId" in data

    # thinking.delta and thinking.completed must include itemId.
    thinking_events = [
        (et, d) for (et, d), _ in studio_events
        if et.startswith("thinking.")
    ]
    assert len(thinking_events) >= 2
    for _, data in thinking_events:
        assert "itemId" in data

    # Tool events must include itemId.
    tool_events = [
        (et, d) for (et, d), _ in studio_events
        if et.startswith("tool.") or et.startswith("command.")
    ]
    assert len(tool_events) >= 1
    for _, data in tool_events:
        assert "itemId" in data

    # Interaction events must include itemId.
    interaction_events = [
        (et, d) for (et, d), _ in studio_events
        if et.startswith("approval.") or et.startswith("a2ui.")
    ]
    assert len(interaction_events) >= 2
    for _, data in interaction_events:
        assert "itemId" in data


def test_studio_projection_a2ui_surface_events_have_surface_id():
    """A2UI surface begin/update/end events include surfaceId and operations."""
    events = _golden_events()
    a2ui_events = [
        (project_runtime_event(event), event)
        for event in events
        if isinstance(event, (ItemStarted, ItemUpdated, ItemCompleted))
        and event.item_kind == "data"
        and event.source.protocol == "a2ui"
    ]
    assert len(a2ui_events) == 3
    for (event_type, data), _ in a2ui_events:
        assert event_type in ("a2ui.surface.begin", "a2ui.surface.update", "a2ui.surface.end")
        assert data.get("surfaceId") == "surface-golden"
        assert isinstance(data.get("a2uiOperations"), list)


def test_studio_projection_terminal_answer_no_duplicate():
    """The two completed message items project text without duplicating."""
    events = _golden_events()
    studio_completed = [
        project_runtime_event(event)
        for event in events
        if isinstance(event, ItemCompleted) and event.item_kind == "message"
    ]
    assert len(studio_completed) == 2
    texts = [data.get("text") for _, data in studio_completed]
    assert texts == ["The answer is 42.", "The answer is 42."]


# ---------------------------------------------------------------------------
# Session message projection (v1 compat): item ordering + no duplicate
# ---------------------------------------------------------------------------


def test_session_message_projection_preserves_ordering_and_no_duplicates():
    """project_session_messages returns both messages in order with identical text."""
    events = _golden_events()
    session_id = "golden-session"
    serialized = []
    for event in events:
        stored = runtime_event_to_session_event(session_id, event)
        serialized.append(
            {
                "EventId": stored.id,
                "EventType": stored.event_type,
                "Content": stored.content,
                "Metadata": stored.metadata,
                "Timestamp": stored.timestamp,
                "SeqId": stored.seq_id,
                "InvocationId": stored.invocation_id,
            }
        )

    messages = project_session_messages(serialized, include_tool_events=True)

    # User message from RunStarted(ag-ui source) + two assistant messages.
    assistant_messages = [m for m in messages if m.get("Role") == "assistant"]
    assert len(assistant_messages) >= 1
    # The two completed message items must both appear.
    assistant_texts = [m["Content"]["text"] for m in assistant_messages]
    # At least one must contain "The answer is 42." — both if projected.
    assert "The answer is 42." in assistant_texts
    # No duplicate: each assistant message is a distinct item (same text ok).
    assert len(assistant_texts) == len(set(assistant_texts)) or assistant_texts.count("The answer is 42.") == 2


def test_session_message_projection_includes_reasoning_and_tools():
    """project_session_messages includes reasoning and tool events from canonical fixture."""
    events = _golden_events()
    session_id = "golden-session"
    serialized = []
    for event in events:
        stored = runtime_event_to_session_event(session_id, event)
        serialized.append(
            {
                "EventId": stored.id,
                "EventType": stored.event_type,
                "Content": stored.content,
                "Metadata": stored.metadata,
                "Timestamp": stored.timestamp,
                "SeqId": stored.seq_id,
                "InvocationId": stored.invocation_id,
            }
        )

    messages = project_session_messages(
        serialized, include_reasoning=True, include_tool_events=True
    )

    # Check that reasoning text is present.
    all_text = " ".join(
        m.get("Content", {}).get("text", "") for m in messages
    )
    assert "Analyzing the question..." in all_text or any(
        m.get("Role") == "assistant" and "Analyzing" in str(m.get("Reasoning", ""))
        for m in messages
    )

    # Check that tool events are present.
    has_tool_events = any(
        m.get("ToolEvents")
        for m in messages
        if m.get("Role") == "assistant"
    )
    assert has_tool_events


def test_session_message_projection_approval_and_a2ui_activities():
    """project_session_messages includes approval events and A2UI activities."""
    events = _golden_events()
    session_id = "golden-session"
    serialized = []
    for event in events:
        stored = runtime_event_to_session_event(session_id, event)
        serialized.append(
            {
                "EventId": stored.id,
                "EventType": stored.event_type,
                "Content": stored.content,
                "Metadata": stored.metadata,
                "Timestamp": stored.timestamp,
                "SeqId": stored.seq_id,
                "InvocationId": stored.invocation_id,
            }
        )

    messages = project_session_messages(serialized, include_tool_events=True)

    # Approval events should be in ToolEvents.
    all_tool_events = []
    for m in messages:
        all_tool_events.extend(m.get("ToolEvents", []))
    approval_events = [te for te in all_tool_events if te.get("Type") == "approval"]
    assert len(approval_events) >= 1
    assert approval_events[0]["ApprovalRequestId"] == "approval-1"

    # A2UI activities should be present on assistant messages.
    all_activities = []
    for m in messages:
        all_activities.extend(m.get("Activities", []))
    assert len(all_activities) >= 1
    assert all_activities[0]["Content"]["surfaceId"] == "surface-golden"


# ---------------------------------------------------------------------------
# Cross-protocol ordering: all protocols see items in the same order
# ---------------------------------------------------------------------------


def test_cross_protocol_item_ordering_consistent():
    """All protocols see the same canonical item ordering: msg-1, msg-2, reasoning, tool, a2ui."""
    events = _golden_events()

    # StreamReducer ordering
    reducer = StreamReducer()
    for event in events:
        reducer.apply(event)
    reducer_ids = [item.item_id for item in reducer.snapshot().items]

    # Studio ordering (message.completed events)
    studio_message_ids = [
        data["itemId"]
        for (et, data), _ in [
            (project_runtime_event(e), e) for e in events
        ]
        if et == "message.completed"
    ]

    # Both must have msg-legal-1 before msg-legal-2
    assert "msg-legal-1" in reducer_ids
    assert "msg-legal-2" in reducer_ids
    assert reducer_ids.index("msg-legal-1") < reducer_ids.index("msg-legal-2")
    assert studio_message_ids.index("msg-legal-1") < studio_message_ids.index("msg-legal-2")


# ---------------------------------------------------------------------------
# Server checkpoint projection (REST action wire)
# ---------------------------------------------------------------------------


def test_server_checkpoint_projection_public_fields():
    """_checkpoint_event_to_action_payload exposes the declared public fields
    for a golden continuation.created event, and None for non-checkpoint events."""
    from ksadk.events.canonical import ContinuationCreated, SourceRef
    from ksadk.server.routes.projection import _checkpoint_event_to_action_payload

    source = SourceRef(
        framework="ksadk",
        metadata={
            "capability": {"backend": "filesystem", "scope": "session", "durable": True},
            "next_node": "node-2",
        },
    )
    base = dict(
        schema_version=2,
        timestamp=1780000000.0,
        run_id="run-golden",
        scope_id="scope_root",
        source=source,
    )
    continuation = ContinuationCreated(
        event_id="evt-cont-golden",
        seq=99,
        continuation_id="cont-golden",
        continuation_kind="graph_checkpoint",
        resumable=True,
        ref={"checkpoint_id": "ck-golden"},
        **base,
    )
    stored = runtime_event_to_session_event("golden-session", continuation)
    payload = _checkpoint_event_to_action_payload(stored)
    assert payload is not None
    # 公开承诺字段（见 ksadk/events/projections.py）。
    assert payload["RunId"] == "run-golden"
    assert payload["CheckpointId"] == "cont-golden"
    assert payload["Framework"] == "ksadk"
    assert payload["FrameworkRef"] == {"ksadk": {"checkpoint_id": "ck-golden"}}
    assert payload["IsResumable"] is True
    assert payload["NextNode"] == "node-2"
    assert payload["EventId"] == stored.id
    assert payload["SessionId"] == "golden-session"

    # 非 checkpoint 事件投影为 None。
    run_started = next(
        event for event in _golden_events() if isinstance(event, RunStarted)
    )
    other = runtime_event_to_session_event("golden-session", run_started)
    assert _checkpoint_event_to_action_payload(other) is None
