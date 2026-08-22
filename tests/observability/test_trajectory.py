from __future__ import annotations

import json

from ksadk.events.canonical import (
    ApprovalRequest,
    ContextCompactionStarted,
    InteractionRequested,
    ItemCompleted,
    ItemStarted,
    ItemUpdated,
    RunProgress,
    RuntimeEvent,
    SourceRef,
)
from ksadk.events.content import (
    ContentSnapshot,
    DataContent,
    TextContent,
    ToolCallContent,
    ToolResultContent,
)
from ksadk.observability.trajectory import encode_sse, project_trajectory_event


def envelope(index: int, **updates):
    values = {
        "schema_version": 2,
        "event_id": f"evt-{index}",
        "seq": index,
        "timestamp": float(index),
        "run_id": "run-1",
        "scope_id": "scope-1",
        "source": SourceRef(framework="ksadk"),
    }
    values.update(updates)
    return values


def test_project_trajectory_event_uses_stable_tool_record_id():
    call = ToolCallContent(
        part_id="part-call-1", call_id="call-1", name="search", arguments={"q": "docs"}
    )
    begin = ItemStarted(
        item_id="item-tool-1",
        item_kind="tool_call",
        initial=ContentSnapshot(parts=(call,)),
        **envelope(1),
    )
    end = ItemCompleted(
        item_id="item-tool-1",
        item_kind="tool_call",
        snapshot=ContentSnapshot(
            parts=(
                call,
                ToolResultContent(
                    part_id="part-result-1",
                    call_id="call-1",
                    result={"status": "completed", "duration_ms": 42, "output": "two docs"},
                ),
            )
        ),
        **envelope(2),
    )

    projected_begin = project_trajectory_event(begin)
    projected_end = project_trajectory_event(end)

    assert projected_begin["recordId"] == projected_end["recordId"] == "tool:call-1"
    assert projected_begin["category"] == "tool"
    assert projected_begin["status"] == "running"
    assert projected_end["status"] == "completed"
    assert projected_end["durationMs"] == 42
    assert projected_end["details"]["output"] == "two docs"


def test_project_trajectory_event_groups_message_item_facts():
    first = ItemUpdated(
        item_id="item-message-1",
        item_kind="message",
        op="append",
        update=TextContent(part_id="part-message-1", text="你"),
        **envelope(1),
    )
    completed = ItemCompleted(
        item_id="item-message-1",
        item_kind="message",
        snapshot=ContentSnapshot(parts=(TextContent(part_id="part-message-1", text="你好"),)),
        **envelope(2),
    )

    projected = [project_trajectory_event(item) for item in (first, completed)]

    assert {item["recordId"] for item in projected} == {"assistant:item-message-1"}
    assert {item["category"] for item in projected} == {"assistant"}
    assert {item["summary"] for item in projected} == {"Message"}
    assert projected[1]["status"] == "completed"


def test_project_trajectory_event_separates_distinct_message_items():
    first = ItemUpdated(
        item_id="item-message-1",
        item_kind="message",
        op="append",
        update=TextContent(part_id="part-message-1", text="first"),
        **envelope(1),
    )
    second = ItemUpdated(
        item_id="item-message-2",
        item_kind="message",
        op="append",
        update=TextContent(part_id="part-message-2", text="second"),
        **envelope(2),
    )

    assert project_trajectory_event(first)["recordId"] == "assistant:item-message-1"
    assert project_trajectory_event(second)["recordId"] == "assistant:item-message-2"


def test_project_trajectory_event_uses_stable_business_record_ids():
    context = ContextCompactionStarted(trigger="budget", **envelope(1))
    approval = InteractionRequested(
        interaction_id="approval-1",
        interaction_kind="approval",
        request=ApprovalRequest(call_id="call-1", kind="tool"),
        **envelope(2),
    )

    cases = [
        (context, "context:scope-1:compaction", "context"),
        (approval, "approval:approval-1", "approval"),
    ]
    for source, record_id, category in cases:
        projected = project_trajectory_event(source)
        assert projected["recordId"] == record_id
        assert projected["category"] == category
        assert projected["eventId"] == source.event_id
        assert projected["seqId"] == source.seq


def test_project_trajectory_event_preserves_a2ui_as_a_system_event():
    source = ItemStarted(
        item_id="item-surface-1",
        item_kind="data",
        initial=ContentSnapshot(
            parts=(DataContent(part_id="part-surface-1", data=[{"op": "create"}]),)
        ),
        **envelope(1, source=SourceRef(framework="ksadk", protocol="a2ui", metadata={"surface_id": "surface-1"})),
    )

    projected = project_trajectory_event(source)

    assert projected["category"] == "system"
    assert projected["recordId"] == "surface:surface-1"
    assert projected["source"]["event_type"] == "item.started"


def test_project_trajectory_event_falls_back_to_system_run_progress():
    projected = project_trajectory_event(
        RunProgress(status="running", progress=0.5, message="halfway", **envelope(1))
    )

    assert projected["projectionVersion"] == 1
    assert projected["recordId"] == "system:evt-1"
    assert projected["category"] == "system"
    assert projected["status"] == "running"
    assert projected["summary"] == "halfway"


def test_encode_sse_is_deterministic():
    frame = encode_sse({"seqId": 7, "summary": "完成"}, event_id=7)

    assert frame.startswith("id: 7\nevent: runtime_event\ndata: ")
    assert frame.endswith("\n\n")
    assert json.loads(frame.split("data: ", 1)[1]) == {"seqId": 7, "summary": "完成"}
