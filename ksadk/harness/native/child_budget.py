"""Pure parent allocation accounting; child executors never write this ledger."""

from __future__ import annotations

import hashlib
import json

from .ports import ToolDefinition, ToolRequest, ToolResourceUsage, ToolResult
from .state import RunSnapshot


def _settlement_digest(result: ToolResult) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "settlement": result.child_budget_settlement.model_dump(mode="json"),
                "usage": result.resource_usage.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def validate_child_budget_admission(request: ToolRequest, result: ToolResult) -> None:
    reservation = result.child_budget_reservation
    if reservation is None:
        return
    remaining = request.budget
    model_slots = remaining.max_model_calls_remaining - (
        0 if remaining.parent_finalization_reserved else 1
    )
    if (
        request.definition.child_budget_policy not in {"start/v1", "delegate/v1"}
        or reservation.parent_operation_key != request.operation_key
        or reservation.parent_tool_call_id != request.call.call_id
        or reservation.max_model_calls > model_slots
        or reservation.max_tool_calls + (
            # Dispatcher grants already exclude the invoking parent calls.
            # Legacy start also reserves an explicit collection/control slot;
            # owned delegates use host collection without an extra tool fee.
            0 if request.definition.child_budget_policy == "delegate/v1" else 1
        ) > remaining.max_tool_calls_remaining
    ):
        # An owner breached its admission contract after dispatch. Do not
        # silently accept this or encourage a second, unaccounted launch.
        raise ValueError("child_budget_start_exceeds_admission")


def apply_child_budget(
    snapshot: RunSnapshot,
    result: ToolResult,
    definition: ToolDefinition | None,
    *,
    parent_operation_key: str | None = None,
    parent_tool_call_id: str | None = None,
) -> tuple[RunSnapshot, ToolResourceUsage]:
    """Return the ledger and the actual delta owed by this receipt.

    Model-visible JSON cannot create reservations or release them. Repeated
    collection preserves its own tool cost but has zero descendant usage.
    """
    reservation, settlement = result.child_budget_reservation, result.child_budget_settlement
    if reservation is not None:
        if definition is None or definition.child_budget_policy not in {"start/v1", "delegate/v1"}:
            raise ValueError("child_budget_start_not_admitted")
        operation = parent_operation_key or snapshot.pending_intent
        call_id = parent_tool_call_id or (
            snapshot.pending_tools[0]["call_id"] if snapshot.pending_tools else None
        )
        if (
            reservation.parent_operation_key != operation
            or reservation.parent_tool_call_id != call_id
            or any(
                getattr(reservation.child_scope, key) != getattr(snapshot.scope, key)
                for key in ("tenant_id", "workspace_id", "user_id")
            )
            or reservation.child_run_id == snapshot.run_id
            or any(
                child.child_run_id == reservation.child_run_id
                or child.parent_operation_key == reservation.parent_operation_key
                for child in snapshot.child_budget_reservations
            )
            or reservation.child_run_id in snapshot.child_run_ids
        ):
            raise ValueError("child_budget_start_identity_conflict")
        snapshot = snapshot.model_copy(
            update={
                "child_budget_reservations": (*snapshot.child_budget_reservations, reservation),
                "child_run_ids": (*snapshot.child_run_ids, reservation.child_run_id),
            }
        )
    if settlement is None:
        # A receipt for an allocated child must use settlement identity. It
        # cannot charge that usage through the older synchronous receipt path.
        if any(
            child.child_run_id in result.resource_usage.child_run_ids
            for child in snapshot.child_budget_reservations
        ):
            raise ValueError("child_budget_settlement_identity_required")
        return snapshot, result.resource_usage
    if definition is None or definition.child_budget_policy != "settle/v1":
        raise ValueError("child_budget_settlement_not_admitted")
    reserved = next(
        (
            child
            for child in snapshot.child_budget_reservations
            if child.child_run_id == settlement.child_run_id
        ),
        None,
    )
    usage = result.resource_usage
    if (
        reserved is None
        or reserved.parent_operation_key != settlement.parent_operation_key
        or reserved.child_request_digest != settlement.child_request_digest
        or usage.child_run_ids.count(settlement.child_run_id) != 1
        or usage.model_calls > reserved.max_model_calls
        or usage.tool_calls > reserved.max_tool_calls
    ):
        raise ValueError("child_budget_settlement_identity_or_limit_conflict")
    sealed = _settlement_digest(result)
    if reserved.settled_usage_digest is not None:
        if reserved.settled_usage_digest != sealed:
            raise ValueError("child_budget_settlement_changed")
        return snapshot, ToolResourceUsage()
    return snapshot.model_copy(
        update={
            "child_budget_reservations": tuple(
                child.model_copy(update={"settled_usage_digest": sealed})
                if child == reserved
                else child
                for child in snapshot.child_budget_reservations
            ),
        }
    ), usage
