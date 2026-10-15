"""Atomic transition construction; no I/O or provider knowledge."""

from __future__ import annotations

import hashlib

from pydantic import JsonValue

from ksadk.harness.tool_display_contract import tool_display_start

from .state import (
    CommitBatch,
    EffectIntent,
    EffectReceipt,
    NativeEvent,
    RunPhase,
    RunSnapshot,
    move,
)


def commit_transition(
    previous: RunSnapshot,
    updated: RunSnapshot,
    kind: str,
    payload: dict[str, JsonValue] | None = None,
    *,
    intents: tuple[EffectIntent, ...] = (),
    receipts: tuple[EffectReceipt, ...] = (),
    command_id: str | None = None,
    extra_events: tuple[tuple[str, dict[str, JsonValue]], ...] = (),
) -> CommitBatch:
    revision = previous.revision + 1
    scope_key = hashlib.sha256(previous.scope.model_dump_json().encode()).hexdigest()
    entries = ((kind, payload or {}), *extra_events)
    if previous.event_seq == 0 and kind != "run.started":
        entries = (("run.started", {}), *entries)
    events = tuple(
        NativeEvent(
            event_id=f"{scope_key}:{previous.run_id}:{previous.event_seq + i}",
            run_id=previous.run_id,
            turn_id=previous.turn_id,
            seq=previous.event_seq + i,
            kind=name,
            payload=data,
        )
        for i, (name, data) in enumerate(entries, 1)
    )
    return CommitBatch(
        commit_id=f"{previous.run_id}:revision:{revision}",
        snapshot=updated.model_copy(update={"revision": revision, "event_seq": events[-1].seq}),
        events=events,
        intents=intents,
        receipts=receipts,
        consumed_command_id=command_id,
    )


def insert_event_before_terminal(
    previous: RunSnapshot,
    batch: CommitBatch,
    event: tuple[str, dict[str, JsonValue]],
) -> CommitBatch:
    """Rebuild one command commit with an accounting fact before its terminal."""
    if previous.event_seq == 0:
        raise ValueError("cannot rebuild a transition before run.started")
    entries = [(item.kind, item.payload) for item in batch.events]
    terminal_kinds = {"run.completed", "run.failed", "run.canceled", "run.interrupted"}
    index = next((i for i, item in enumerate(entries) if item[0] in terminal_kinds), len(entries))
    entries.insert(index, event)
    return commit_transition(
        previous,
        batch.snapshot,
        entries[0][0],
        entries[0][1],
        intents=batch.intents,
        receipts=batch.receipts,
        command_id=batch.consumed_command_id,
        extra_events=tuple(entries[1:]),
    )


def finish(snapshot: RunSnapshot, outcome: str, reason: str) -> RunSnapshot:
    return move(snapshot, RunPhase.FINALIZING).model_copy(
        update={
            "outcome": outcome,
            "reason": reason,
            "pending_intent": None,
            "pending_interaction": None,
            "interaction": None,
            "interaction_host_id": None,
            "child_approval": None,
            "pending_tools": (),
            "final_text": (snapshot.candidate or {}).get("final_text", ""),
        }
    )


def effect_operation_key(snapshot: RunSnapshot) -> str:
    return f"{snapshot.run_id}:effect:{snapshot.revision + 1}"


def prepare_effect(snapshot: RunSnapshot, kind: str, payload: dict[str, JsonValue]) -> CommitBatch:
    operation_key = effect_operation_key(snapshot)
    child_intents = ()
    if kind == "tool" and payload.get("parallel_readonly") is True:
        calls = payload.get("calls")
        if not isinstance(calls, list) or len(calls) < 2:
            raise ValueError("parallel effect needs a bounded call batch")
        payload = {**payload, "parallel_artifact_base": [
            ref["uri"] for ref in snapshot.artifact_refs
        ]}
        child_intents = tuple(
            EffectIntent(
                operation_key=f"{operation_key}:call:{index + 1}",
                kind="tool",
                payload={"call": call},
            )
            for index, call in enumerate(calls)
        )
    intent = EffectIntent(operation_key=operation_key, kind=kind, payload=payload)
    updated = snapshot.model_copy(update={"pending_intent": operation_key})
    event_payload: dict[str, JsonValue] = {"operation_key": operation_key, "kind": kind}
    if kind == "model":
        event_payload["model_purpose"] = (
            payload.get("purpose") if payload.get("purpose") in {"compaction", "delivery_review"}
            else "main"
        )
    if kind == "tool":
        raw_calls = payload.get("calls")
        if not isinstance(raw_calls, list):
            raw_calls = [payload.get("call")]
        # The durable trace needs call identity for replay and cancellation,
        # but must not copy model-supplied arguments into public events.
        event_payload["tool_calls"] = [
            {"call_id": call["call_id"], "name": call["name"],
             "tool_display": tool_display_start(call["name"], call.get("arguments"))}
            for call in raw_calls
            if isinstance(call, dict)
            and isinstance(call.get("call_id"), str)
            and isinstance(call.get("name"), str)
            and call["call_id"]
            and call["name"]
        ]
    return commit_transition(
        snapshot,
        updated,
        "effect.prepared",
        event_payload,
        intents=(intent, *child_intents),
    )
