"""Bind interrupted batch dispositions to the original host transcript."""

from __future__ import annotations

import hashlib
import json

from .state import (
    CommitBatch,
    EffectIntent,
    EffectReceipt,
    InterruptedToolCall,
    RunSnapshot,
    ToolBatchInterruption,
)
from .transitions import insert_event_before_terminal


def tool_batch_digest(message: dict) -> str:
    raw = json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def record_tool_interruption(
    previous: RunSnapshot, batch: CommitBatch,
    intent: EffectIntent | None = None, receipt: EffectReceipt | None = None,
) -> CommitBatch:
    """Keep dispatch knowledge when a host transition clears an unfinished batch.

    No model/tool prose supplies execution authority. Claimed effects remain
    unknown; only calls outside that effect, or a prepared effect, are known
    undispatched. Original messages, receipts and admission counters stay intact.
    Legacy/malformed histories without an exact source keep their unknown fallback.
    """
    if (not previous.pending_tools or batch.snapshot.pending_tools
            or batch.snapshot.outcome is None):
        return batch
    pending = {call["call_id"] for call in previous.pending_tools}
    source = next((
        (index, message) for index, message in reversed(tuple(enumerate(previous.messages)))
        if message.get("role") == "assistant"
        and pending.issubset({call.get("id") for call in message.get("tool_calls", ())})
    ), None)
    if source is None:
        return batch
    index, message = source
    completed = {item.get("tool_call_id") for item in batch.snapshot.messages[index + 1:]
                 if item.get("role") == "tool"}
    pending -= completed
    if not pending:
        return batch
    scheduled, unknown = set(), set()
    if intent is not None and intent.kind == "tool":
        raw = intent.payload.get("calls")
        raw = raw if isinstance(raw, list) else [intent.payload.get("call")]
        scheduled = {call.get("call_id") for call in raw if isinstance(call, dict)}
        explicitly_undispatched = (
            receipt is not None and receipt.status == "failed"
            and receipt.result.get("reason") == "not_dispatched"
        )
        if not scheduled or None in scheduled:
            unknown = pending
        elif not explicitly_undispatched and (receipt is not None or intent.status == "running"):
            unknown = scheduled
    elif intent is None:
        if previous.pending_intent is not None:
            unknown = pending  # Missing host dispatch evidence is never a non-execution proof.
        elif previous.pending_interaction is not None:
            # A paused tool/child has already entered its Port. Its nonterminal
            # approval is not a committed execution result or rollback proof.
            unknown = {previous.pending_tools[0]["call_id"]}
    observation = ToolBatchInterruption(
        source_message_index=index, source_message_digest=tool_batch_digest(message),
        reason=batch.snapshot.reason or "interrupted_tool_batch",
        calls=tuple(InterruptedToolCall(
            call_id=call["call_id"],
            execution_status="unknown" if call["call_id"] in unknown else "not_dispatched",
            operation_key=(intent.operation_key if intent is not None
                           and call["call_id"] in scheduled else None),
        ) for call in previous.pending_tools if call["call_id"] in pending),
    )
    batch = batch.model_copy(update={"snapshot": batch.snapshot.model_copy(update={
        "tool_batch_interruption": observation,
    })})
    return insert_event_before_terminal(previous, batch, (
        "tools.interrupted", observation.model_dump(mode="json"),
    ))


def interrupted_tool_calls(parent: RunSnapshot, index: int, message: dict) -> dict:
    """Use a disposition only for its exact original assistant batch."""
    observation = parent.tool_batch_interruption
    if (observation is None or observation.source_message_index != index
            or observation.source_message_digest != tool_batch_digest(message)):
        return {}
    return {call.call_id: call for call in observation.calls}
