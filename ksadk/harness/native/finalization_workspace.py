"""Re-observe a recovered final candidate through the ordinary workspace effect."""

from __future__ import annotations

from .completion import CompletionCandidate, VerificationReport, _report_mismatch
from .ports import RunConfiguration, WorkspaceObservation
from .state import CommitBatch, EffectIntent, EffectReceipt, RunPhase, RunSnapshot
from .transitions import finish, prepare_effect
from .verification_binding import verification_subject_context_digest
from .verification_workspace import changed_verification_subject


def _cursor(snapshot: RunSnapshot, fence: int) -> str:
    return (f"finalize:{fence}:{snapshot.goal_revision}:{snapshot.model_calls}:"
            f"{snapshot.tool_calls}:{(snapshot.candidate or {}).get('candidate_id', '')}")


def prepare_finalization_workspace(snapshot: RunSnapshot, fence: int) -> CommitBatch | None:
    """A receipt from another owner cannot discharge this owner's fresh check."""
    if (snapshot.phase != RunPhase.FINALIZING or snapshot.verification is None
            or snapshot.reason == "policy_revoked"):
        return None
    cursor = _cursor(snapshot, fence)
    return prepare_effect(snapshot, "workspace", {
        "purpose": "finalization", "cursor": cursor, "fencing_token": fence,
    }).model_copy(update={"require_no_pending_commands": True})


def _unavailable(snapshot: RunSnapshot, reason: str, *, wall_expired: bool = False) -> RunSnapshot:
    # The old receipt remains immutable in the Store. Only its eligibility as
    # the current final answer is withdrawn; no replacement verifier is forged.
    cleared = snapshot.model_copy(update={
        "candidate": None, "verification": None, "verification_tool_calls": None,
        "workspace_observation_cursor": None,
    })
    return finish(cleared, "budget_exhausted" if wall_expired else "unverified", reason)


def _workspace_independent_report(
    snapshot: RunSnapshot, observation: WorkspaceObservation, config: RunConfiguration,
) -> bool:
    # Equal capture failures cannot attest freshness. This narrow exception is
    # for an explicitly independent report when no workspace existed on either
    # side, never for a formerly captured workspace that disappeared.
    if (snapshot.workspace_observation_status != "unavailable"
            or snapshot.workspace_observation_reason != "workspace_missing"
            or snapshot.workspace_revision is not None or snapshot.workspace_changed_paths
            or observation.status != "unavailable" or observation.reason != "workspace_missing"
            or observation.revision is not None or observation.changed_paths
            or snapshot.verification_tool_calls != snapshot.tool_calls):
        return False
    try:
        candidate = CompletionCandidate.model_validate(snapshot.candidate)
        report = VerificationReport.model_validate(snapshot.verification)
    except ValueError:
        return False
    known = {item.acceptance_id for item in config.contract.items}
    return (
        report.workspace_dependency == "none"
        and all(item.acceptance_id in known for item in report.items)
        and _report_mismatch(config.contract, candidate, report) is None
        and candidate.subject_context_digest == verification_subject_context_digest(snapshot)
    )


def reduce_finalization_workspace(
    snapshot: RunSnapshot, intent: EffectIntent, receipt: EffectReceipt,
    config: RunConfiguration,
) -> RunSnapshot:
    """Seal observation, or revoke the stale candidate without spending repair calls."""
    wall_expired = (receipt.result.get("reason") == "wall_time_hard_limit"
                    or receipt.result.get("deadline_cause") == "wall_time_hard_limit")
    if receipt.status != "committed":
        return _unavailable(snapshot, "wall_time_hard_limit" if wall_expired
                            else "finalization_workspace_unavailable", wall_expired=wall_expired)
    fence = intent.payload.get("fencing_token")
    if (snapshot.phase != RunPhase.FINALIZING or type(fence) is not int or fence < 1
            or intent.payload.get("cursor") != _cursor(snapshot, fence)):
        return _unavailable(snapshot, "finalization_workspace_mismatch")
    try:
        observation = WorkspaceObservation.model_validate(receipt.result)
    except ValueError:
        return _unavailable(snapshot, "finalization_workspace_mismatch")
    if observation.operation_key != snapshot.pending_intent:
        return _unavailable(snapshot, "finalization_workspace_mismatch")
    if ((observation.status != "captured" or snapshot.workspace_observation_status != "captured")
            and not _workspace_independent_report(snapshot, observation, config)):
        return _unavailable(snapshot, "finalization_workspace_unavailable")
    if changed := changed_verification_subject(
        snapshot, observation, 0, reason="finalization_workspace_changed",
    ):
        return changed
    # The original cursor is part of the sealed verification subject identity.
    # Completion of this check is tracked by the current lease owner, not by
    # rewriting that identity. A later owner always starts a new check.
    return snapshot.model_copy(update={"pending_intent": None})
