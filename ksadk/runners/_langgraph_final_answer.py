"""Complete explicit graph answers that have no model-message stream."""

import time
from collections.abc import Mapping

from ksadk.events.canonical import ItemCompleted, ItemStarted
from ksadk.events.content import ContentSnapshot, DataContent, TextContent
from ksadk.events.identity import stable_event_id, stable_item_id
from ksadk.runners._langgraph_output import extract_output


def answer_update_fields(event) -> set[str]:
    """Observe root writes, including writes equal to the checkpoint's value.

    Values snapshots include inherited state; nested updates can describe a
    child's private output. Only root node updates establish answer ownership.
    """
    if not (
        isinstance(event, ItemCompleted)
        and event.item_kind == "data"
        and event.source.metadata.get("channel") == "updates"
        and event.source.metadata.get("namespace") == []
    ):
        return set()
    return {
        field
        for part in event.snapshot.parts
        if isinstance(part, DataContent) and isinstance(part.data, Mapping)
        for node, update in part.data.items()
        if node != "__metadata__" and isinstance(update, Mapping)
        for field in ("output", "answer", "messages")
        if field in update
    }


def final_answer_events(
    state, projection, context, scope_id, source, *, updated_fields, needs_recovery,
):
    completed_answer = any(
        item.item_kind == "message"
        and item.phase == "final_answer"
        and item.status == "completed"
        for item in projection.items
    )
    if needs_recovery:
        # A canceled internal model may coexist with a valid current answer.
        # The final state alone cannot tell an inherited output from a recovery
        # write (and an explicit write may legitimately repeat the old value).
        explicit = {key: state[key] for key in ("output", "answer")
                    if key in updated_fields and isinstance(state, Mapping) and key in state}
        text = extract_output(explicit)
        if (not text and not completed_answer and "messages" in updated_fields
                and isinstance(state, Mapping)):
            text = extract_output({"messages": state.get("messages")})
    elif completed_answer:
        return ()
    else:
        text = extract_output(state)
    if not text:
        return ()
    item_id = stable_item_id(
        "langgraph", context.run_id, "explicit-answer", context.publication_epoch or "initial"
    )
    content = ContentSnapshot(parts=(TextContent(part_id="answer", text=text),))
    common = dict(
        schema_version=2,
        timestamp=time.time(),
        run_id=context.run_id,
        scope_id=scope_id,
        source=source,
        item_id=item_id,
        item_kind="message",
    )
    return (
        ItemStarted(
            **common,
            phase="final_answer",
            seq=context.allocate_placeholder_seq(),
            event_id=stable_event_id(
                "langgraph", scope_id, item_id, "item.started", "answer", item_id, 0
            ),
        ),
        ItemCompleted(
            **common,
            snapshot=content,
            seq=context.allocate_placeholder_seq(),
            event_id=stable_event_id(
                "langgraph", scope_id, item_id, "item.completed", "answer", item_id, 0
            ),
        ),
    )
