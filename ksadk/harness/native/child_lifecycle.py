"""Host child collection at parent safe points; the Store remains the ledger.

ZCode 872ad960 subagent-messages.ts persists sourced, model-only replies and
runner.ts owns child cancellation. Here collection is a parent effect, not a
second registry, a user goal or a synthetic model-selected tool call.
"""

from __future__ import annotations

import json

from .child_budget import apply_child_budget
from .child_lifecycle_contract import ChildLifecyclePort as ChildLifecyclePort
from .child_lifecycle_contract import ChildLifecycleRequest as ChildLifecycleRequest
from .child_lifecycle_contract import ChildLifecycleResult as ChildLifecycleResult
from .completion_text import candidate_model_message
from .ports import RunConfiguration, ToolDefinition
from .state import EffectIntent, EffectReceipt, RunPhase, RunSnapshot, move
from .transitions import finish, prepare_effect


def lifecycle_cursor(snapshot: RunSnapshot) -> str:
    return f"{snapshot.goal_revision}:{snapshot.model_calls}:{snapshot.tool_calls}"


def recover_paused_child_observation(
    snapshot: RunSnapshot,
    intent: EffectIntent,
    receipt: EffectReceipt,
    config: RunConfiguration,
):
    """Close only a joined observation; original children and usage stay owned.

    ZCode's registry wait abort removes its waiter, while stopTask separately
    cancels child execution. An unknown or unjoined effect cannot use this path.
    """
    if (
        config.child_lifecycle_policy != "owned-background/v2"
        or intent.kind != "child"
        or receipt.status != "failed"
        or receipt.result != {"reason": "child_observation_paused"}
        or receipt.operation_key != intent.operation_key
        or snapshot.pending_intent != intent.operation_key
    ):
        return None
    request = ChildLifecycleRequest.model_validate(intent.payload["request"])
    if request.action not in {"poll", "wait"}:
        return None
    if (request.scope != snapshot.scope or request.run_id != snapshot.run_id
            or request.operation_key != intent.operation_key):
        raise ValueError("child_observation_identity_conflict")
    return snapshot.model_copy(update={"pending_intent": None, "child_lifecycle_cursor": None})


def lifecycle_request(snapshot, operation_key, action):
    return ChildLifecycleRequest(
        scope=snapshot.scope,
        run_id=snapshot.run_id,
        operation_key=operation_key,
        action=action,
        reservations=tuple(
            item for item in snapshot.child_budget_reservations if item.settled_usage_digest is None
        ),
        child_approval=snapshot.child_approval if action != "cancel" else None,
        interaction=snapshot.interaction if action != "cancel" else None,
    )


def prepare_child_lifecycle(snapshot: RunSnapshot, config: RunConfiguration, *, cancel=False):
    if (
        config.child_lifecycle_policy is None
        or snapshot.pending_intent
        or not any(item.settled_usage_digest is None for item in snapshot.child_budget_reservations)
    ):
        return None
    action = (
        "cancel"
        if cancel or snapshot.phase == RunPhase.FINALIZING
        else ("wait" if snapshot.phase == RunPhase.VERIFYING else "poll")
    )
    if snapshot.child_lifecycle_failure == "cancel" or (
        snapshot.child_lifecycle_failure and action != "cancel"
    ):
        return None
    if action == "poll" and (
        snapshot.phase != RunPhase.PREPARING_CONTEXT
        or (snapshot.child_lifecycle_cursor == lifecycle_cursor(snapshot)
            and snapshot.child_approval is None)
    ):
        return None
    key = f"{snapshot.run_id}:effect:{snapshot.revision + 1}"
    request = lifecycle_request(snapshot, key, action)
    return prepare_effect(snapshot, "child", {"request": request.model_dump(mode="json")})


def validate_child_lifecycle_result(request, result):
    result = ChildLifecycleResult.model_validate_json(result.model_dump_json())
    requested = {item.child_run_id for item in request.reservations}
    settled = [
        item.child_budget_settlement.child_run_id
        for item in result.results
        if item.child_budget_settlement is not None
    ]
    pending = result.pending_child_run_ids
    approvals = [item.child_run_id for item in result.approvals]
    if (
        result.operation_key != request.operation_key
        or len(requested) != len(request.reservations)
        or len(settled) != len(result.results)
        or len(set(settled)) != len(settled)
        or len(set(pending)) != len(pending)
        or set(settled) & set(pending)
        or set(settled) | set(pending) != requested
        or any(item.child_budget_reservation is not None for item in result.results)
        or len(approvals) != len(set(approvals))
        or not set(approvals).issubset(pending)
        or (request.action == "cancel" and approvals)
    ):
        raise ValueError("child_lifecycle_result_identity_conflict")
    from .child_approval import validate_approval_allocation
    for approval in result.approvals:
        validate_approval_allocation(request.reservations, approval)
    return result


_SETTLEMENT_AUTHORITY = ToolDefinition(
    name="host_child_collection",
    description="Host child lifecycle receipt",
    parameters={"type": "object"},
    child_budget_policy="settle/v1",
)


def reduce_child_lifecycle(snapshot, request, result, config=None):
    """Consume each actual usage once; findings cannot verify the parent task."""
    result = validate_child_lifecycle_result(request, result)
    updated = snapshot.model_copy(
        update={
            "pending_intent": None,
            "child_lifecycle_cursor": lifecycle_cursor(snapshot),
            **({"child_approval": None, "interaction": None, "interaction_host_id": None}
               if request.action != "cancel" else {}),
        }
    )
    notices, usage_events = [], []
    for item in result.results:
        updated, usage = apply_child_budget(updated, item, _SETTLEMENT_AUTHORITY)
        if not usage.child_run_ids:
            continue
        updated = updated.model_copy(
            update={
                "descendant_model_calls": updated.descendant_model_calls + usage.model_calls,
                "descendant_tool_calls": updated.descendant_tool_calls + usage.tool_calls,
            }
        )
        known = all(
            value is not None
            for value in (
                usage.input_tokens,
                usage.output_tokens,
                usage.cached_input_tokens,
                usage.reasoning_tokens,
            )
        )
        child_id = item.child_budget_settlement.child_run_id
        usage_events.append(
            (
                "usage.reported" if known else "usage.unavailable",
                {
                    "semantics": "delta",
                    "request_id": request.operation_key,
                    "attempt_id": request.operation_key + ":child:" + child_id,
                    "source": "descendant_runs",
                    **usage.model_dump(mode="json"),
                },
            )
        )
        if request.action != "cancel":
            content = json.dumps(item.content, ensure_ascii=False, sort_keys=True)
            notices.append(
                {
                    "role": "user",
                    "kind": "background_child_observation",
                    "child_run_id": child_id,
                    "receipt_ref": request.operation_key,
                    "content": json.dumps(
                        {
                            "source": "child_result",
                            "trust": "data",
                            "status": item.status,
                            "parent_acceptance": "not_evaluated",
                            "child_run_id": child_id,
                            "result": content[:8192],
                            "text_truncated": len(content) > 8192,
                            "result_tool": "native_child_result",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                }
            )
    if notices:
        messages = updated.messages
        if snapshot.phase == RunPhase.VERIFYING:
            messages = (*messages, candidate_model_message(snapshot))
            updated = move(updated, RunPhase.PREPARING_CONTEXT).model_copy(
                update={
                    "candidate": None,
                    "candidate_replay": None,
                    "verification": None,
                    "prepared_context": None,
                }
            )
        updated = updated.model_copy(
            update={
                "messages": (*messages, *notices),
                "pending_child_observation_ids": tuple(
                    dict.fromkeys(
                        (
                            *updated.pending_child_observation_ids,
                            *(item["child_run_id"] for item in notices),
                        )
                    )
                ),
            }
        )
    if result.approvals and not result.error_code:
        from .child_approval import stage_child_approval

        if config is None or config.child_approval_policy is None:
            raise ValueError("child_approval_policy_unavailable")
        updated = stage_child_approval(updated, result.approvals[0],
                                       operation_key=request.operation_key,
                                       resume_phase=updated.phase)
    elif result.error_code or (request.action != "poll" and result.pending_child_run_ids):
        updated = finish(
            updated.model_copy(
                update={
                    "candidate": None,
                    "child_lifecycle_failure": request.action,
                }
            ),
            "unverified",
            result.error_code or "background_children_unsettled",
        )
    return updated, tuple(usage_events)
