from __future__ import annotations

from ksadk.conversations.context import (
    project_model_messages,
    project_responses_history,
)
from ksadk.events.canonical import ItemCompleted, SourceRef
from ksadk.events.canonical_store import runtime_event_to_session_event
from ksadk.events.content import ContentSnapshot, TextContent


def _completed_message(*, event_id: str, item_id: str, seq: int, text: str):
    event = ItemCompleted(
        schema_version=2,
        event_id=event_id,
        seq=seq,
        timestamp=1_788_000_000.0 + seq,
        run_id="run-1",
        scope_id="scope-1",
        source=SourceRef(framework="ksadk"),
        item_id=item_id,
        item_kind="message",
        snapshot=ContentSnapshot(
            parts=(TextContent(part_id=f"{item_id}-text", text=text),)
        ),
    )
    return runtime_event_to_session_event("session-1", event)


def test_canonical_completed_messages_project_as_assistant_by_identity() -> None:
    first = _completed_message(
        event_id="completed-1",
        item_id="message-1",
        seq=1,
        text="same answer",
    )
    second = _completed_message(
        event_id="completed-2",
        item_id="message-2",
        seq=2,
        text="same answer",
    )

    # A replay of the same canonical fact must be idempotent, while a distinct
    # item with identical author/text remains a distinct assistant message.
    events = [first, first, second]

    assert project_model_messages(events, assistant_role="assistant") == [
        {"role": "assistant", "content": "same answer"},
        {"role": "assistant", "content": "same answer"},
    ]
    assert project_responses_history(events) == [
        {
            "role": "assistant",
            "content": [{"type": "input_text", "text": "same answer"}],
        },
        {
            "role": "assistant",
            "content": [{"type": "input_text", "text": "same answer"}],
        },
    ]
