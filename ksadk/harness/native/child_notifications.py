"""Consume completed child facts before an undispatched parent model request.

ZCode 872ad960 turn-loop.ts drains sourced runtime notices before the next
model round. Readiness is a host hint; the original child effect still owns
result admission, usage settlement and model-only observations.
"""

from .child_lifecycle_contract import ChildReadyRequest, ChildReadyResult
from .ports import RunConfiguration
from .state import EffectIntent, EffectReceipt, RunPhase, RunSnapshot, move
from .transitions import commit_transition


def child_ready_probe(
    snapshot: RunSnapshot,
    config: RunConfiguration,
    intent: EffectIntent | None,
):
    if (
        config.child_notification_policy is None
        or snapshot.phase != RunPhase.REQUESTING_MODEL
        or intent is None
        or intent.kind != "model"
        or intent.status != "prepared"
        or snapshot.pending_intent != intent.operation_key
        or intent.payload.get("purpose") == "delivery_review"
    ):
        return None
    reservations = tuple(
        item for item in snapshot.child_budget_reservations if item.settled_usage_digest is None
    )
    if not reservations:
        return None
    return ChildReadyRequest(
        scope=snapshot.scope, run_id=snapshot.run_id, reservations=reservations,
        include_approvals=config.child_approval_policy is not None,
    )


def stage_child_ready(
    snapshot: RunSnapshot,
    intent: EffectIntent,
    request: ChildReadyRequest,
    result: ChildReadyResult,
):
    result = ChildReadyResult.model_validate_json(result.model_dump_json())
    allowed = {item.child_run_id for item in request.reservations}
    if len(result.child_run_ids) != len(set(result.child_run_ids)) or not set(
        result.child_run_ids
    ).issubset(allowed):
        raise ValueError("child_readiness_identity_conflict")
    if not result.child_run_ids:
        return None
    updated = move(snapshot, RunPhase.PREPARING_CONTEXT).model_copy(
        update={
            "pending_intent": None,
            "prepared_context": None,
            "child_lifecycle_cursor": None,
        }
    )
    # This request was prepared, never claimed or sent. Preserve all Run
    # counters and close its original intent in the same Store transition.
    receipt = EffectReceipt(
        operation_key=intent.operation_key, status="failed", result={"reason": "not_dispatched"}
    )
    return commit_transition(
        snapshot,
        updated,
        "child.ready_observed",
        {
            "child_run_ids": list(result.child_run_ids),
            "operation_key": intent.operation_key,
        },
        receipts=(receipt,),
    )
