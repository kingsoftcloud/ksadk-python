"""Pure admission/projection of a host-owned, already reserved child's pause."""

import hashlib
import json

from .state import ChildApproval, RunPhase, move


def host_id(approval: ChildApproval) -> str:
    return (
        "native-child-"
        + hashlib.sha256(
            json.dumps(approval.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
    )


def validate_child_approval(snapshot, approval: ChildApproval) -> None:
    validate_approval_allocation(snapshot.child_budget_reservations, approval)


def validate_approval_allocation(reservations, approval: ChildApproval) -> None:
    if not any(
        item.child_run_id == approval.child_run_id
        and item.child_scope == approval.child_scope
        and item.parent_operation_key == approval.parent_operation_key
        and item.parent_tool_call_id == approval.parent_tool_call_id
        and item.settled_usage_digest is None
        for item in reservations
    ):
        raise ValueError("child_approval_not_reserved")


def stage_child_approval(snapshot, approval, *, operation_key, resume_phase):
    validate_child_approval(snapshot, approval)
    return move(snapshot, RunPhase.AWAITING_INTERACTION).model_copy(
        update={
            "resume_phase": resume_phase,
            "pending_interaction": operation_key + ":approval",
            "interaction_host_id": host_id(approval),
            "interaction": None,
            "child_approval": approval,
        }
    )


def approval_event(snapshot):
    approval = snapshot.child_approval
    return "approval.requested", {
        "approval_id": snapshot.pending_interaction,
        "call_id": approval.tool_call.call_id,
        "kind": "tool",
        "detail": {
            "tool_name": approval.tool_call.name,
            "arguments": approval.tool_call.arguments,
            "source": "child_tool",
            "child_run_id": approval.child_run_id,
            "child_scope": approval.child_scope.model_dump(mode="json"),
        },
    }
