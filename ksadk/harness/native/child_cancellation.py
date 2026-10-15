"""Cancel one original owned child, preserving parent execution and accounting."""

from .child_promotion import ChildPromotionInput
from .state import RunPhase
from .transitions import commit_transition


async def consume_child_cancellation(snapshot, command, *, enabled, port, commit):
    child_id, reason = None, "child_cancellation_unavailable"
    if enabled:
        try:
            child_id = ChildPromotionInput.model_validate(command.payload).child_run_id
        except ValueError:
            reason = "invalid_child_cancellation"
        else:
            reason = "requested" if port.can_cancel(snapshot, child_id) else "child_unavailable"
    target = snapshot
    extra = ()
    if reason == "requested":
        extra = (
            (
                "child.cancel_requested",
                {
                    "child_run_id": child_id,
                    "command_id": command.command_id,
                },
            ),
        )
        approval = snapshot.child_approval
        if (
            approval is not None
            and approval.child_run_id == child_id
            and snapshot.pending_interaction
        ):
            # Cancel this obsolete request, never submit an allow/deny decision
            # to the protected child tool. An explicit parent pause stays paused.
            resume = snapshot.resume_phase or RunPhase.EXECUTING_TOOLS
            updates = {
                "pending_interaction": None,
                "interaction_host_id": None,
                "interaction": None,
                "child_approval": None,
                "resume_phase": None,
            }
            if snapshot.phase == RunPhase.AWAITING_INTERACTION:
                updates["phase"] = resume
            elif (
                snapshot.phase == RunPhase.PAUSED
                and snapshot.paused_from == RunPhase.AWAITING_INTERACTION
            ):
                updates["paused_from"] = resume
            target = snapshot.model_copy(update=updates)
            extra = (
                *extra,
                (
                    "approval.resolved",
                    {
                        "approval_id": snapshot.pending_interaction,
                        "call_id": approval.tool_call.call_id,
                        "decision": "canceled",
                        "reason": "child_cancel_requested",
                    },
                ),
            )
    updated = commit(
        snapshot,
        commit_transition(
            snapshot,
            target,
            "command.consumed",
            {"command_id": command.command_id, "kind": command.kind, "reason": reason},
            command_id=command.command_id,
            extra_events=extra,
        ),
    )
    if reason == "requested":
        # Only revoke the child's locally owned task. Its original runner
        # settles cancellation and usage; this does not fabricate a terminal.
        await port.cancel_child(updated, child_id)
    return updated
