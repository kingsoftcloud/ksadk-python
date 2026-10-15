"""Pure reduction of workspace changes observed around a verifier call."""

from __future__ import annotations

import json

from .ports import WorkspaceObservation
from .state import RunPhase, RunSnapshot, move
from .transitions import finish


def changed_verification_subject(
    snapshot: RunSnapshot,
    current: WorkspaceObservation,
    max_repairs: int,
    *,
    reason: str,
) -> RunSnapshot | None:
    before = (
        snapshot.workspace_observation_status,
        snapshot.workspace_revision,
        snapshot.workspace_changed_paths,
        snapshot.workspace_observation_reason,
    )
    after = (current.status, current.revision, current.changed_paths, current.reason)
    if before == after:
        return None
    changed = snapshot.model_copy(
        update={
            "pending_intent": None,
            "workspace_observation_status": current.status,
            "workspace_revision": current.revision,
            "workspace_changed_paths": current.changed_paths,
            "workspace_observation_reason": current.reason,
            "workspace_observation_cursor": None,
            "candidate": None,
            "verification": None,
            "verification_tool_calls": None,
        }
    )
    if snapshot.phase == RunPhase.FINALIZING:
        # A cold final candidate has already used its verification attempt.
        # Withdraw eligibility without resurrecting a completed repair loop.
        return finish(changed, "unverified", reason)
    if snapshot.repair_attempts >= max_repairs:
        return finish(changed, "unverified", "verification_subject_change_exhausted")
    observation = {
        "role": "user",
        "kind": "verification_observation",
        "content": json.dumps(
            {
                "instruction": "The workspace changed after the last model observation. "
                "Inspect the current state and produce a new completion candidate.",
                "reason": reason,
            },
            ensure_ascii=False,
        ),
    }
    return move(changed, RunPhase.PREPARING_CONTEXT).model_copy(
        update={
            "repair_attempts": snapshot.repair_attempts + 1,
            "messages": (*snapshot.messages, observation),
            # Changed execution facts invalidate the completed candidate and
            # start a fresh answer chain; aggregate Run budgets are unchanged.
            "continuations": 0,
        }
    )
