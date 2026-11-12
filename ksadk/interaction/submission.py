"""Private reservation decisions for native, deferred-confirmation providers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ksadk.interaction.contracts import (
    InteractionReceipt,
    InteractionRecord,
    InteractionSubmission,
    is_terminal,
)
from ksadk.interaction.form_values import normalize_form_answer
from ksadk.interaction.ledger import submission_digest
from ksadk.interaction.schema import InputValidationError
from ksadk.kernel.errors import InvalidCommandError

SUBMISSION_KEY = "native_submission"


def normalized_submission(
    record: InteractionRecord, submission: InteractionSubmission
) -> InteractionSubmission:
    """Business delivery uses typed answers; request digests keep the raw JSON.

    Keep reservations/retries tied to the original signed request, including the
    difference between string and numeric JSON, while delivering and displaying
    the same Schema-validated value.
    """
    if record.kind != "structured_input" or submission.action != "submit":
        return submission
    try:
        response = normalize_form_answer(record.request_schema, submission.response)
    except InputValidationError as exc:
        raise InvalidCommandError(
            "Response does not satisfy the input schema",
            details={"reason": exc.code, "issues": exc.issues},
        ) from exc
    return submission.model_copy(update={"response": response})


def needs_recovery_check(record: InteractionRecord, fence: int) -> bool:
    attempt = (record.continuation_metadata or {}).get(SUBMISSION_KEY, {})
    previous = attempt.get("fence") if isinstance(attempt, dict) else None
    return (record.status == "resolving" and isinstance(previous, int)
            and previous < fence and attempt.get("state") == "reserved")


def _unstarted_reservation(record, run, command_id, fence):
    """Proof from the fenced Run: no stream starts before durable RUNNING.

    WAITING with the original checkpoint/waits is a narrower fact than merely
    finding an old interrupt in a historical checkpoint. Unknown consumption
    and later waits must never reopen the reservation.
    """
    if not needs_recovery_check(record, fence) or not isinstance(run, dict):
        return False
    attempt = record.continuation_metadata[SUBMISSION_KEY]
    metadata = run.get("metadata") or {}
    native = (metadata.get("handle") or {}).get("native_ref") or {}
    target = record.native_target or {}
    return (
        attempt.get("command_id") == command_id
        and run.get("run_id") == record.run_id
        and run.get("agent_instance_id") == record.agent_instance_id
        and run.get("session_id") == record.session_id
        and run.get("state") == "waiting"
        and run.get("activation_fence") == attempt.get("fence")
        and metadata.get("tenant_id") == record.tenant_id
        and not metadata.get("native_reconciliation_required")
        and record.provider_id == "langgraph" and bool(target.get("task_id"))
        and native.get("checkpoint_id") == target.get("checkpoint_id")
        and (native.get("native_waits") or {}).get(record.interaction_id) == target
    )


@dataclass(frozen=True)
class SubmissionReservation:
    receipt: InteractionReceipt
    acquired: bool


def check_reserved_submission(record: InteractionRecord, submission: InteractionSubmission) -> None:
    metadata = record.continuation_metadata or {}
    attempt = metadata.get(SUBMISSION_KEY)
    if not isinstance(attempt, dict):
        return
    if (
        record.status != "resolving"
        or attempt.get("digest") != submission_digest(submission)
        or attempt.get("state") != "reserved"
    ):
        raise InvalidCommandError(
            "Native confirmation does not match the reserved submission",
            details={"reason": "interaction_submission_mismatch"},
        )


def plan_reservation(
    record: InteractionRecord,
    submission: InteractionSubmission,
    *,
    existing: tuple[str, InteractionReceipt] | None,
    command_id: str,
    fence: int,
    recovery_run: dict | None = None,
) -> tuple[InteractionRecord, SubmissionReservation]:
    digest = submission_digest(submission)
    if existing is not None:
        previous_digest, receipt = existing
        if previous_digest != digest:
            raise InvalidCommandError(
                "Idempotency key was used with another response",
                details={"reason": "idempotency_conflict"},
            )
        if receipt.status == "resolving" and _unstarted_reservation(
            record, recovery_run, command_id, fence
        ):
            metadata = dict(record.continuation_metadata)
            metadata[SUBMISSION_KEY] = {**metadata[SUBMISSION_KEY], "fence": fence}
            return record.model_copy(update={"continuation_metadata": metadata}), SubmissionReservation(receipt, True)
        if not is_terminal(record.status) or receipt.status == record.status:
            return record, SubmissionReservation(receipt, False)
    if is_terminal(record.status):
        raise InvalidCommandError(
            "Interaction is already terminal", details={"reason": "interaction_already_resolved"}
        )
    if record.revision != submission.expected_revision:
        raise InvalidCommandError(
            "Interaction revision does not match",
            details={"reason": "interaction_revision_mismatch"},
        )
    if record.expires_at and datetime.fromisoformat(record.expires_at) <= datetime.now(
        timezone.utc
    ):
        raise InvalidCommandError(
            "Interaction has expired", details={"reason": "interaction_expired"}
        )
    if record.status == "resolving":
        raise InvalidCommandError(
            "A native submission is already in progress",
            details={"reason": "interaction_submission_in_progress"},
        )
    if record.kind == "structured_input":
        if submission.action != "submit":
            raise InvalidCommandError("Structured input requires a submit action")
        normalized_submission(record, submission)
    metadata = dict(record.continuation_metadata or {})
    metadata[SUBMISSION_KEY] = {
        "digest": digest,
        "state": "reserved",
        "command_id": command_id,
        "fence": fence,
        "submission": submission.model_dump(mode="json"),
    }
    updated = record.model_copy(update={"status": "resolving", "continuation_metadata": metadata})
    receipt = InteractionReceipt(
        interaction_id=record.interaction_id, revision=record.revision, status="resolving"
    )
    return updated, SubmissionReservation(receipt, True)


def plan_release(
    record: InteractionRecord, submission: InteractionSubmission
) -> tuple[InteractionRecord, InteractionReceipt]:
    reserved_submission(record)
    check_reserved_submission(record, submission)
    metadata = dict(record.continuation_metadata or {})
    metadata[SUBMISSION_KEY] = {**metadata[SUBMISSION_KEY], "state": "rejected"}
    return (
        record.model_copy(update={"status": "pending", "continuation_metadata": metadata}),
        InteractionReceipt(
            interaction_id=record.interaction_id, revision=record.revision, status="pending"
        ),
    )


def reserved_submission(record: InteractionRecord) -> tuple[InteractionSubmission, dict[str, Any]]:
    attempt = (record.continuation_metadata or {}).get(SUBMISSION_KEY)
    if not isinstance(attempt, dict) or attempt.get("state") != "reserved":
        raise InvalidCommandError("No native submission is reserved")
    return InteractionSubmission.model_validate(attempt["submission"]), attempt
