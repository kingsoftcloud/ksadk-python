"""Historical Agent live approvals and cold history keep their public contract."""

import pytest

from ksadk.conversations.message_projection import project_session_messages
from ksadk.events.canonical import ApprovalRequest, InteractionRequested, RunInterrupted, SourceRef
from ksadk.runtime import conversation_execution


@pytest.mark.asyncio
async def test_live_approval_uses_the_same_public_id_as_durable_resume(monkeypatch):
    event = InteractionRequested(
        schema_version=2,
        event_id="request-event",
        seq=1,
        timestamp=1.0,
        run_id="run-1",
        scope_id="scope-1",
        source=SourceRef(framework="langgraph"),
        interaction_id="public-approval",
        interaction_kind="approval",
        request=ApprovalRequest(
            call_id="tool-call-1",
            kind="tool",
            detail={
                "id": "native-approval",
                "approval_request_id": "native-approval",
                "tool_name": "run_command",
                "arguments": {"command": "python3 --version"},
            },
        ),
    )

    async def events(**kwargs):
        yield event
        yield RunInterrupted(
            schema_version=2,
            event_id="interrupted",
            seq=2,
            timestamp=2.0,
            run_id="run-1",
            scope_id="scope-1",
            source=SourceRef(framework="langgraph"),
            status="interrupted",
            reason="approval",
            interaction_id="public-approval",
        )

    monkeypatch.setattr(conversation_execution, "iter_runtime_conversation_events", events)
    projected = [
        item async for item in conversation_execution.iter_runtime_conversation_semantic_events()
    ]
    approval = next(item["interrupt_info"] for item in projected if item["type"] == "interrupt")
    assert approval["approval_request_id"] == "public-approval"
    assert approval["id"] == "public-approval"
    assert event.request.detail["approval_request_id"] == "native-approval"


def message_event(seq, event_type, text, *, item_id="message-1"):
    holder = (
        {"update": {"part_id": "text-0", "content_type": "text", "text": text}, "op": "append"}
        if event_type == "item.updated"
        else {"snapshot": {"parts": [{"part_id": "text-0", "content_type": "text", "text": text}]}}
    )
    return {
        "EventId": f"event-{seq}",
        "SeqId": seq,
        "Timestamp": seq,
        "InvocationId": "run-1",
        "EventType": event_type,
        "Author": "assistant",
        "Metadata": {"ksadk_canonical_runtime_event": True, "schema_version": 2},
        "Content": {
            "runtime_event": {
                "event_type": event_type,
                "item_kind": "message",
                "item_id": item_id,
                "source": {"framework": "langgraph"},
                **holder,
            }
        },
    }


@pytest.mark.parametrize("tail", ["item.completed", "assistant_stream_snapshot"])
def test_delta_text_is_not_repeated_by_the_terminal_snapshot(tail):
    events = [message_event(1, "item.updated", "hello "), message_event(2, "item.updated", "world")]
    events.append(
        message_event(3, tail, "hello world")
        if tail == "item.completed"
        else {
            "EventId": "event-3",
            "SeqId": 3,
            "InvocationId": "run-1",
            "EventType": tail,
            "Author": "assistant",
            "Content": {"parts": [{"text": "hello world"}]},
        }
    )
    messages = project_session_messages(events, include_reasoning=True, include_tool_events=True)
    text = "".join(
        block["Content"]
        for message in messages
        for block in message.get("Blocks", [])
        if block["Type"] == "text"
    )
    assert text == "hello world"


def test_distinct_items_with_identical_text_remain_visible():
    events = [
        message_event(1, "item.completed", "same", item_id="first"),
        message_event(2, "item.completed", "same", item_id="second"),
    ]
    messages = project_session_messages(events, include_reasoning=True, include_tool_events=True)
    assert [message["Content"]["text"] for message in messages] == ["same", "same"]
    assert [_visible_text(message) for message in messages] == ["same", "same"]


def _visible_text(message):
    blocks = message.get("Blocks")
    if blocks:
        return "".join(block["Content"] for block in blocks if block["Type"] == "text")
    return message["Content"]["text"]


@pytest.mark.parametrize("terminal", ["item.completed", "assistant_stream_snapshot"])
def test_authoritative_replacement_text_is_visible(terminal):
    events = [message_event(1, "item.updated", "draft")]
    events.append(
        message_event(2, terminal, "corrected")
        if terminal == "item.completed"
        else {
            "EventId": "event-2",
            "SeqId": 2,
            "InvocationId": "run-1",
            "EventType": terminal,
            "Author": "assistant",
            "Content": {"parts": [{"text": "corrected"}]},
        }
    )
    messages = project_session_messages(events, include_reasoning=True, include_tool_events=True)
    assert len(messages) == 1
    assert _visible_text(messages[0]) == "corrected"
