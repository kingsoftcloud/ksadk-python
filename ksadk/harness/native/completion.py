"""Pure completion reduction from a pinned contract and trusted evidence metadata.

The host must execute verifiers and resolve evidence before supplying a report.
Model text, including a final answer, has no authority to produce ``verified``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from .output_contract import validate_submission_schema

NonBlank = Annotated[str, Field(min_length=1, pattern=r"\S")]
VerificationMethod = Literal["deterministic", "human", "model_assisted"]
VerificationStatus = Literal["passed", "failed", "not_run", "blocked"]
CompletionOutcome = Literal["verified", "unverified", "partial", "blocked"]


class _ContractModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class VerificationRepairTarget(_ContractModel):
    """Host diagnosis of the delivery to repair, never a tool permission grant."""

    kind: Literal["workspace_file", "final_output"]
    path: Annotated[str, Field(min_length=1, max_length=2048)] | None = None

    @model_validator(mode="after")
    def relative_workspace_path(self) -> VerificationRepairTarget:
        if self.kind == "final_output":
            if self.path is not None:
                raise ValueError("final output target has no workspace path")
        elif (
            self.path is None
            or not self.path.strip()
            or any(c in self.path for c in ("\\", ":"))
            or any(ord(c) < 32 for c in self.path)
            or any(part in {"", ".", ".."} for part in self.path.split("/"))
        ):
            raise ValueError("repair target requires a relative workspace path")
        return self


class AcceptanceItem(_ContractModel):
    acceptance_id: NonBlank
    required: bool = True
    verifier_ref: NonBlank
    verifier_version: NonBlank
    verification_method: VerificationMethod
    description: NonBlank


class HostResolvedAnswer(_ContractModel):
    """A host-owned candidate for one goal; never a verification verdict.

    The host may resolve a deterministic information query from its locked
    state. The Kernel still commits and verifies the candidate, and never
    fabricates a model request or a usage receipt for it.
    """

    goal_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    goal_revision: Annotated[int, Field(strict=True, ge=0)]
    text: Annotated[str, Field(min_length=1, max_length=8192, pattern=r"\S")]
    authority: NonBlank
    evidence_ref: NonBlank


class TaskContract(_ContractModel):
    revision: NonBlank
    items: tuple[AcceptanceItem, ...]
    # Trusted host restriction for this goal. None inherits the Build's full
    # bound surface; an empty tuple permits no executable task tools.
    allowed_tool_names: tuple[NonBlank, ...] | None = None
    # Per-goal delivery protocol, pinned with acceptance and tool authority.
    # None keeps the activation's default delivery; no previous goal is inherited.
    final_submission_schema: dict[str, JsonValue] | None = None
    final_submission_payload_key: NonBlank | None = None
    host_resolved_answer: HostResolvedAnswer | None = None

    @model_validator(mode="after")
    def unique_acceptance_ids(self) -> TaskContract:
        _check_unique_ids(self.items)
        if self.host_resolved_answer is not None and (
            self.allowed_tool_names != () or self.final_submission_schema is not None
        ):
            raise ValueError("host resolved answer requires no task tools or structured submission")
        if self.allowed_tool_names is not None and len(set(self.allowed_tool_names)) != len(
            self.allowed_tool_names
        ):
            raise ValueError("duplicate allowed_tool_names")
        if self.final_submission_schema is not None:
            validate_submission_schema(
                self.final_submission_schema, self.final_submission_payload_key
            )
        elif self.final_submission_payload_key is not None:
            raise ValueError("final submission payload requires a schema")
        return self


class CompletionCandidate(_ContractModel):
    candidate_id: NonBlank
    final_text: str
    subject_digest: NonBlank
    # Older native snapshots remain readable, but cannot qualify as current evidence.
    subject_context_digest: NonBlank | None = None
    environment_digest: NonBlank


class VerificationItemResult(_ContractModel):
    acceptance_id: NonBlank
    status: VerificationStatus
    verifier_id: NonBlank
    verifier_version: NonBlank
    evidence_refs: tuple[NonBlank, ...]
    verification_method: VerificationMethod
    safe_summary: str = ""
    # Host-signed item-level repair authority. None preserves older reports'
    # report-wide decision; True can keep one actionable item in a mixed report.
    repairable: bool | None = None
    repair_target: VerificationRepairTarget | None = None


class VerificationReport(_ContractModel):
    candidate_id: NonBlank
    contract_revision: NonBlank
    subject_digest: NonBlank
    subject_context_digest: NonBlank | None = None
    environment_digest: NonBlank
    items: tuple[VerificationItemResult, ...]
    repairable: bool
    failure_fingerprint: str
    # Only the trusted verifier may declare that its evidence has no workspace
    # dependency. Missing metadata remains conservative, including old receipts.
    workspace_dependency: Literal["required", "none"] = Field(
        default="required", exclude_if=lambda value: value == "required"
    )

    @model_validator(mode="after")
    def unique_acceptance_ids(self) -> VerificationReport:
        _check_unique_ids(self.items)
        return self


class CompletionDecision(_ContractModel):
    action: Literal["finalize", "repair"]
    outcome: CompletionOutcome
    reason: NonBlank


def _check_unique_ids(
    items: tuple[AcceptanceItem, ...] | tuple[VerificationItemResult, ...],
) -> None:
    identities = [item.acceptance_id for item in items]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate acceptance_id")


def _finalize(outcome: CompletionOutcome, reason: str) -> CompletionDecision:
    return CompletionDecision(action="finalize", outcome=outcome, reason=reason)


def _report_mismatch(
    contract: TaskContract,
    candidate: CompletionCandidate,
    report: VerificationReport,
) -> str | None:
    expected_items = {item.acceptance_id: item for item in contract.items}
    if any(item.acceptance_id not in expected_items for item in report.items):
        raise ValueError("unknown report acceptance_id")
    if candidate.subject_context_digest is None or report.subject_context_digest is None:
        return "subject_context_digest_missing"
    checks = (
        (report.candidate_id, candidate.candidate_id, "candidate_mismatch"),
        (report.contract_revision, contract.revision, "contract_revision_mismatch"),
        (report.subject_digest, candidate.subject_digest, "subject_digest_mismatch"),
        (
            report.subject_context_digest,
            candidate.subject_context_digest,
            "subject_context_digest_mismatch",
        ),
        (
            report.environment_digest,
            candidate.environment_digest,
            "environment_digest_mismatch",
        ),
    )
    for actual, expected, reason in checks:
        if actual != expected:
            return reason
    for item in report.items:
        expected = expected_items[item.acceptance_id]
        if item.verifier_id != expected.verifier_ref:
            return "verifier_identity_mismatch"
        if item.verifier_version != expected.verifier_version:
            return "verifier_version_mismatch"
        if item.verification_method != expected.verification_method:
            return "verification_method_mismatch"
    return None


def reduce_completion(
    contract: TaskContract,
    candidate: CompletionCandidate,
    report: VerificationReport | None,
    repair_attempts: int = 0,
    max_repairs: int = 3,
) -> CompletionDecision:
    """Decide completion without I/O or changing the acceptance contract.

    ``repair_attempts`` counts repairs already performed. Fingerprint history,
    policy/budget revocation, evidence resolution, and trusted report provenance
    belong to the host; this reducer enforces metadata pins and a repair cap.
    """
    for value in (repair_attempts, max_repairs):
        if type(value) is not int or value < 0:
            raise ValueError("repair budgets must be a non-negative integer")
    if report is None:
        return _finalize("unverified", "verification_report_missing")
    if mismatch := _report_mismatch(contract, candidate, report):
        return _finalize("unverified", mismatch)
    required = tuple(item for item in contract.items if item.required)
    if not required:
        return _finalize("unverified", "required_acceptance_empty")

    results = {item.acceptance_id: item for item in report.items}
    observations = tuple(results.get(item.acceptance_id) for item in required)
    qualified = tuple(
        item
        for item in observations
        if item is not None
        and item.status == "passed"
        and item.evidence_refs
        and item.verification_method != "model_assisted"
    )
    if len(qualified) == len(required):
        return _finalize("verified", "required_acceptance_verified")
    if any(item is not None and item.status == "blocked" for item in observations):
        return _finalize("blocked", "required_acceptance_blocked")

    outcome: CompletionOutcome = "partial" if qualified else "unverified"
    # An absent required result is an incomplete host report, not an Agent
    # failure. An explicit not_run can be repaired only when the trusted host
    # supplied an actionable explanation, a stable failure identity and an
    # affirmative repairability decision; unknown verifier state cannot retry.
    if any(item is None for item in observations):
        return _finalize(outcome, "required_verification_incomplete")
    has_not_run = any(item.status == "not_run" for item in observations)
    has_failed = any(item.status == "failed" for item in observations)
    item_repairable = any(
        item is not None
        and item.status in {"failed", "not_run"}
        and item.repairable is True
        and item.safe_summary.strip()
        and item.evidence_refs
        for item in observations
    )
    actionable_not_run = (
        has_not_run
        and not has_failed
        and bool(report.failure_fingerprint.strip())
        and (
            item_repairable
            or (
                report.repairable
                and all(
                    item.safe_summary.strip() and item.evidence_refs
                    for item in observations
                    if item is not None and item.status == "not_run"
                )
            )
        )
    )
    if has_not_run and not has_failed and not actionable_not_run:
        return _finalize(outcome, "required_verification_incomplete")
    if any(
        item is not None
        and item.status == "passed"
        and (not item.evidence_refs or item.verification_method == "model_assisted")
        for item in observations
    ):
        return _finalize(outcome, "qualified_evidence_missing")
    if not (report.repairable or item_repairable):
        return _finalize(
            outcome,
            "required_verification_incomplete" if has_not_run else "required_acceptance_failed",
        )
    if repair_attempts >= max_repairs:
        return _finalize(outcome, "repair_budget_exhausted")
    if not report.failure_fingerprint.strip():
        return _finalize(outcome, "failure_fingerprint_missing")
    return CompletionDecision(
        action="repair",
        outcome=outcome,
        reason=(
            "required_verification_incomplete"
            if actionable_not_run
            else "required_acceptance_failed"
        ),
    )
