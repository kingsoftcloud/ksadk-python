"""Host-owned, scoped effect ports. Ports never receive a writable RunStore."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import PurePosixPath
from typing import Literal, Protocol, get_args

from pydantic import Field, JsonValue, model_serializer, model_validator

from .artifact_contract import ArtifactRef as ArtifactRef
from .browser_contract import BrowserRecovery, BrowserSessionRef
from .completion import CompletionCandidate, TaskContract, VerificationReport
from .context_contributions import ContextPort as ContextPort
from .context_contributions import ContextSourceDefinition
from .delivery_review_contract import DeliveryReviewPolicy
from .hook_runtime_contract import HookDefinition
from .model import (
    ModelImageContent,
    ModelProfile,
    ModelStreamEvent,
    ModelTextContent,
    ModelToolCall,
    ModelTurnResult,
)
from .output_contract import submission_output_format, validate_submission_schema
from .state import BackgroundCommandRef, ChildApproval, ChildBudgetReservation, Contract, RunScope
from .tool_schema import model_tool_parameters
from .turn_control import ModelTurnControl

ChildRecoveryPolicy = Literal[
    "sealed_local_child/v1",
    "prepared_local_child/v1",
    "resume_prepared_local_child/v1",
    "resume_local_child_checkpoint/v1",
]
CHILD_RECOVERY_POLICIES = frozenset(get_args(ChildRecoveryPolicy))


def digest(value: Contract) -> str:
    payload = value.model_dump(mode="json")
    if isinstance(value, RunConfiguration):
        if payload.get("final_submission_payload_key") is None:
            payload.pop("final_submission_payload_key", None)
        if payload.get("workspace_observation_policy") is None:
            payload.pop("workspace_observation_policy", None)
        for tool in payload.get("tools", []):
            if tool.get("progress_observation") is False:
                tool.pop("progress_observation", None)
            if tool.get("context_role") == "none":
                tool.pop("context_role", None)
    elif isinstance(value, ToolDefinition) and payload.get("context_role") == "none":
        payload.pop("context_role", None)
    if isinstance(value, ToolDefinition) and payload.get("progress_observation") is False:
        payload.pop("progress_observation", None)
    # This optional restriction was added after the first durable Native
    # snapshots. Omission and None retain their original identity; a pinned
    # allowlist changes the digest and remains immutable for the active goal.
    contract = payload.get("contract") if isinstance(value, RunConfiguration) else payload
    if isinstance(contract, dict) and contract.get("allowed_tool_names") is None:
        contract.pop("allowed_tool_names", None)
    if isinstance(contract, dict) and contract.get("final_submission_schema") is None:
        contract.pop("final_submission_schema", None)
    if isinstance(contract, dict) and contract.get("final_submission_payload_key") is None:
        contract.pop("final_submission_payload_key", None)
    if isinstance(contract, dict) and contract.get("host_resolved_answer") is None:
        contract.pop("host_resolved_answer", None)
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def validate_task_tool_scope(contract: TaskContract, tools: tuple[ToolDefinition, ...]) -> None:
    allowed = contract.allowed_tool_names
    if allowed is not None and not set(allowed).issubset(tool.name for tool in tools):
        raise ValueError("task contract references an unbound tool")


class ToolDefinition(Contract):
    name: str = Field(min_length=1)
    description: str
    parameters: dict[str, JsonValue]
    # Host-only role. A failed interactive observation survives compaction
    # until an actual interactive recheck succeeds.
    observation_role: Literal["none", "browser"] = "none"
    # Host-owned scheduling authority, never projected into the model schema.
    # Only independent read-only calls may share one durable effect batch.
    read_only: bool = False
    # Admit a non-reader's result as a new observation (e.g. MCP or a command).
    # This grants no read-only/concurrency, acceptance or replay authority.
    progress_observation: bool = False
    # Host-pinned context semantics. A reader alias can retain source receipts
    # without the Kernel recognizing its name. Never exposed to the model.
    context_role: Literal[
        "none", "workspace_read", "workspace_search", "workspace_list", "artifact_read", "task_planning"
    ] = "none"
    concurrent_safe: bool = False
    # Host-pinned writer retained in a deadline delivery window. This neither
    # bypasses execution policy nor grants verification or replay authority.
    delivery_write: bool = Field(default=False, exclude_if=lambda value: not value)
    # A sealed host role for inspecting this goal's successful file deliveries.
    # The Kernel derives paths from executed receipts, never from model text.
    delivery_artifact_role: Literal["none", "read", "write"] = Field(
        default="none", exclude_if=lambda value: value == "none",
    )
    # Host-only grant for a read-only delegated child. Unlike ordinary reads,
    # its sealed receipt may include descendant model/tool usage and child IDs.
    delegated_read_only: bool = False
    # Optional host observation profile. A change cannot silently add feedback
    # to a previously locked tool or an existing Run configuration.
    feedback_profile_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    # Host action policy is part of the locked definition. A stricter execution
    # boundary must not silently replace the policy of an existing Run.
    execution_policy_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    # Host-only preservation policy, locked before execution. A bounded
    # callback model view cannot discard the original observation remainder.
    result_archive_policy: Literal[
        "archived-model-view/v1", "archived-model-view/v2", "archived-model-view/v3",
        "archived-model-view/v4", "archived-model-view/v5",
    ] | None = None
    # Explicit host policy for status-preserving typed model observations.
    # Remote JSON cannot grant this policy or rewrite the executed receipt.
    result_model_policy: Literal["typed-tool-observation/v1"] | None = None
    # Runtime-owned MCP privacy/offload policy; tool-returned JSON cannot grant it.
    result_disclosure_policy: Literal["mcp-default/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    # Host-pinned context retention, never inferred from a result's text.
    # Progressive Skill instructions remain available during local pruning.
    context_retention: Literal["normal", "persistent"] = "normal"
    child_budget_policy: Literal["start/v1", "settle/v1", "delegate/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    child_approval_policy: Literal["owned-child-approval/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    background_command_policy: Literal["owned-command-launch/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_serializer(mode="wrap")
    def omit_unconfigured_feedback(self, serialize):
        payload = serialize(self)
        if self.feedback_profile_digest is None:
            payload.pop("feedback_profile_digest", None)
        if self.execution_policy_digest is None:
            payload.pop("execution_policy_digest", None)
        if self.result_archive_policy is None:
            payload.pop("result_archive_policy", None)
        if self.result_model_policy is None:
            payload.pop("result_model_policy", None)
        if self.context_retention == "normal":
            payload.pop("context_retention", None)
        return payload

    @model_validator(mode="after")
    def valid_concurrency(self) -> ToolDefinition:
        if self.background_command_policy is not None and (self.read_only or self.concurrent_safe):
            raise ValueError("background launch requires serialized mutation admission")
        # Dual-mode delegates have per-member allocation accounting. Other
        # lifecycle actions still require serialized ledger admission.
        if self.child_budget_policy in {"start/v1", "settle/v1"} and self.concurrent_safe:
            raise ValueError("child budget lifecycle requires serialized admission")
        if (self.child_budget_policy == "delegate/v1" and self.concurrent_safe
                and not self.delegated_read_only):
            raise ValueError("parallel dual-mode delegates require delegated_read_only")
        if self.context_role == "workspace_read" and not self.read_only:
            raise ValueError("workspace_read context role requires read_only")
        if self.result_archive_policy is not None and not self.read_only:
            raise ValueError("archived model views require a read_only tool")
        if (self.result_disclosure_policy is not None
                and self.result_model_policy != "typed-tool-observation/v1"):
            raise ValueError("MCP disclosure requires typed model observations")
        if self.result_model_policy is not None and self.result_archive_policy is not None:
            raise ValueError("a tool must select one model observation policy")
        if (
            self.result_archive_policy == "archived-model-view/v5"
            and self.context_role != "workspace_read"
        ):
            raise ValueError("captured source archives require workspace_read")
        if self.context_role == "workspace_search" and not self.read_only:
            raise ValueError("workspace_search context role requires read_only")
        if self.context_role == "artifact_read" and not self.read_only:
            raise ValueError("artifact_read context role requires read_only")
        if self.context_role == "task_planning" and self.read_only:
            raise ValueError("task planning updates mutate host state")
        if self.concurrent_safe and (not self.read_only or self.observation_role != "none"):
            raise ValueError("concurrent_safe tools must be read_only without observation state")
        if self.delegated_read_only and not self.concurrent_safe:
            raise ValueError("delegated_read_only requires concurrent_safe")
        return self

    def function_schema(self) -> dict[str, JsonValue]:
        """Project only model-visible fields; host progress metadata stays local."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": model_tool_parameters(self.parameters),
        }


class RunLimits(Contract):
    # Active execution time, excluding a committed pause or approval wait.
    # Omitted SDK budgets keep their original configuration identity.
    max_wall_seconds: float | None = Field(default=None, gt=0, le=86400, allow_inf_nan=False)
    # No implicit count ceiling. Explicit finite values from sealed legacy
    # configurations continue to validate and retain their original digest.
    max_model_calls: int | None = Field(default=None, strict=True, ge=1)
    max_tool_calls: int | None = Field(default=None, strict=True, ge=0)
    max_parallel_tools: int = Field(default=4, ge=1, le=16)
    max_repairs: int = Field(default=3, ge=0)
    # Optional host-owned evidence checkpoint before the model budget is spent.
    verification_reserve_model_calls: int = Field(default=0, ge=0)
    verification_idle_tool_calls: int = Field(default=0, ge=0)
    # Host-owned maximum tool-call gap between browser attempts while web
    # changes remain unobserved; zero leaves only the late reserve gate.
    browser_observation_interval_tool_calls: int = Field(default=0, ge=0)
    # Once this many model calls remain, expose a no-tool finalization turn.
    # A finite SDK run reserves its final submission by default. Explicit zero
    # opts out (including replay of already locked, serialized configurations).
    # Hosts with model-backed tools reserve their maximum per-tool model cost
    # plus one final call; the Kernel never silently raises the hard budget.
    finalization_reserve_model_calls: int = Field(default=1, ge=0)
    # Opt-in host policy. Zero preserves existing sealed configurations.
    finalization_reserve_wall_seconds: int = Field(
        default=0, strict=True, ge=0, le=240, exclude_if=lambda value: value == 0,
    )
    delivery_context_recovery_policy: Literal[
        "committed-write-summary/v1", "delivery-window-summary/v2",
    ] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    max_output_restarts: int = Field(default=0, ge=0)
    max_subagent_depth: int = Field(default=3, ge=0, le=16)
    effect_timeout_seconds: float = Field(default=120, gt=0, le=3600)
    max_tool_result_bytes: int = Field(default=65536, ge=1024, le=4 * 1024 * 1024)
    max_tool_image_bytes: int = Field(default=8 * 1024 * 1024, ge=0, le=64 * 1024 * 1024)

    @model_serializer(mode="wrap")
    def omit_unconfigured_wall_budget(self, serialize):
        # Preserve legacy serialized configurations as well as their digest.
        payload = serialize(self)
        if self.max_wall_seconds is None:
            payload.pop("max_wall_seconds", None)
        return payload


class WorkspaceObservationPolicy(Contract):
    """Optional task policy; the generic Kernel has no builtin file types."""

    role: Literal["browser"] = "browser"
    path_suffixes: tuple[str, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def valid_suffixes(self) -> WorkspaceObservationPolicy:
        if any(not s.startswith(".") or s != s.lower() or "/" in s for s in self.path_suffixes):
            raise ValueError("observation suffixes must be lowercase file suffixes")
        return self


class RunConfiguration(Contract):
    profile: ModelProfile
    contract: TaskContract
    tools: tuple[ToolDefinition, ...] = ()
    limits: RunLimits = Field(default_factory=RunLimits)
    instructions: str = ""
    context_sources: tuple[ContextSourceDefinition, ...] = Field(default=(), max_length=16)
    hooks: tuple[HookDefinition, ...] = ()
    hook_stop_continuation_limit: int | None = Field(default=None, strict=True, ge=0)
    # Host authorization/activation identity, separate from model instructions.
    host_binding_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    # Distinct host grants: sealed query, verify-only takeover, or continuation
    # from an undispatched child effect. No policy replays a running effect.
    tool_recovery_policy: ChildRecoveryPolicy | None = None
    tool_error_recovery_policy: Literal["diagnose-before-repeat/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    tool_error_recovery_model_calls: int | None = Field(
        default=None, strict=True, ge=1, le=10, exclude_if=lambda value: value is None
    )
    child_lifecycle_policy: Literal["owned-background/v1", "owned-background/v2"] | None = None
    child_promotion_policy: Literal["owned-child-promotion/v1"] | None = None
    child_input_policy: Literal["owned-child-guide/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    child_release_policy: Literal["owned-run-release/v1"] | None = None
    child_notification_policy: Literal["ready-before-model/v1"] | None = None
    background_notification_policy: Literal["completed-before-model/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    child_approval_policy: Literal["owned-child-approval/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    guidance_policy: Literal["boundary-guide/v1"] | None = None
    model_recovery_policy: Literal["pure-inference-takeover/v1"] | None = None
    # Host-pinned distinction between resuming work and completing a text suffix.
    # None preserves the legacy contract/configuration digest for old activations.
    output_recovery_policy: Literal["task-or-delivery/v1"] | None = None
    workspace_observation_policy: WorkspaceObservationPolicy | None = None
    delivery_review_policy: DeliveryReviewPolicy | None = None
    # Host-pinned delivery contract; prose and code fences stay untouched by default.
    final_output_format: Literal["text", "json_object", "json_value"] = "text"
    # Optional host-pinned final submission protocol. This is a model-visible
    # function schema, not an executable user tool or verifier replacement.
    final_submission_schema: dict[str, JsonValue] | None = None
    final_submission_payload_key: str | None = Field(default=None, min_length=1)
    delegation_depth: int = Field(default=0, ge=0, le=16)

    @model_serializer(mode="wrap")
    def omit_unconfigured_review(self, serialize):
        payload = serialize(self)
        if self.delivery_review_policy is None:
            payload.pop("delivery_review_policy", None)
        if self.host_binding_digest is None:
            payload.pop("host_binding_digest", None)
        if self.tool_recovery_policy is None:
            payload.pop("tool_recovery_policy", None)
        if self.child_lifecycle_policy is None:
            payload.pop("child_lifecycle_policy", None)
        if self.child_promotion_policy is None:
            payload.pop("child_promotion_policy", None)
        if self.child_release_policy is None:
            payload.pop("child_release_policy", None)
        if self.child_notification_policy is None:
            payload.pop("child_notification_policy", None)
        if self.guidance_policy is None:
            payload.pop("guidance_policy", None)
        if not self.context_sources:
            payload.pop("context_sources", None)
        if not self.hooks:
            payload.pop("hooks", None)
        if self.hook_stop_continuation_limit is None:
            payload.pop("hook_stop_continuation_limit", None)
        if self.model_recovery_policy is None:
            payload.pop("model_recovery_policy", None)
        if self.output_recovery_policy is None:
            payload.pop("output_recovery_policy", None)
        return payload

    @model_validator(mode="after")
    def unique_tools(self) -> RunConfiguration:
        if self.background_notification_policy is not None and not any(
            tool.background_command_policy is not None for tool in self.tools
        ):
            raise ValueError("background notifications require a pinned launcher")
        if self.model_recovery_policy is not None and self.host_binding_digest is None:
            raise ValueError("model takeover requires a pinned host binding")
        if self.child_input_policy is not None and self.child_lifecycle_policy is None:
            raise ValueError("child input requires owned child lifecycle")
        if self.child_approval_policy is not None and self.child_lifecycle_policy is None:
            raise ValueError("child approval requires owned child lifecycle")
        if self.child_promotion_policy is not None and self.child_lifecycle_policy is None:
            raise ValueError("child promotion requires owned child lifecycle")
        if self.child_release_policy is not None and self.child_lifecycle_policy is None:
            raise ValueError("child release requires owned child lifecycle")
        if self.child_notification_policy is not None and self.child_lifecycle_policy is None:
            raise ValueError("child notifications require owned child lifecycle")
        if len({hook.hook_id for hook in self.hooks}) != len(self.hooks):
            raise ValueError("duplicate Hook definitions")
        if any("Stop" in hook.events for hook in self.hooks):
            if self.hook_stop_continuation_limit is None:
                raise ValueError("Stop Hooks require an explicit continuation limit")
        elif self.hook_stop_continuation_limit is not None:
            raise ValueError("Stop continuation limit requires a Stop Hook")
        if any(hook.timeout_seconds >= self.limits.effect_timeout_seconds for hook in self.hooks):
            raise ValueError("Hook deadline must precede the effect deadline")
        if len({source.source_id for source in self.context_sources}) != len(self.context_sources):
            raise ValueError("duplicate Context source definitions")
        if any(source.contributor_policy is not None for source in self.context_sources) and (
            sum(source.timeout_seconds for source in self.context_sources)
            >= self.limits.effect_timeout_seconds
        ):
            raise ValueError("Context collection deadlines must precede the effect deadline")
        if len({tool.name for tool in self.tools}) != len(self.tools):
            raise ValueError("duplicate tool definitions")
        if self.delivery_review_policy is not None:
            from .delivery_review import REVIEW_SUBMISSION_TOOL

            if any(tool.name == REVIEW_SUBMISSION_TOOL for tool in self.tools):
                raise ValueError("delivery review submission tool name is reserved")
        validate_task_tool_scope(self.contract, self.tools)
        if self.final_submission_schema is not None:
            from .final_submission import FINAL_SUBMISSION_TOOL

            if any(tool.name == FINAL_SUBMISSION_TOOL for tool in self.tools):
                raise ValueError("final submission tool name is reserved")
            validate_submission_schema(
                self.final_submission_schema, self.final_submission_payload_key
            )
            expected = submission_output_format(
                self.final_submission_schema, self.final_submission_payload_key
            )
            if self.final_output_format != expected:
                raise ValueError(f"final submission requires {expected} output")
        elif self.final_submission_payload_key is not None:
            raise ValueError("final submission payload requires a schema")
        # Give the ModelPort time to return a classified transport error before
        # the outer effect deadline turns the whole attempt into an uncertain
        # receipt. The latter cannot safely enter the bounded model retry path.
        request_timeout = self.profile.request_timeout_seconds
        if request_timeout is not None and request_timeout >= self.limits.effect_timeout_seconds:
            raise ValueError("model request timeout must precede effect timeout")
        return self

    @property
    def digest(self) -> str:
        return digest(self)

    def with_task_contract(self, contract: TaskContract) -> RunConfiguration:
        """Compile an active contract from this locked base configuration."""
        validate_task_tool_scope(contract, self.tools)
        values = {**self.model_dump(), "contract": contract}
        if contract.final_submission_schema is not None:
            values.update(
                final_output_format=submission_output_format(
                    contract.final_submission_schema, contract.final_submission_payload_key,
                ),
                final_submission_schema=contract.final_submission_schema,
                final_submission_payload_key=contract.final_submission_payload_key,
            )
        return RunConfiguration.model_validate(values)


class ModelRequest(Contract):
    scope: RunScope
    run_id: str
    request_id: str
    attempt_id: str
    profile: ModelProfile
    # Host admission, not inferred from an empty tool list or prompt wording.
    purpose: Literal["task", "compaction", "delivery_review"] = "task"
    max_output_tokens: int | None = Field(default=None, strict=True, ge=1)
    instructions: str
    messages: tuple[dict[str, JsonValue], ...]
    tools: tuple[ToolDefinition, ...]
    turn_control: ModelTurnControl | None = None

    @model_validator(mode="after")
    def output_limit_within_profile(self) -> ModelRequest:
        if self.purpose == "compaction" and (self.tools or self.turn_control is not None):
            raise ValueError("compaction cannot admit task tools or final-turn control")
        if self.turn_control is not None:
            self.turn_control.validate_tools(
                tuple(tool.name for tool in self.tools),
                stable_catalog=self.profile.tool_catalog_policy == "stable-task/v1",
            )
        if (
            self.max_output_tokens is not None
            and self.max_output_tokens > self.profile.max_output_tokens
        ):
            raise ValueError("model request output limit exceeds pinned profile")
        return self

    @property
    def effective_max_output_tokens(self) -> int:
        return self.max_output_tokens or self.profile.max_output_tokens

    @property
    def effective_tool_choice(self) -> str | dict[str, JsonValue]:
        control = self.turn_control
        if self.profile.tool_catalog_policy == "stable-task/v1" and control is not None:
            if control.finalization_only:
                return ({"type": "function", "function": {"name": "submit_final_output"}}
                        if control.final_submission_required else "none")
        return control.tool_choice if control is not None else "auto"


class ToolBudget(Contract):
    max_model_calls_remaining: int | None = Field(default=0, strict=True, ge=0)
    # This allowance already excludes the parent's final submission. Legacy
    # standalone adapters may still receive a gross allowance (default False).
    parent_finalization_reserved: bool = Field(default=False, strict=True)
    max_tool_calls_remaining: int | None = Field(default=0, strict=True, ge=0)
    depth: int = Field(default=0, strict=True, ge=0)
    max_depth: int = Field(default=0, strict=True, ge=0)


class ToolResourceUsage(Contract):
    model_calls: int = Field(default=0, strict=True, ge=0)
    tool_calls: int = Field(default=0, strict=True, ge=0)
    input_tokens: int | None = Field(default=None, strict=True, ge=0)
    output_tokens: int | None = Field(default=None, strict=True, ge=0)
    cached_input_tokens: int | None = Field(default=None, strict=True, ge=0)
    reasoning_tokens: int | None = Field(default=None, strict=True, ge=0)
    child_run_ids: tuple[str, ...] = ()


class ChildBudgetSettlement(Contract):
    """A trusted child owner binds actual ToolResult usage to its terminal state."""

    child_run_id: str = Field(min_length=1)
    parent_operation_key: str = Field(min_length=1)
    child_request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    terminal_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ToolRequest(Contract):
    scope: RunScope
    run_id: str
    operation_key: str
    call: ModelToolCall
    definition: ToolDefinition
    # Host-only projection: the exact wrapper call in the Run ledger when
    # policy is evaluated for its concrete remote target. Model arguments
    # cannot fill this field or change the operation/approval identity.
    admission_call: ModelToolCall | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    # An opaque decision returned by the authenticated control plane. Executors
    # must validate current policy, scope, call arguments and grant on every call.
    interaction: dict[str, JsonValue] | None = None
    child_approval: ChildApproval | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    budget: ToolBudget = Field(default_factory=ToolBudget)
    # Native owns final result governance. Direct executor callers retain the
    # bounded legacy behavior unless they explicitly use this mode.
    result_delivery: Literal["executor_managed", "runtime_managed"] = "executor_managed"
    available_artifacts: tuple[ArtifactRef, ...] = ()


class ToolModelObservation(Contract):
    """Host-derived model data; it cannot change execution or acceptance facts."""

    version: Literal["typed-tool-observation/v1"] = "typed-tool-observation/v1"
    status: Literal["ok", "error"]
    content: JsonValue = None
    content_blocks: tuple[ModelTextContent | ModelImageContent, ...] = ()

    def message_content(self) -> str | list[dict]:
        envelope = json.dumps({"status": self.status, "content": self.content}, ensure_ascii=False)
        return (
            [{"type": "text", "text": envelope},
             *(block.model_dump(mode="json") for block in self.content_blocks)]
            if self.content_blocks else envelope
        )


class ToolResult(Contract):
    status: Literal["ok", "error", "approval_required", "uncertain"]
    content: JsonValue = None
    # Host-owned public view; never populated by promoting a key from content.
    public_display: dict[str, JsonValue] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    background_command_ref: BackgroundCommandRef | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    background_command_finished: bool = Field(
        default=False, strict=True, exclude_if=lambda value: value is False
    )
    # A host Port can identify the actual observation independently of its
    # transport envelope. Only a host-declared observing tool earns novelty;
    # this digest cannot authorize an action or establish task acceptance.
    progress_observation_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    # Optional host-owned model view. Keep the original content in the effect
    # receipt; only successful, bounded plain-text projections may bypass the
    # generic status/content envelope in the model transcript.
    model_content: str | None = Field(default=None, min_length=1, max_length=65536)
    model_observation: ToolModelObservation | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    content_blocks: tuple[ModelTextContent | ModelImageContent, ...] = ()
    # Host-observed write paths; never inferred from model text or tool content.
    workspace_changed_paths: tuple[str, ...] = ()
    artifact_refs: tuple[ArtifactRef, ...] = ()
    resource_usage: ToolResourceUsage = Field(default_factory=ToolResourceUsage)
    child_budget_reservation: ChildBudgetReservation | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    child_budget_settlement: ChildBudgetSettlement | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    # Typed host reference, never promoted from page text or tool JSON content.
    browser_session_ref: BrowserSessionRef | None = None
    # Confirmed owner closure, independent of bounded model-view delivery.
    browser_session_closed: bool = Field(
        default=False, strict=True, exclude_if=lambda value: value is False
    )
    interaction_id: str | None = None
    child_approval: ChildApproval | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def valid_interaction(self) -> ToolResult:
        if self.background_command_ref is not None and self.status != "ok":
            raise ValueError("background command reference requires a successful launcher receipt")
        if self.background_command_finished and self.background_command_ref is None:
            raise ValueError("background completion requires its launcher reference")
        if self.child_approval is not None and self.status != "approval_required":
            raise ValueError("child approval requires an approval receipt")
        if self.child_budget_reservation is not None:
            if self.child_budget_settlement is not None or self.status != "ok":
                raise ValueError("child_budget startup must be a separate successful receipt")
            if self.child_budget_reservation.settled_usage_digest is not None:
                raise ValueError("child_budget startup cannot pre-settle usage")
            if self.resource_usage != ToolResourceUsage():
                raise ValueError("child_budget startup cannot fabricate actual usage")
        if self.child_budget_settlement is not None and self.status == "approval_required":
            raise ValueError("child_budget settlement cannot request approval")
        if self.browser_session_closed and self.browser_session_ref is None:
            raise ValueError("browser closure needs a typed host reference")
        if self.model_content is not None and self.status != "ok":
            raise ValueError("only successful tool results can override model content")
        if self.model_observation is not None:
            if self.model_observation.status != self.status:
                raise ValueError("model observation must preserve execution status")
            if self.model_content is not None or self.content_blocks:
                raise ValueError("typed observations cannot coexist with another model view")
        if self.status == "approval_required" and not self.interaction_id:
            raise ValueError("approval_required needs an interaction_id")
        if self.status != "approval_required" and self.interaction_id is not None:
            raise ValueError("only approval_required can expose an interaction_id")
        if len(set(self.workspace_changed_paths)) != len(self.workspace_changed_paths):
            raise ValueError("workspace changed paths must be unique")
        for path in self.workspace_changed_paths:
            normalized = PurePosixPath(path)
            if (
                not path
                or "\\" in path
                or normalized.is_absolute()
                or ".." in normalized.parts
                or normalized.as_posix() != path
            ):
                raise ValueError("workspace changed paths must be normalized relative paths")
        return self


class BrowserRecoveryPort(Protocol):
    async def recover_sessions(
        self, refs: tuple[BrowserSessionRef, ...]
    ) -> tuple[BrowserRecovery, ...]: ...


class ArtifactWriteRequest(Contract):
    scope: RunScope
    run_id: str = Field(min_length=1)
    operation_key: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    content: str
    mime_type: str = Field(min_length=1, max_length=160, pattern=r"^[^\r\n]+$")


class ArtifactReadRequest(Contract):
    scope: RunScope
    run_id: str = Field(min_length=1)
    ref: ArtifactRef
    offset_chars: int = Field(default=0, strict=True, ge=0)
    limit_chars: int = Field(default=16000, strict=True, ge=1, le=32000)


class ArtifactReadResult(Contract):
    ref: ArtifactRef
    content: str
    offset_chars: int = Field(strict=True, ge=0)
    next_offset_chars: int = Field(strict=True, ge=0)
    complete: bool

    @model_validator(mode="after")
    def valid_page(self) -> ArtifactReadResult:
        if self.next_offset_chars < self.offset_chars:
            raise ValueError("artifact page cursor moved backwards")
        if self.next_offset_chars != self.offset_chars + len(self.content):
            raise ValueError("artifact page cursor must equal returned character count")
        if bool(self.content) != (self.next_offset_chars > self.offset_chars):
            raise ValueError("artifact page content and cursor disagree")
        return self


class WorkspaceRequest(Contract):
    scope: RunScope
    run_id: str = Field(min_length=1)
    operation_key: str = Field(min_length=1)


class WorkspaceObservation(Contract):
    """Host-captured workspace identity; provider/tool text cannot create one."""

    operation_key: str = Field(min_length=1)
    status: Literal["captured", "unavailable"]
    revision: str | None = Field(default=None, min_length=1)
    changed_paths: tuple[str, ...] = Field(default=(), max_length=2048)
    reason: str | None = Field(default=None, max_length=80, pattern=r"^[a-z0-9_]+$")

    @model_validator(mode="after")
    def valid_capture(self) -> WorkspaceObservation:
        if self.status == "captured":
            if self.revision is None or self.reason is not None:
                raise ValueError("captured workspace needs revision without failure reason")
        elif self.revision is not None or self.changed_paths or self.reason is None:
            raise ValueError("unavailable workspace needs only a safe reason")
        if len(set(self.changed_paths)) != len(self.changed_paths):
            raise ValueError("workspace changed paths must be unique")
        for path in self.changed_paths:
            normalized = PurePosixPath(path)
            if (
                not path
                or "\\" in path
                or normalized.is_absolute()
                or ".." in normalized.parts
                or normalized.as_posix() != path
            ):
                raise ValueError("workspace changed paths must be normalized relative paths")
        return self


class VerificationRequest(Contract):
    scope: RunScope
    run_id: str
    candidate_id: str
    final_text: str
    purpose: Literal["final", "checkpoint"] = "final"
    contract: TaskContract
    subject_context_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class VerificationBundle(Contract):
    candidate: CompletionCandidate
    report: VerificationReport | None
    post_workspace: WorkspaceObservation | None = None


class ModelPort(Protocol):
    async def complete(self, request: ModelRequest) -> ModelTurnResult: ...


class StreamingModelPort(ModelPort, Protocol):
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        """Produce ordered provisional observations and exactly one sealed result.

        The Port owns its resources and must cooperate with cancellation.
        The consumer joins an optional aclose operation; an AsyncIterator is
        not required to implement async-generator extensions. Cleanup failure
        is an operational warning, never a new provider reply or retry grant.
        """
        ...


class ToolPort(Protocol):
    async def execute(self, request: ToolRequest) -> ToolResult:
        """Validate schema/policy before dispatch; never return ok on denial."""
        ...


class ToolReconciliationPort(Protocol):
    async def reconcile(self, request: ToolRequest) -> ToolResult:
        """Read an authenticated sealed result; never execute missing/active work."""
        ...


class ToolPreparedRecoveryPort(Protocol):
    async def recover_prepared(self, request: ToolRequest) -> ToolResult:
        """Recover a matching leased, undispatched child verifier; never replay tools/models."""
        ...


class ToolPreparedContinuationPort(Protocol):
    async def resume_prepared(self, request: ToolRequest) -> ToolResult:
        """Continue a matching leased child from an undispatched effect after owner expiry."""
        ...


class ToolCheckpointContinuationPort(Protocol):
    async def resume_checkpoint(self, request: ToolRequest) -> ToolResult:
        """Continue a matching leased child from a known committed checkpoint."""
        ...


class WorkspacePort(Protocol):
    async def capture(self, request: WorkspaceRequest) -> WorkspaceObservation:
        """Return a scoped host observation without mutating the workspace."""
        ...


class ArtifactPort(Protocol):
    async def write(self, request: ArtifactWriteRequest) -> ArtifactRef:
        """Persist exact text idempotently for the supplied operation identity."""
        ...

    async def read(self, request: ArtifactReadRequest) -> ArtifactReadResult:
        """Read a bounded page after enforcing the authenticated scope."""
        ...


class VerificationPort(Protocol):
    async def verify(self, request: VerificationRequest) -> VerificationBundle:
        """Seal artifact/environment digests and resolve trusted evidence refs.

        Verifier code and expected values must be outside the agent's writable
        workspace. Changed artifacts during verification invalidate the report.
        """
        ...
