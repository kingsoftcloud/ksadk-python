"""Native control state. No I/O, provider objects, or framework-specific state."""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from .delivery_review_contract import DeliveryReviewState
from .model import ModelInputReceipt, ModelReplayMetadata, ModelToolCall


class Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RunPhase(str, Enum):
    CREATED = "created"
    PREPARING_CONTEXT = "preparing_context"
    REQUESTING_MODEL = "requesting_model"
    EXECUTING_TOOLS = "executing_tools"
    AWAITING_INTERACTION = "awaiting_interaction"
    COMPACTING = "compacting"
    VERIFYING = "verifying"
    FINALIZING = "finalizing"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        return self in {self.COMPLETED, self.FAILED, self.CANCELED, self.INTERRUPTED}


class RunScope(Contract):
    tenant_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)


class ChildApproval(Contract):
    """Host reference to an existing child pause, never a model permission."""

    child_run_id: str = Field(min_length=1)
    child_scope: RunScope
    parent_operation_key: str = Field(min_length=1)
    parent_tool_call_id: str = Field(min_length=1)
    interaction_id: str = Field(min_length=1)
    host_interaction_id: str = Field(min_length=1)
    tool_call: ModelToolCall
    definition_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ForkSource(Contract):
    """Immutable terminal transcript boundary, separate from a session's parent."""

    source_run_id: str = Field(min_length=1)
    source_session_id: str = Field(min_length=1)
    source_revision: int = Field(strict=True, ge=0)
    source_event_seq: int = Field(strict=True, ge=0)
    source_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ForkCommand(Contract):
    """Immutable request binding; the child Run ID is its scoped lookup key."""

    command_id: str = Field(min_length=1, max_length=160, pattern=r"\S")
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ChildBudgetReservation(Contract):
    """Parent-owned allowance, distinct from the child's actual usage.

    A startup receipt binds this to one child and one parent operation. The
    allowance remains charged until a terminal receipt settles it, including
    across context compaction and Store serialization.
    """

    child_run_id: str = Field(min_length=1)
    child_scope: RunScope
    parent_operation_key: str = Field(min_length=1)
    parent_tool_call_id: str = Field(min_length=1)
    child_request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    configuration_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_model_calls: int = Field(strict=True, ge=1)
    max_tool_calls: int = Field(strict=True, ge=0)
    settled_usage_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class InterruptedToolCall(Contract):
    """Host disposition of an accepted call, never an invented execution result."""

    call_id: str = Field(min_length=1)
    execution_status: Literal["not_dispatched", "unknown"]
    operation_key: str | None = Field(default=None, min_length=1,
                                      exclude_if=lambda value: value is None)


class ToolBatchInterruption(Contract):
    schema_version: Literal["ksadk.native-tool-interruption/v1"] = (
        "ksadk.native-tool-interruption/v1"
    )
    source_message_index: int = Field(strict=True, ge=0)
    source_message_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1)
    calls: tuple[InterruptedToolCall, ...] = Field(min_length=1)


class RunSnapshot(Contract):
    schema_version: Literal["ksadk.native-run/v1"] = "ksadk.native-run/v1"
    scope: RunScope
    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    parent_run_id: str | None = Field(default=None, min_length=1)
    fork_source: ForkSource | None = Field(default=None, exclude_if=lambda value: value is None)
    fork_command: ForkCommand | None = Field(default=None, exclude_if=lambda value: value is None)
    revision: int = Field(default=0, ge=0)
    event_seq: int = Field(default=0, ge=0)
    phase: RunPhase = RunPhase.CREATED
    resume_phase: RunPhase | None = None
    paused_from: RunPhase | None = None
    goal: str = Field(min_length=1)
    # Invocation identity is immutable; the active goal changes through steering.
    # Older snapshots have no independent proof once their goal has changed.
    initial_goal_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    goal_revision: int = Field(default=0, ge=0)
    pending_guidance_ids: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)
    messages: tuple[dict[str, JsonValue], ...] = ()
    context_binding: str = ""
    prepared_context: dict[str, JsonValue] | None = None
    context_source_goal_revision: int | None = Field(
        default=None, strict=True, ge=0, exclude_if=lambda value: value is None
    )
    context_source_receipt: dict[str, JsonValue] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    compaction_request: dict[str, JsonValue] | None = None
    compaction_history: dict[str, JsonValue] = Field(default_factory=dict)
    force_compaction: bool = False
    context_overflows: int = Field(default=0, ge=0)
    retry_not_before: float = Field(default=0, ge=0, allow_inf_nan=False)
    pending_intent: str | None = None
    model_input_observation: ModelInputReceipt | None = Field(
        default=None, repr=False, exclude_if=lambda value: value is None,
    )
    pending_tools: tuple[dict[str, JsonValue], ...] = ()
    tool_batch_interruption: ToolBatchInterruption | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    pending_interaction: str | None = None
    interaction_host_id: str | None = None
    interaction: dict[str, JsonValue] | None = None
    child_approval: ChildApproval | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    execution_elapsed_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    # Conservative lost-model reservation, separate from measured active time.
    model_recovery_debt_seconds: float = Field(
        default=0, strict=True, ge=0, allow_inf_nan=False, exclude_if=lambda value: value == 0
    )
    # New successful observations from host-declared readers. Call IDs,
    # arguments, todos and summaries cannot advance this cursor. Deduplication
    # survives compaction; exhaustion conservatively stops crediting novelty.
    observed_progress_revision: int = Field(default=0, strict=True, ge=0)
    observed_progress_fingerprints: tuple[str, ...] = Field(default=(), max_length=2048)
    # Host-observed error streak survives message pruning and compaction.
    repeated_tool_error_signature: str | None = None
    repeated_tool_error_count: int = Field(default=0, ge=0)
    # One model decision can contain a failed batch. Count that decision once;
    # its individual failures have not yet been observed by the next exchange.
    repeated_tool_error_model_call: int | None = Field(default=None, ge=0)
    repeated_tool_error_goal_revision: int = Field(default=0, ge=0)
    repeated_tool_error_workspace_revision: str | None = None
    # Host-owned failed interaction evidence is not lost when messages compact.
    unresolved_interaction_observation: dict[str, JsonValue] | None = None
    # Latest successful host-browser screenshot; unlike model text, this is a
    # durable receipt that can be compared with the last workspace mutation.
    last_browser_observation: dict[str, JsonValue] | None = None
    # Only typed host browser receipts populate these references.
    browser_session_refs: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=16)
    # Retired resource identities awaiting a newly admitted observation.
    browser_reobservation_refs: tuple[dict[str, JsonValue], ...] = Field(default=(), max_length=16)
    # Host-owned activation / MCP generation references contain no credentials.
    activation_resource_ref: dict[str, JsonValue] | None = None
    # Any completed browser ToolPort receipt, including "entry not found". It
    # releases the observation-before-more-edits gate without claiming a
    # successful screenshot or clearing an unresolved interactive failure.
    last_browser_attempt_tool_call: int | None = Field(default=None, strict=True, ge=0)
    descendant_model_calls: int = Field(default=0, ge=0)
    descendant_tool_calls: int = Field(default=0, ge=0)
    child_run_ids: tuple[str, ...] = ()
    child_lifecycle_cursor: str | None = Field(default=None, exclude_if=lambda value: value is None)
    child_lifecycle_failure: Literal["poll", "wait", "cancel"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    pending_child_observation_ids: tuple[str, ...] = Field(
        default=(), exclude_if=lambda value: not value,
    )
    child_budget_reservations: tuple[ChildBudgetReservation, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    # Call IDs whose delegated child receipts were committed while the parent
    # parallel effect was still running. This preserves partial completion on
    # cancellation and prevents double accounting when the batch seals.
    parallel_child_settlements: tuple[str, ...] = ()
    model_errors: int = Field(default=0, ge=0)
    provider_errors: int = Field(default=0, ge=0)
    # Opt-in shared provider/takeover retry expenditure; never a new allowance.
    model_recovery_retries_spent: int = Field(
        default=0, strict=True, ge=0, exclude_if=lambda value: value == 0
    )
    continuations: int = Field(default=0, ge=0)
    output_restarts: int = Field(default=0, ge=0)
    repair_attempts: int = Field(default=0, ge=0)
    consecutive_failure_count: int = Field(default=0, ge=0)
    failure_fingerprint: str | None = None
    failure_evidence_digest: str | None = None
    candidate: dict[str, JsonValue] | None = None
    candidate_replay: ModelReplayMetadata | None = None
    stop_hook_state: dict[str, JsonValue] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    tool_hook_state: dict[str, JsonValue] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    precompact_hook_state: dict[str, JsonValue] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    delivery_review: DeliveryReviewState | None = None
    verification: dict[str, JsonValue] | None = None
    # Set only with a report whose candidate/contract/verifier metadata matched.
    verification_tool_calls: int | None = Field(default=None, strict=True, ge=0)
    checkpoint_workspace_revision: str | None = None
    # Prevent repeated web-delivery checkpoints for the same source mutation
    # when subsequent browser screenshots change host-owned artifacts.
    checkpoint_web_mutation_tool_call: int | None = Field(default=None, strict=True, ge=0)
    workspace_dirty_since_checkpoint: bool = False
    last_workspace_mutation_tool_call: int | None = Field(default=None, strict=True, ge=0)
    last_web_mutation_tool_call: int | None = Field(default=None, strict=True, ge=0)
    task_progress: tuple[dict[str, JsonValue], ...] = ()
    task_progress_revision: int = Field(default=0, strict=True, ge=0)
    # Validated ArtifactRef JSON written only by the runtime result governor.
    # Keeping refs in the canonical snapshot makes them survive compaction and
    # session projection without trusting tool text or a summary.
    artifact_refs: tuple[dict[str, JsonValue], ...] = ()
    workspace_revision: str | None = Field(default=None, min_length=1)
    workspace_changed_paths: tuple[str, ...] = ()
    # Host-observed changes retained across later reads and compaction.
    workspace_modified_paths: tuple[str, ...] = ()
    workspace_observation_status: Literal["initial", "captured", "unavailable"] = "unavailable"
    workspace_observation_reason: str | None = None
    workspace_observation_cursor: str | None = None
    outcome: str | None = None
    final_text: str = ""
    reason: str | None = None
    profile_digest: str = ""
    contract_digest: str = ""
    task_contract: dict[str, JsonValue] | None = None
    configuration_digest: str = ""
    origin: dict[str, JsonValue] = Field(default_factory=dict)

    @property
    def browser_reobservation_required(self) -> bool:
        return bool(self.browser_reobservation_refs)

    @property
    def model_budget_claimed(self) -> int:
        return self.model_calls + self.descendant_model_calls + sum(
            child.max_model_calls for child in self.child_budget_reservations
            if child.settled_usage_digest is None
        )

    @property
    def tool_budget_claimed(self) -> int:
        return self.tool_calls + self.descendant_tool_calls + sum(
            child.max_tool_calls for child in self.child_budget_reservations
            if child.settled_usage_digest is None
        )

    def matches_initial_goal(self, goal: str) -> bool:
        if self.initial_goal_digest is None:
            return self.goal_revision == 0 and self.goal == goal
        return self.initial_goal_digest == hashlib.sha256(goal.encode("utf-8")).hexdigest()


class RunCommand(Contract):
    command_id: str = Field(min_length=1)
    kind: Literal[
        "cancel", "policy_revoked", "pause", "resume", "submit_interaction", "steer", "guide",
        "promote_child", "message_child", "cancel_child",
    ]
    payload: dict[str, JsonValue] = Field(default_factory=dict)

    @property
    def priority(self) -> int:
        return {
            "cancel": 100,
            "policy_revoked": 95,
            "submit_interaction": 90,
            "pause": 80,
            "resume": 80,
            "steer": 60,
            "guide": 60,
            "promote_child": 70,
            "message_child": 70,
            "cancel_child": 85,
        }[self.kind]


class NativeEvent(Contract):
    event_id: str
    run_id: str
    turn_id: str
    seq: int = Field(ge=1)
    kind: str
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class EffectIntent(Contract):
    operation_key: str = Field(min_length=1)
    kind: Literal["workspace", "model", "tool", "verify", "context", "hook", "child"]
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    status: Literal["prepared", "running"] = "prepared"

    @property
    def loop_attempts(self) -> int:
        """Known admission cost; not evidence of successful external execution."""
        if self.kind == "model":
            return 1
        if self.kind != "tool":
            return 0
        calls = self.payload.get("calls")
        if self.payload.get("parallel_readonly") is True and isinstance(calls, list):
            return max(1, len(calls))
        return 1


class EffectReceipt(Contract):
    operation_key: str = Field(min_length=1)
    status: Literal["committed", "failed", "uncertain"]
    result: dict[str, JsonValue] = Field(default_factory=dict)


class CommitBatch(Contract):
    commit_id: str = Field(min_length=1)
    snapshot: RunSnapshot
    events: tuple[NativeEvent, ...] = ()
    intents: tuple[EffectIntent, ...] = ()
    receipts: tuple[EffectReceipt, ...] = ()
    consumed_command_id: str | None = None
    require_no_pending_commands: bool = Field(
        default=False, strict=True, exclude_if=lambda value: value is False
    )


class CommandReceipt(Contract):
    command_id: str
    status: Literal["accepted", "duplicate", "rejected"]
    reason: str | None = None


NORMAL = {
    RunPhase.CREATED: {RunPhase.PREPARING_CONTEXT},
    RunPhase.PREPARING_CONTEXT: {
        RunPhase.AWAITING_INTERACTION,
        RunPhase.REQUESTING_MODEL,
        RunPhase.COMPACTING,
        RunPhase.VERIFYING,
        RunPhase.FINALIZING,
    },
    RunPhase.REQUESTING_MODEL: {
        RunPhase.EXECUTING_TOOLS,
        RunPhase.VERIFYING,
        RunPhase.PREPARING_CONTEXT,
        RunPhase.FINALIZING,
    },
    RunPhase.EXECUTING_TOOLS: {
        RunPhase.PREPARING_CONTEXT,
        RunPhase.AWAITING_INTERACTION,
        RunPhase.FINALIZING,
    },
    RunPhase.COMPACTING: {RunPhase.PREPARING_CONTEXT, RunPhase.FINALIZING},
    RunPhase.VERIFYING: {
        RunPhase.PREPARING_CONTEXT, RunPhase.AWAITING_INTERACTION, RunPhase.FINALIZING,
    },
    RunPhase.FINALIZING: {RunPhase.COMPLETED, RunPhase.INTERRUPTED},
}


def move(snapshot: RunSnapshot, phase: RunPhase) -> RunSnapshot:
    """Validate a lifecycle transition; Store assigns revision at commit time."""
    current = snapshot.phase
    if current.terminal:
        raise ValueError("terminal run cannot transition")
    if phase == current:
        return snapshot
    global_states = {RunPhase.CANCELED, RunPhase.FAILED, RunPhase.FINALIZING}
    if phase == RunPhase.PAUSED:
        return snapshot.model_copy(update={"phase": phase, "paused_from": current})
    if current == RunPhase.PAUSED:
        allowed = {snapshot.paused_from, *global_states}
    elif current == RunPhase.AWAITING_INTERACTION:
        allowed = {snapshot.resume_phase, *global_states}
    else:
        allowed = NORMAL.get(current, set()) | global_states
    if phase not in allowed:
        raise ValueError(f"illegal transition {current.value} -> {phase.value}")
    return snapshot.model_copy(
        update={
            "phase": phase,
            "paused_from": None,
            "resume_phase": (
                snapshot.resume_phase if phase == RunPhase.AWAITING_INTERACTION else None
            ),
        }
    )


def workspace_observation_cursor(snapshot: RunSnapshot, purpose: str) -> str:
    if purpose == "model":
        return f"{snapshot.goal_revision}:{snapshot.model_calls}:{snapshot.tool_calls}"
    if purpose == "verification":
        candidate = snapshot.candidate or {}
        return (
            f"verify:{snapshot.goal_revision}:{snapshot.model_calls}:"
            f"{snapshot.tool_calls}:{candidate.get('candidate_id', '')}"
        )
    raise ValueError("unsupported workspace observation purpose")
