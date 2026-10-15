"""Original owner and atomic commit boundary for user-to-child control."""

from pydantic import ValidationError

from .child_input import ChildInput, ChildInputRejected, delivery_for
from .transitions import commit_transition


def consume_child_input(snapshot, command, *, enabled, port, commit):
    reason, delivery = "child_input_policy_unavailable", None
    if enabled:
        try:
            value = ChildInput.model_validate(command.payload)
            target = port.child_input_target(snapshot, value.child_run_id)
            if target is None:
                reason = "child_owner_unavailable_or_not_authorized"
            else:
                delivery = delivery_for(snapshot, command, target)
                reason = "queued"
        except ValidationError:
            reason = "invalid_child_input"
    payload = {"command_id": command.command_id, "kind": command.kind, "reason": reason}
    extra = (
        (
            (
                "child.input_queued",
                {
                    "command_id": command.command_id,
                    "child_run_id": delivery.target.run_id,
                    "child_input_id": delivery.command.command_id,
                    "child_scope": delivery.target.scope.model_dump(mode="json"),
                    "parent_operation_key": delivery.target.parent_operation_key,
                },
            ),
        )
        if delivery
        else ()
    )
    batch = commit_transition(
        snapshot,
        snapshot,
        "command.consumed",
        payload,
        command_id=command.command_id,
        extra_events=extra,
    )
    try:
        updated = commit(snapshot, batch, child_input=delivery)
    except ChildInputRejected as exc:
        payload["reason"] = str(exc)
        return commit(
            snapshot,
            commit_transition(
                snapshot, snapshot, "command.consumed", payload, command_id=command.command_id
            ),
        )
    if delivery is not None:
        port.wake_child_input(updated, delivery.target.run_id)
    return updated
