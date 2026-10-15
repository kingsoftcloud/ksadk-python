"""Host control of a waiting owned child, using the original command ledger.

OpenCode fe3f3a41 task.ts races foreground waiting with promotion of the same
background owner. Promotion changes delivery only; it never launches a Run,
replays a tool, changes authorization or settles the child's usage.
"""

from typing import Protocol

from pydantic import Field

from .state import Contract, RunSnapshot
from .transitions import commit_transition


class ChildPromotionInput(Contract):
    child_run_id: str = Field(min_length=1, max_length=256, pattern=r"\S")


class ChildPromotionPort(Protocol):
    def child_tasks(self, parent: RunSnapshot) -> tuple[dict, ...]: ...

    def can_promote(self, parent: RunSnapshot, child_run_id: str) -> bool: ...

    def release_foreground(self, parent: RunSnapshot, child_run_id: str) -> None: ...


def consume_promotion(snapshot, command, *, enabled: bool, port: ChildPromotionPort | None, commit):
    """Persist the delivery fact before waking the same child owner's waiter."""
    reason, child_id = "child_promotion_policy_unavailable", None
    if enabled:
        try:
            child_id = ChildPromotionInput.model_validate(command.payload).child_run_id
        except ValueError:
            reason = "invalid_child_promotion"
        else:
            reason = (
                "applied"
                if port.can_promote(snapshot, child_id)
                else "foreground_child_unavailable"
            )
    updated = commit(
        snapshot,
        commit_transition(
            snapshot,
            snapshot,
            "command.consumed",
            {"command_id": command.command_id, "kind": command.kind, "reason": reason},
            command_id=command.command_id,
            extra_events=(
                (
                    "child.promoted",
                    {
                        "command_id": command.command_id,
                        "child_run_id": child_id,
                        "operation_key": snapshot.pending_intent,
                    },
                ),
            )
            if reason == "applied"
            else (),
        ),
    )
    if reason == "applied":
        port.release_foreground(updated, child_id)
    return updated
