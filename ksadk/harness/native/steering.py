"""Pure, versioned steering of an existing run at a host-controlled safe point.

The host authenticates the command and compiles replacement acceptance contracts;
model text cannot submit either. A host must settle/reconcile a running effect
before applying steering: a superseded tool message means *not dispatched*, not
that an external side effect was rolled back. Store commits and approval events
belong to the caller. This module never resets a run's aggregate effect budgets.
"""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import Field, ValidationError

from .completion import TaskContract
from .context_runtime_contracts import CompactionHistory
from .ports import digest
from .state import Contract, RunCommand, RunPhase, RunSnapshot
from .transitions import finish


class SteeringUpdate(Contract):
    text: Annotated[str, Field(min_length=1, pattern=r"\S")]
    expected_goal_revision: int = Field(strict=True, ge=0)
    task_contract: TaskContract | None = None


def reserved_goal_revision(snapshot: RunSnapshot, commands: tuple[RunCommand, ...]) -> int:
    """Reserve the next CAS revision from ordered, unconsumed steering commands.

    Explicit stale commands remain stale when applied. This projection is only
    for host-generated live corrections submitted before the driver reaches its
    next command boundary; it changes neither the ledger nor the CAS rule.
    """
    revision = snapshot.goal_revision
    for command in commands:
        if command.kind == "steer" and command.payload.get("expected_goal_revision") == revision:
            revision += 1
    return revision


def apply_steering(
    snapshot: RunSnapshot,
    command: RunCommand,
    *,
    bound_tool_names: frozenset[str] | None = None,
) -> tuple[RunSnapshot, str]:
    """Return a proposed state and stable reason; rejected updates retain identity.

    Contract revisions are opaque identities, ordered by ``goal_revision`` CAS.
    Replacing acceptance requires a fresh contract revision. Omitting a contract
    means guidance under the existing acceptance, never implicit downgrading.
    ``revision`` and ``event_seq`` are assigned only by the eventual store commit.
    """
    if command.kind != "steer":
        return snapshot, "command_not_applicable"
    if snapshot.phase.terminal:
        return snapshot, "run_is_terminal"
    if "expected_goal_revision" not in command.payload:
        return (
            finish(snapshot, "blocked", "contract_revision_required"),
            "contract_revision_required",
        )
    try:
        update = SteeringUpdate.model_validate(command.payload)
    except ValidationError:
        return snapshot, "invalid_steering"
    if update.expected_goal_revision != snapshot.goal_revision:
        return snapshot, "stale_goal_revision"

    contract = update.task_contract
    if contract is not None:
        if (
            bound_tool_names is not None
            and contract.allowed_tool_names is not None
            and not set(contract.allowed_tool_names).issubset(bound_tool_names)
        ):
            return snapshot, "task_tool_scope_unbound"
        try:
            previous = TaskContract.model_validate(snapshot.task_contract)
        except ValidationError:
            return snapshot, "contract_revision_required"
        if previous.revision == contract.revision:
            return snapshot, "contract_revision_required"
        if digest(previous) != snapshot.contract_digest:
            return snapshot, "contract_binding_mismatch"

    calls = [tool.get("call_id") for tool in snapshot.pending_tools]
    if any(not isinstance(call, str) or not call for call in calls) or len(set(calls)) != len(
        calls
    ):
        return snapshot, "invalid_pending_tools"
    superseded = tuple(
        {
            "role": "tool",
            "tool_call_id": call,
            "content": json.dumps(
                {
                    "status": "superseded",
                    "content": "Tool call superseded by user steering before execution.",
                }
            ),
        }
        for call in calls
    )
    paused = snapshot.phase == RunPhase.PAUSED
    values = {
        "phase": RunPhase.PAUSED if paused else RunPhase.PREPARING_CONTEXT,
        "paused_from": RunPhase.PREPARING_CONTEXT if paused else None,
        "resume_phase": None,
        "goal": update.text,
        "goal_revision": snapshot.goal_revision + 1,
        # A real user goal breaks the unfinished response chain. Only this
        # chain-local count resets; aggregate Run budgets remain consumed.
        "continuations": 0,
        "messages": (*snapshot.messages, *superseded, {"role": "user", "content": update.text}),
        "candidate": None,
        "stop_hook_state": None,
        "tool_hook_state": None,
        "delivery_review": None,
        "verification": None,
        "verification_tool_calls": None,
        "checkpoint_web_mutation_tool_call": None,
        "unresolved_interaction_observation": None,
        "last_browser_observation": None,
        "task_progress": (),
        # The cursor remains monotonic across goal revisions; only novelty
        # membership is rebased so useful rereads for the new goal can count.
        "observed_progress_fingerprints": (),
        "task_progress_revision": snapshot.task_progress_revision + 1,
        "workspace_revision": None,
        "workspace_changed_paths": (),
        "workspace_observation_status": "unavailable",
        "workspace_observation_reason": "goal_changed",
        "workspace_observation_cursor": None,
        "prepared_context": None,
        "compaction_request": None,
        "force_compaction": False,
        "retry_not_before": 0,
        "pending_intent": None,
        "pending_tools": (),
        "pending_interaction": None,
        "interaction_host_id": None,
        "child_approval": None,
        "interaction": None,
        "failure_fingerprint": None,
        "failure_evidence_digest": None,
        "consecutive_failure_count": 0,
        "outcome": None,
        "final_text": "",
        "reason": None,
    }
    if contract is not None:
        try:
            history = CompactionHistory.model_validate(
                snapshot.compaction_history
            ).reset_projection()
        except ValidationError:
            return snapshot, "compaction_history_invalid"
        values.update(
            task_contract=contract.model_dump(mode="json"),
            contract_digest=digest(contract),
            compaction_history=history,
        )
    # This steering-specific transition deliberately does not loosen move()'s
    # normal state machine. JSON validation also detaches nested transcript data.
    proposed = snapshot.model_copy(update=values)
    return RunSnapshot.model_validate_json(proposed.model_dump_json()), "applied"
