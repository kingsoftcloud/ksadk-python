"""Pure compaction validation; summaries never become execution authority.

The host supplies token counts from its pinned model tokenizer and retains the
original transcript. This module performs no model, storage, or artifact I/O.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from .completion import VerificationRepairTarget
from .execution_budget import ExecutionBudgetObservation

NonBlank = Annotated[str, Field(min_length=1, pattern=r"\S")]
TokenCount = Annotated[int, Field(strict=True, ge=0)]
PositiveTokenCount = Annotated[int, Field(strict=True, ge=1)]


class _ContextContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AcceptanceProgress(_ContextContract):
    """A current-subject pass may still need a new final-candidate verdict."""

    acceptance_id: NonBlank
    required: bool
    status: Literal["pending", "passed", "failed", "blocked"] = "pending"
    last_verdict: Literal["passed", "failed", "not_run", "blocked"] | None = None
    needs_reverification: bool = True
    safe_summary: str = ""
    evidence_refs: tuple[NonBlank, ...] = ()
    verifier_id: NonBlank | None = None
    verifier_version: NonBlank | None = None
    verification_method: Literal["deterministic", "human", "model_assisted"] | None = None
    repairable: bool | None = None
    repair_target: VerificationRepairTarget | None = None


class VerificationProgress(_ContextContract):
    """Identity of the stored report; only a host-validated cursor grants trust."""

    candidate_id: NonBlank
    contract_revision: NonBlank
    subject_digest: NonBlank
    subject_context_digest: NonBlank | None = None
    environment_digest: NonBlank
    binding_validated: bool
    tool_calls_at_verification: int | None = Field(default=None, strict=True, ge=0)
    tool_calls_since_verification: int | None = Field(default=None, strict=True, ge=0)


class PlanningProgress(_ContextContract):
    # Preserve the stable TodoWrite identity across model turns and compaction.
    # It remains model planning, without granting acceptance authority.
    id: str | None = Field(default=None, min_length=1, max_length=180,
                           exclude_if=lambda value: value is None)
    content: NonBlank = Field(max_length=500)
    status: Literal["pending", "in_progress", "completed"]
    priority: Literal["high", "medium", "low"]
    authority: Literal["model_planning_only"] = "model_planning_only"


class SourceReadProgress(_ContextContract):
    """Receipt-derived navigation, never permission, freshness or task coverage."""

    path: NonBlank = Field(max_length=2048)
    receipt_ref: NonBlank
    tool_call_id: NonBlank
    returned_lines: tuple[PositiveTokenCount, PositiveTokenCount]
    # The configured clip is a navigation hint. The planner may retain a larger
    # complete batch when it fits; these fields do not attest model observation.
    configured_prefix_lines: tuple[PositiveTokenCount, PositiveTokenCount] | None
    configured_next_start_line: PositiveTokenCount | None
    total_lines: PositiveTokenCount
    source_mtime_ns: TokenCount | None = None
    source_size_bytes: TokenCount | None = None
    source_payload_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_freshness: Literal["not_attested"] = "not_attested"
    semantic_coverage: Literal["not_attested"] = "not_attested"
    authority: Literal["navigation_only"] = "navigation_only"

    @model_validator(mode="after")
    def coherent_navigation(self):
        start, end = self.returned_lines
        if not 1 <= start <= end <= self.total_lines:
            raise ValueError("invalid returned source range")
        if self.configured_prefix_lines is None:
            if self.configured_next_start_line is not None:
                raise ValueError("partial source line cannot provide a line cursor")
        else:
            first, last = self.configured_prefix_lines
            if first != start or not first <= last <= end:
                raise ValueError("source preview must be a continuous returned prefix")
            expected = last + 1 if last < self.total_lines else None
            if self.configured_next_start_line != expected:
                raise ValueError("source cursor must follow the complete visible prefix")
        return self


class DirectoryListingProgress(_ContextContract):
    """Observed navigation only; names do not prove implementation or coverage."""

    path: NonBlank = Field(max_length=2048)
    receipt_refs: tuple[NonBlank, ...] = Field(min_length=1)
    tool_call_ids: tuple[NonBlank, ...] = Field(min_length=1)
    child_directories: tuple[NonBlank, ...]
    latest_result_truncated: bool
    observation_scope: Literal["historically_returned_children"] = "historically_returned_children"
    authority: Literal["navigation_only"] = "navigation_only"
    source_freshness: Literal["not_attested"] = "not_attested"
    semantic_coverage: Literal["not_attested"] = "not_attested"


class ExecutionContinuityState(_ContextContract):
    """Host-owned facts; pending_work names repair gaps, not final rechecks."""

    execution_budget: ExecutionBudgetObservation | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    goal: NonBlank
    goal_revision: int = Field(strict=True, ge=0)
    contract_digest: NonBlank
    required_acceptance_ids: tuple[NonBlank, ...]
    pending_work: tuple[NonBlank, ...]
    failure_fingerprints: tuple[NonBlank, ...]
    evidence_refs: tuple[NonBlank, ...]
    artifact_refs: tuple[NonBlank, ...]
    artifact_catalog: Literal["earned-artifact-catalog/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    next_action: NonBlank | None
    workspace_revision: NonBlank | None
    observed_progress_revision: int = Field(default=0, strict=True, ge=0)
    workspace_state: Literal["initial", "captured", "unavailable"] = "unavailable"
    workspace_changed_paths: tuple[NonBlank, ...] = ()
    workspace_observation_reason: NonBlank | None = None
    acceptance_progress: tuple[AcceptanceProgress, ...] = ()
    last_verification: VerificationProgress | None = None
    tool_receipt_refs: tuple[NonBlank, ...] = ()
    task_progress: tuple[PlanningProgress, ...] = ()
    task_progress_revision: int = Field(default=0, strict=True, ge=0)
    # Verification debt is distinct from work the host has found defective.
    # A candidate still goes through the completion verifier even when no
    # repair is required; unchecked evidence is never a completion pass.
    pending_verification: tuple[NonBlank, ...] = ()
    # Generic tool content cannot attest to artifacts or a current code revision.
    artifact_state: Literal["unknown", "available"] = "unknown"
    pending_interaction_refs: tuple[NonBlank, ...] = ()
    unresolved_observation_refs: tuple[NonBlank, ...] = ()
    pending_subagent_refs: tuple[NonBlank, ...] = ()
    pending_guidance_ids: tuple[NonBlank, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    # Parent-accounted locators, distinct from pending execution or acceptance.
    accounted_subagent_run_ids: tuple[NonBlank, ...] = ()
    source_read_progress: tuple[SourceReadProgress, ...] = ()
    source_read_history: tuple[SourceReadProgress, ...] = Field(
        default=(), exclude_if=lambda value: not value,
    )
    source_read_pages: tuple[SourceReadProgress, ...] = Field(
        default=(), exclude_if=lambda value: not value,
    )
    source_path_projection: Literal["canonical-source-paths/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    directory_listing_progress: tuple[DirectoryListingProgress, ...] = Field(
        default=(), exclude_if=lambda value: not value,
    )
    # Host-owned delivery debt, separate from plugin content and task goals.
    pending_hook_operation_keys: tuple[NonBlank, ...] = ()
    context_source_operation_key: str | None = Field(default=None, min_length=1,
                                                    exclude_if=lambda value: value is None)

    @model_serializer(mode="wrap")
    def omit_unconfigured_subagents(self, serialize):
        payload = serialize(self)
        if not self.accounted_subagent_run_ids:
            payload.pop("accounted_subagent_run_ids", None)
        if not self.source_read_progress:
            payload.pop("source_read_progress", None)
        if not self.pending_hook_operation_keys:
            payload.pop("pending_hook_operation_keys", None)
        return payload

    pending_tool_call_ids: tuple[NonBlank, ...] = ()

    @model_validator(mode="after")
    def unique_required_and_pending_ids(self) -> ExecutionContinuityState:
        for identities in (
            self.required_acceptance_ids,
            self.pending_hook_operation_keys,
            self.pending_tool_call_ids,
            tuple(item.acceptance_id for item in self.acceptance_progress),
            self.tool_receipt_refs,
            self.accounted_subagent_run_ids,
            tuple(item.path for item in self.source_read_progress),
            tuple(item.path for item in self.source_read_history),
            tuple((item.receipt_ref, item.tool_call_id) for item in self.source_read_pages),
            tuple(item.path for item in self.directory_listing_progress),
            self.pending_verification,
        ):
            if len(set(identities)) != len(identities):
                raise ValueError("duplicate acceptance or pending tool identity")
        if not set(self.pending_verification).issubset(self.required_acceptance_ids):
            raise ValueError("pending verification must name required acceptances")
        if not {item.path for item in self.source_read_history}.issubset(
            item.path for item in self.source_read_progress
        ):
            raise ValueError("source history must have a latest page")
        if not {item.path for item in self.source_read_pages}.issubset(
            item.path for item in self.source_read_progress
        ):
            raise ValueError("source pages must have a latest page")
        return self


class ContextMessage(_ContextContract):
    """Transcript reference, full raw payload digest, and trusted model-view cost.

    Request assembly must resolve ``message_id`` against the original transcript;
    this metadata cannot substitute for tool arguments or multimodal payloads.
    """

    message_id: NonBlank
    payload_digest: NonBlank
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    token_count: PositiveTokenCount
    tool_call_ids: tuple[NonBlank, ...] = ()
    tool_result_for: NonBlank | None = None

    @model_validator(mode="after")
    def valid_tool_metadata(self) -> ContextMessage:
        if self.tool_call_ids and self.role != "assistant":
            raise ValueError("only assistant messages may declare tool calls")
        if (self.role == "tool") != (self.tool_result_for is not None):
            raise ValueError("tool results require the tool role and a call identity")
        if len(set(self.tool_call_ids)) != len(self.tool_call_ids):
            raise ValueError("duplicate tool call identity")
        return self


class ContextBudget(_ContextContract):
    window_tokens: PositiveTokenCount
    max_output_tokens: PositiveTokenCount
    fixed_tokens: TokenCount = 0

    @model_validator(mode="after")
    def reserve_fits_window(self) -> ContextBudget:
        if self.max_output_tokens + self.fixed_tokens > self.window_tokens:
            raise ValueError("output and fixed reserves exceed the context window")
        return self

    @property
    def input_capacity(self) -> int:
        return self.window_tokens - self.max_output_tokens - self.fixed_tokens


class CompactionEnvelope(_ContextContract):
    source_revision: int = Field(strict=True, ge=0)
    continuity: ExecutionContinuityState
    continuity_digest: NonBlank
    summary_text: NonBlank
    summary_token_count: PositiveTokenCount
    preserved_message_refs: tuple[NonBlank, ...]
    validator_version: Literal["ksadk.compaction/v1"] = "ksadk.compaction/v1"


class CompactionProjection(_ContextContract):
    """Low-trust request context, never an assistant final or a Run outcome."""

    kind: Literal["compaction_summary"] = "compaction_summary"
    trust: Literal["untrusted"] = "untrusted"
    purpose: Literal["context_only"] = "context_only"
    source_revision: int
    continuity_digest: NonBlank
    summary_text: NonBlank
    messages: tuple[ContextMessage, ...]
    total_reserved_tokens: PositiveTokenCount


class CompactionRejected(ValueError):
    """Host must retain the previous context and repair or block compaction."""


def continuity_digest(state: ExecutionContinuityState) -> str:
    encoded = json.dumps(
        state.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _tool_groups(
    messages: tuple[ContextMessage, ...],
) -> tuple[tuple[frozenset[str], ...], frozenset[str], frozenset[str]]:
    """Return complete groups, unfinished group refs, and unresolved call IDs."""
    message_ids: set[str] = set()
    call_ids: set[str] = set()
    pending: set[str] = set()
    current_group: set[str] = set()
    complete_groups: list[frozenset[str]] = []
    for message in messages:
        if message.message_id in message_ids:
            raise CompactionRejected("duplicate_message_identity")
        message_ids.add(message.message_id)
        if message.role == "tool":
            if message.tool_result_for not in pending:
                raise CompactionRejected("orphan_or_duplicate_tool_result")
            pending.remove(message.tool_result_for)
            current_group.add(message.message_id)
            if not pending:
                complete_groups.append(frozenset(current_group))
                current_group = set()
        elif pending:
            raise CompactionRejected("interleaved_pending_tool_protocol")
        elif message.tool_call_ids:
            if call_ids.intersection(message.tool_call_ids):
                raise CompactionRejected("duplicate_tool_call_identity")
            call_ids.update(message.tool_call_ids)
            pending.update(message.tool_call_ids)
            current_group.add(message.message_id)
    return tuple(complete_groups), frozenset(current_group), frozenset(pending)


def _preserve_messages(
    old: tuple[ContextMessage, ...],
    new: tuple[ContextMessage, ...],
    continuity: ExecutionContinuityState,
) -> None:
    groups, pending_refs, pending_ids = _tool_groups(old)
    if pending_ids != frozenset(continuity.pending_tool_call_ids):
        raise CompactionRejected("pending_tool_state_mismatch")
    old_index = {message.message_id: index for index, message in enumerate(old)}
    retained = frozenset(message.message_id for message in new)
    if not pending_refs.issubset(retained):
        raise CompactionRejected("pending_tool_protocol_lost")
    for group in groups:
        if group.intersection(retained) and not group.issubset(retained):
            raise CompactionRejected("partial_tool_group")
    previous_index = -1
    for message in new:
        index = old_index.get(message.message_id)
        if index is None:
            raise CompactionRejected("unknown_message")
        if index <= previous_index:
            raise CompactionRejected("message_order_or_identity_changed")
        if message != old[index]:
            raise CompactionRejected("retained_message_changed")
        previous_index = index
    _, new_pending_refs, new_pending_ids = _tool_groups(new)
    if new_pending_refs != pending_refs or new_pending_ids != pending_ids:
        raise CompactionRejected("pending_tool_protocol_changed")


def validate_compaction(
    *,
    source_revision: int,
    continuity: ExecutionContinuityState,
    old_messages: tuple[ContextMessage, ...],
    new_messages: tuple[ContextMessage, ...],
    envelope: CompactionEnvelope,
    budget: ContextBudget,
) -> CompactionProjection:
    """Validate a proposal before a CAS commit against ``source_revision``.

    Rejection must not overwrite the committed continuity state or transcript.
    ``fixed_tokens`` includes system/tool schemas, structured continuity, and
    message framing; message/summary counts must use the same model tokenizer.
    Artifact resolution, scope checks, and summary consistency belong to ports.
    """
    if type(source_revision) is not int or source_revision < 0:
        raise CompactionRejected("invalid_source_revision")
    if envelope.source_revision != source_revision:
        raise CompactionRejected("source_revision_mismatch")
    if envelope.continuity != continuity:
        raise CompactionRejected("continuity_mismatch")
    digest = continuity_digest(continuity)
    if envelope.continuity_digest != digest:
        raise CompactionRejected("continuity_digest_mismatch")
    if envelope.preserved_message_refs != tuple(item.message_id for item in new_messages):
        raise CompactionRejected("message_manifest_mismatch")
    _preserve_messages(old_messages, new_messages, continuity)
    total = (
        sum(message.token_count for message in new_messages)
        + envelope.summary_token_count
        + budget.fixed_tokens
        + budget.max_output_tokens
    )
    if total > budget.window_tokens:
        raise CompactionRejected("context_window_exceeded")
    return CompactionProjection(
        source_revision=source_revision,
        continuity_digest=digest,
        summary_text=envelope.summary_text,
        messages=new_messages,
        total_reserved_tokens=total,
    )
