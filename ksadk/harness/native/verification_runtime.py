"""Pure verifier and checkpoint verdict transitions for the Native Run."""

from __future__ import annotations

import hashlib
import json

from .call_budget import call_room
from .completion import (
    VerificationItemResult,
    VerificationReport,
    _report_mismatch,
    reduce_completion,
)
from .completion_text import candidate_model_message
from .delivery_review import schedule_delivery_review
from .final_submission import FINAL_SUBMISSION_TOOL
from .ports import RunConfiguration, VerificationBundle
from .state import RunPhase, RunSnapshot, move
from .tool_budget import model_tool_surface
from .transitions import finish
from .verification_binding import verification_subject_context_digest
from .verification_workspace import changed_verification_subject


def _model_feedback_item(item: VerificationItemResult) -> dict[str, object]:
    """Give the model a repair diagnosis, not an audit evidence locator.

    Evidence references remain in the signed report and Run ledger. They can
    resemble tool arguments even when they are not resources the model may
    open, so projecting them into a repair turn can cause unrelated tool calls.
    """

    feedback: dict[str, object] = {
        "acceptance_id": item.acceptance_id,
        "status": item.status,
        "safe_summary": item.safe_summary,
        "repairable": item.repairable,
    }
    if item.repair_target is not None:
        feedback["repair_target"] = item.repair_target.model_dump(mode="json", exclude_none=True)
    return feedback


def _workspace_repair_execution_unavailable(
    snapshot: RunSnapshot, report: VerificationReport, config: RunConfiguration
) -> bool:
    """Stop only known file repairs when current admission has no task tools."""
    required = {item.acceptance_id for item in config.contract.items if item.required}
    outstanding = tuple(
        item for item in report.items if item.acceptance_id in required and item.status != "passed"
    )
    if not outstanding or any(
        item.repair_target is None or item.repair_target.kind != "workspace_file"
        for item in outstanding
    ):
        return False
    return not any(
        config.final_submission_schema is None or tool.name != FINAL_SUBMISSION_TOOL
        for tool in model_tool_surface(snapshot, config)
    )


def reduce_verification(
    snapshot: RunSnapshot, bundle: VerificationBundle, config: RunConfiguration
) -> RunSnapshot:
    candidate = snapshot.candidate or {}
    updated = snapshot.model_copy(update={"pending_intent": None})
    if bundle.candidate.candidate_id != candidate.get(
        "candidate_id"
    ) or bundle.candidate.final_text != candidate.get("final_text"):
        return finish(updated, "unverified", "candidate_mismatch")
    expected_binding = verification_subject_context_digest(snapshot)
    if bundle.candidate.subject_context_digest != expected_binding:
        return finish(updated, "unverified", "subject_context_digest_mismatch")
    post = bundle.post_workspace
    if post is not None:
        expected_operation = f"{snapshot.pending_intent}:post-verification"
        if post.operation_key != expected_operation:
            return finish(updated, "unverified", "verification_workspace_mismatch")
        if changed := changed_verification_subject(
            snapshot,
            post,
            config.limits.max_repairs,
            reason="verification_subject_changed",
        ):
            return changed
    if candidate.get("purpose") == "checkpoint":
        report = bundle.report
        if report is not None and (
            mismatch := _report_mismatch(config.contract, bundle.candidate, report)
        ):
            return finish(updated, "unverified", f"checkpoint_{mismatch}")
        observed = list(report.items) if report else []
        failed = [item for item in observed if item.status == "failed"]
        blocked = [item for item in observed if item.status == "blocked"]
        messages = snapshot.messages
        if observed:
            messages = (
                *messages,
                {
                    "role": "user",
                    "kind": "verification_observation",
                    "content": json.dumps(
                        {
                            "instruction": (
                                "Repair observed failures and complete remaining acceptance "
                                "checks before final delivery. Blocked checks were not "
                                "verified; inspect their reported cause before treating them "
                                "as passed."
                            ),
                            "failed": [_model_feedback_item(item) for item in failed],
                            "blocked": [_model_feedback_item(item) for item in blocked],
                            "not_run": [
                                _model_feedback_item(item)
                                for item in observed
                                if item.status == "not_run"
                            ],
                            "contract_revision": config.contract.revision,
                        },
                        ensure_ascii=False,
                    ),
                },
            )
        return move(updated, RunPhase.PREPARING_CONTEXT).model_copy(
            update={
                "candidate": None,
                "verification": report.model_dump(mode="json") if report else None,
                "verification_tool_calls": snapshot.tool_calls if report else None,
                "checkpoint_workspace_revision": (
                    post.revision if post is not None else snapshot.workspace_revision
                ),
                "checkpoint_web_mutation_tool_call": snapshot.last_web_mutation_tool_call,
                "workspace_dirty_since_checkpoint": False,
                "messages": messages,
            }
        )
    decision = reduce_completion(
        config.contract,
        bundle.candidate,
        bundle.report,
        snapshot.repair_attempts,
        config.limits.max_repairs,
    )
    report = bundle.report
    updated = updated.model_copy(
        update={
            "candidate": bundle.candidate.model_dump(mode="json"),
            "verification": report.model_dump(mode="json") if report else None,
            "verification_tool_calls": (
                snapshot.tool_calls
                if report is not None
                and _report_mismatch(config.contract, bundle.candidate, report) is None
                else None
            ),
        }
    )
    if decision.action == "finalize":
        if review := schedule_delivery_review(updated, config, reason=decision.reason):
            return review
        return finish(updated, decision.outcome, decision.reason)
    if not call_room(config.limits.max_model_calls, updated.model_budget_claimed):
        # The candidate already consumed the last possible model exchange.
        # A text-only task may still repair without tools while model budget
        # remains, so tool availability is not a valid reason to stop here.
        return finish(updated, decision.outcome, "repair_budget_unavailable")
    if _workspace_repair_execution_unavailable(updated, report, config):
        return finish(updated, decision.outcome, "repair_execution_unavailable")
    # Evidence URIs may contain a new candidate ID on every verification. Such
    # a new location is not progress when the subject, environment and verdict
    # are unchanged. Only host-verified content/diagnostic changes count here.
    evidence = {
        "subject": report.subject_digest,
        "environment": report.environment_digest,
        "items": sorted(
            (
                item.acceptance_id,
                item.status,
                item.verifier_id,
                item.verifier_version,
                item.safe_summary,
                item.repair_target.model_dump_json() if item.repair_target is not None else None,
            )
            for item in report.items
        ),
    }
    evidence_digest = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    same = (
        snapshot.failure_fingerprint == report.failure_fingerprint
        and snapshot.failure_evidence_digest == evidence_digest
    )
    if same:
        return finish(updated, decision.outcome, "repeated_verification_failure")
    observation = {
        "role": "user",
        "kind": "verification_observation",
        "content": json.dumps(
            {
                "instruction": (
                    "Repair failed required items and host-marked actionable unobserved "
                    "items, then verify again. Unobserved is not passed."
                ),
                "failed": [
                    _model_feedback_item(item)
                    for item in report.items
                    if item.status == "failed"
                ],
                "not_run": [
                    _model_feedback_item(item)
                    for item in report.items
                    if item.status == "not_run"
                ],
                "contract_revision": config.contract.revision,
                "candidate_status": "rejected",
            },
            ensure_ascii=False,
        ),
    }
    # Preserve the exact rejected draft for repair, including ordinary prose.
    # The following host feedback marks it rejected; it grants no evidence.
    proposal = candidate_model_message(snapshot)
    return move(updated, RunPhase.PREPARING_CONTEXT).model_copy(
        update={
            "repair_attempts": snapshot.repair_attempts + 1,
            # Verifier feedback starts a separate response, not a suffix of
            # the rejected candidate. Keep aggregate recovery budgets intact.
            "continuations": 0,
            "failure_fingerprint": report.failure_fingerprint,
            "failure_evidence_digest": evidence_digest,
            "consecutive_failure_count": 1,
            "messages": (*snapshot.messages, proposal, observation),
            "candidate": None,
        }
    )
