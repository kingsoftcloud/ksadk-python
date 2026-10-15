"""Close the original expert item from verified rollout output, never a fake RPC."""

import hashlib
import time

from ksadk.events.canonical import ErrorInfo, ItemCompleted, ItemFailed
from ksadk.events.content import ContentSnapshot, ToolCallContent, ToolResultContent
from ksadk.events.identity import stable_event_id, stable_part_id


async def recovered_expert_terminal(authority, call, rollout, response):
    rollout.require_call(
        turn_id=call.turn_id, call_id=call.call_id, tool=call.tool, arguments=call.arguments
    )
    items = response.get("contentItems", [])
    if (
        len(items) != 1
        or items[0].get("type") != "inputText"
        or not isinstance(items[0].get("text"), str)
        or not isinstance(response.get("success"), bool)
        or rollout.function_output(call.call_id) != items[0]["text"]
    ):
        raise ValueError("A2A_CODEX_RECOVERY_OUTPUT_CONFLICT")
    events = await authority.list(authority.session_id)
    triggers = {}
    for event in events:
        source = event.source
        if (
            source.framework != "codex"
            or source.native_run_id != call.turn_id
            or source.native_item_id != call.call_id
            or source.metadata.get("thread_id") != call.thread_id
        ):
            continue
        snapshot = getattr(event, "initial", None) or getattr(event, "snapshot", None)
        if getattr(event, "item_kind", None) != "tool_call" or snapshot is None:
            continue
        if any(
            isinstance(part, ToolCallContent)
            and part.call_id == call.call_id
            and part.name == call.tool
            and part.arguments == {"arguments": call.arguments, "namespace": None}
            for part in snapshot.parts
        ):
            triggers.setdefault((event.scope_id, event.item_id), event)
    if len(triggers) != 1:
        raise ValueError("A2A_CODEX_NATIVE_TRIGGER_NOT_PERSISTED")
    (scope, item), original = next(iter(triggers.items()))
    for event in events:
        if event.scope_id != scope or getattr(event, "item_id", None) != item:
            continue
        if isinstance(event, ItemFailed):
            if response["success"]:
                raise ValueError("A2A_CODEX_RECOVERY_TOOL_TERMINAL_CONFLICT")
            return None
        if isinstance(event, ItemCompleted):
            results = [part for part in event.snapshot.parts if isinstance(part, ToolResultContent)]
            if (
                len(results) != 1
                or results[0].call_id != call.call_id
                or not isinstance(results[0].result, dict)
                or results[0].result.get("contentItems") != items
                or results[0].result.get("success") != response["success"]
            ):
                raise ValueError("A2A_CODEX_RECOVERY_TOOL_TERMINAL_CONFLICT")
            return None
    source = original.source.model_copy(
        update={
            "native_event_id": None,
            "native_cursor": "rollout-output:" + rollout.sha256,
            "metadata": {
                **original.source.metadata,
                "recovery_method": "native_rollout_output",
                "native_callback_replayed": False,
            },
        }
    )
    event_type = "item.completed" if response["success"] else "item.failed"
    values = dict(
        schema_version=2,
        run_id=authority.run_id,
        scope_id=scope,
        parent_scope_id=original.parent_scope_id,
        item_id=item,
        item_kind="tool_call",
        seq=0,
        timestamp=time.time(),
        source=source,
        event_id=stable_event_id(
            "codex",
            scope,
            item,
            event_type,
            "recovered-output",
            hashlib.sha256(items[0]["text"].encode()).hexdigest(),
            0,
        ),
    )
    if not response["success"]:
        return ItemFailed(
            **values,
            error=ErrorInfo(
                code="A2A_CODEX_RECOVERED_EXPERT_FAILED",
                message="Recovered expert invocation failed",
                source="codex",
                scope_id=scope,
                item_id=item,
                source_ref=source,
            ),
        )
    snapshot = getattr(original, "initial", None) or original.snapshot
    result = ToolResultContent(
        part_id=stable_part_id("codex", item, "tool_result", "primary"),
        call_id=call.call_id,
        result={"status": "completed", "success": True, "contentItems": items, "durationMs": None},
    )
    return ItemCompleted(
        **values,
        snapshot=ContentSnapshot(
            parts=tuple(part for part in snapshot.parts if not isinstance(part, ToolResultContent))
            + (result,),
        ),
    )
