"""Qualify the final report against the current candidate and execution facts."""

from .completion import CompletionCandidate, VerificationReport, _report_mismatch, reduce_completion
from .ports import RunConfiguration
from .state import RunSnapshot
from .verification_binding import verification_subject_context_digest


def prepare_terminal_verification(snapshot: RunSnapshot, config: RunConfiguration) -> RunSnapshot:
    """Retire stale report pointers, preserving the original effect receipts.

    A checkpoint or rejected final report can remain useful during the loop.
    It cannot be published as the verdict for a different final candidate.
    This happens in the authoritative terminal commit, never in the UI.
    """
    report = None
    try:
        candidate = CompletionCandidate.model_validate(snapshot.candidate)
        observed = VerificationReport.model_validate(snapshot.verification)
        if (
            (snapshot.candidate or {}).get("purpose") != "checkpoint"
            and candidate.final_text == snapshot.final_text
            and snapshot.verification_tool_calls == snapshot.tool_calls
            and candidate.subject_context_digest == verification_subject_context_digest(snapshot)
            and _report_mismatch(config.contract, candidate, observed) is None
        ):
            report = observed
    except (ValueError, TypeError):
        # Old or rejected metadata is still available in its original receipt;
        # absence of a qualified final report is not evidence of a passed check.
        pass
    updates = {
        "verification": report.model_dump(mode="json") if report is not None else None,
        "verification_tool_calls": snapshot.verification_tool_calls if report is not None else None,
    }
    if snapshot.outcome == "verified" and (
        report is None
        or reduce_completion(
            config.contract, candidate, report, max_repairs=0,
        ).outcome != "verified"
    ):
        updates.update(outcome="unverified", reason="terminal_verification_unavailable")
    return snapshot.model_copy(update=updates)
