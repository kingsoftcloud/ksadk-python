"""Resolve a control command against any pending effect before one atomic commit."""

from __future__ import annotations

from ksadk.harness.stores.native_contract import RunStore

from .machine import reduce_command
from .model_runtime import unavailable_uncertain_usage
from .ports import RunConfiguration
from .state import CommitBatch, EffectReceipt, RunCommand, RunPhase, RunScope, RunSnapshot
from .tool_interruption import record_tool_interruption
from .transitions import commit_transition, finish, insert_event_before_terminal


def resolve_command_batch(
    store: RunStore,
    scope: RunScope,
    snapshot: RunSnapshot,
    command: RunCommand,
    *,
    bound_tool_names: frozenset[str] | None = None,
    guidance_enabled: bool = False,
    replay_configuration: RunConfiguration | None = None,
) -> CommitBatch:
    batch = reduce_command(snapshot, command, bound_tool_names=bound_tool_names,
                           guidance_enabled=guidance_enabled,
                           replay_configuration=replay_configuration)
    key = snapshot.pending_intent
    if batch.snapshot.pending_intent is not None:
        return batch
    if not key:
        return record_tool_interruption(snapshot, batch)
    intent = store.get_intent(scope, snapshot.run_id, key)
    receipt = store.get_receipt(scope, snapshot.run_id, key)
    if receipt is not None or intent is None:
        return record_tool_interruption(snapshot, batch, intent, receipt)
    if intent.status == "running" and intent.kind != "workspace" and command.kind == "steer":
        batch = commit_transition(
            snapshot,
            finish(snapshot, "unverified", "effect_outcome_unknown"),
            "command.consumed",
            {
                "command_id": command.command_id,
                "kind": "steer",
                "reason": "effect_outcome_unknown",
            },
            command_id=command.command_id,
        )
    receipt = EffectReceipt(
        operation_key=key,
        status=(
            "failed" if intent.status == "prepared" or intent.kind == "workspace" else "uncertain"
        ),
        result={
            "reason": (
                "not_dispatched"
                if intent.status == "prepared"
                else (
                    "read_only_observation_discarded"
                    if intent.kind == "workspace"
                    else "effect_outcome_unknown"
                )
            )
        },
    )
    if receipt.status == "uncertain" and intent.kind == "model":
        batch = insert_event_before_terminal(snapshot, batch, unavailable_uncertain_usage(key))
    updated = batch.snapshot
    if intent.status == "running" and intent.kind in {"model", "tool"}:
        counter = "model_calls" if intent.kind == "model" else "tool_calls"
        updated = updated.model_copy(update={
            counter: getattr(snapshot, counter) + intent.loop_attempts,
        })
    return record_tool_interruption(
        snapshot, batch.model_copy(update={"receipts": (receipt,), "snapshot": updated}),
        intent, receipt,
    )


def stage_child_cancellation(store, scope, snapshot, command, *, bound_tool_names):
    """Close the parent effect, retaining its command until children settle.

    Reuse ordinary command accounting and protocol closure. The intermediate
    commit is nonterminal, and neither consumes nor re-enqueues cancellation.
    """
    batch = resolve_command_batch(
        store, scope, snapshot, command, bound_tool_names=bound_tool_names,
    )
    updated = batch.snapshot.model_copy(update={
        "phase": RunPhase.FINALIZING, "outcome": "unverified",
        "reason": "parent_child_cleanup_pending",
    })
    accounting = tuple((event.kind, event.payload) for event in batch.events
                       if event.kind.startswith("usage.") or event.kind == "tools.interrupted")
    return commit_transition(
        snapshot, updated, "child.parent_cancel_pending",
        {"command_id": command.command_id, "kind": command.kind},
        receipts=batch.receipts, extra_events=accounting,
    )
