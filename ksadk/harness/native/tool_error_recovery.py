"""Action-local diagnosis, inspired by ZCode's repeated-call reminder/settlement.

Unlike a generic retry, this host policy never dispatches the same failing
action again without new host evidence. It grants one trial, preserves
authorization and unknown-effect stops, and spends the original model budget.
"""

from __future__ import annotations

import hashlib
import json

from .call_budget import cap_calls, remaining_calls
from .ports import RunConfiguration, ToolDefinition, ToolResult
from .state import CommitBatch, RunPhase, RunSnapshot, ToolErrorRecovery, move
from .tool_error_streak import tool_error_streak_exhausted
from .transitions import commit_transition, finish


def recovery_boundary(snapshot: RunSnapshot, config: RunConfiguration) -> CommitBatch | None:
    recovery = snapshot.tool_error_recovery
    allowance = config.tool_error_recovery_model_calls
    if allowance is None and config.tool_error_recovery_policy is None:
        return None
    if recovery is not None:
        if snapshot.goal_revision != recovery.goal_revision:
            return commit_transition(
                snapshot,
                snapshot.model_copy(
                    update={
                        "tool_error_recovery": None,
                        "repeated_tool_error_count": 0,
                    }
                ),
                "tool.error_recovery_resolved",
                {"reason": "user_goal_changed"},
            )
        if allowance is not None and (
            recovery.prior_model_calls_spent + snapshot.model_calls
            - recovery.started_model_call >= allowance
        ):
            reason = "repeated_tool_error_recovery_exhausted"
            return commit_transition(
                snapshot,
                finish(snapshot, "blocked", reason),
                "tool.error_recovery_exhausted",
                {"reason": reason},
            )
        return None
    if not tool_error_streak_exhausted(snapshot) or snapshot.repeated_tool_error_action is None:
        return None
    recovery = ToolErrorRecovery(
        action=snapshot.repeated_tool_error_action,
        failure_fingerprint=snapshot.repeated_tool_error_signature,
        started_model_call=snapshot.model_calls,
        observation_revision=snapshot.observed_progress_revision,
        goal_revision=snapshot.goal_revision,
        workspace_revision=snapshot.workspace_revision,
    )
    observation = {
        "role": "user",
        "kind": "tool_error_recovery_observation",
        "content": json.dumps(
            {
                "failed_tool": recovery.action.name,
                "repeated_errors": snapshot.repeated_tool_error_count,
                "remaining_model_calls": cap_calls(
                    allowance,
                    remaining_calls(config.limits.max_model_calls, snapshot.model_budget_claimed),
                ),
                "diagnosis_is_not_completion": True,
                "instructions": (
                    "Stop repeating the failed action. Inspect actual state or use another "
                    "authorized method. A new successful host-declared observation permits "
                    "one corrected trial on a later model decision; it does not prove repair "
                    "or completion. The task remains within its original budget."
                ),
            },
            ensure_ascii=False,
        ),
    }
    updated = move(snapshot, RunPhase.PREPARING_CONTEXT).model_copy(
        update={
            "tool_error_recovery": recovery,
            "messages": (*snapshot.messages, observation),
        }
    )
    return commit_transition(
        snapshot,
        updated,
        "tool.error_recovery_started",
        {
            "failed_tool": recovery.action.name,
            "model_call_allowance": allowance,
        },
    )


def guarded_action(snapshot: RunSnapshot, call: dict) -> bool:
    recovery = snapshot.tool_error_recovery
    if recovery is None or call["name"] != recovery.action.name:
        return False
    if recovery.action.arguments_digest is None:
        return True
    digest = hashlib.sha256(
        json.dumps(
            call["arguments"],
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return digest == recovery.action.arguments_digest


def refuse_undiagnosed_action(snapshot: RunSnapshot) -> CommitBatch | None:
    if not snapshot.pending_tools or not guarded_action(snapshot, snapshot.pending_tools[0]):
        return None
    recovery = snapshot.tool_error_recovery
    assert recovery is not None
    call = snapshot.pending_tools[0]
    if (recovery.diagnostic_observation_seen and not recovery.trial_consumed
            and recovery.trial_call_id is None
            and (recovery.diagnostic_model_call is None
                 or snapshot.model_calls > recovery.diagnostic_model_call)) or (
        recovery.trial_call_id == call["call_id"]
        and recovery.trial_model_call == snapshot.model_calls
    ):
        return None
    message = {
        "role": "tool",
        "tool_call_id": call["call_id"],
        "content": json.dumps(
            {
                "status": "error",
                "execution_status": "not_dispatched",
                "content": {"reason": "tool_error_diagnosis_required", "executed": False},
            }
        ),
    }
    updated = snapshot.model_copy(
        update={
            "pending_tools": snapshot.pending_tools[1:],
            "messages": (*snapshot.messages, message),
        }
    )
    return commit_transition(
        snapshot,
        updated,
        "tool.error_recovery_refused",
        {
            "call_id": call["call_id"],
            "reason": "tool_error_diagnosis_required",
            "execution_status": "not_dispatched",
        },
    )


def reserve_diagnostic_trial(snapshot: RunSnapshot, batch: CommitBatch) -> CommitBatch:
    recovery = snapshot.tool_error_recovery
    if recovery is None or not guarded_action(snapshot, snapshot.pending_tools[0]):
        return batch
    # The admission and trial reservation are one commit. Reattaching an
    # undispatched effect or resuming its approval cannot create a second trial.
    updated = batch.snapshot.model_copy(
        update={
            "tool_error_recovery": recovery.model_copy(
                update={
                    "trial_call_id": snapshot.pending_tools[0]["call_id"],
                    "trial_model_call": snapshot.model_calls,
                    "trial_consumed": True,
                }
            ),
        }
    )
    return batch.model_copy(update={"snapshot": updated})


def advance_diagnostic_recovery(
    snapshot: RunSnapshot,
    result: ToolResult,
    definition: ToolDefinition | None,
    observed_progress: dict,
) -> dict:
    recovery = snapshot.tool_error_recovery
    if recovery is None or result.status != "ok":
        return {}
    if (
        guarded_action(snapshot, snapshot.pending_tools[0])
        and recovery.trial_call_id == snapshot.pending_tools[0]["call_id"]
        and recovery.trial_model_call == snapshot.model_calls
    ):
        # An actual successful trial is receipt evidence, not task acceptance.
        return {"tool_error_recovery": None, "repeated_tool_error_count": 0}
    if (not recovery.diagnostic_observation_seen
        and definition is not None
        and definition.name == snapshot.pending_tools[0]["name"]
        and (result.workspace_changed_paths or (
            (definition.read_only or definition.observation_role == "browser")
            and observed_progress.get("observed_progress_revision", 0)
            > recovery.observation_revision
        ))
    ):
        return {
            "tool_error_recovery": recovery.model_copy(
                update={
                    "diagnostic_observation_seen": True,
                    "diagnostic_model_call": snapshot.model_calls,
                }
            )
        }
    return {}


def recovery_continuity(snapshot: RunSnapshot, config: RunConfiguration) -> str | None:
    """Static guard facts survive compaction without invalidating a pending seal.

    Remaining-call numbers change during summary retries. Keep the fixed start
    and allowance here; ContextDriver enforces the live fence before inference.
    This record does not grant a tool, replace the goal or establish acceptance.
    """
    recovery = snapshot.tool_error_recovery
    if recovery is None:
        return None
    return json.dumps({
        "tool_error_recovery": {
            "failed_tool": recovery.action.name,
            "failure_fingerprint": recovery.failure_fingerprint,
            "started_model_call": recovery.started_model_call,
            "model_call_allowance": config.tool_error_recovery_model_calls,
            "diagnostic_observation_seen": recovery.diagnostic_observation_seen,
            "diagnostic_model_call": recovery.diagnostic_model_call,
            "trial_consumed": recovery.trial_consumed,
        },
        "instruction": (
            "The repeated-error guard is still active. Use only currently authorized tools. "
            "Do not repeat the failed action before observing new diagnostic evidence in a "
            "later model decision; one corrected trial is allowed. Unrelated reads or writes "
            "do not reset this window. Diagnosis and tool success are not task acceptance."
        ),
    }, ensure_ascii=False, sort_keys=True)
