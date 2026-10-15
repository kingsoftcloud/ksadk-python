"""Immutable context contracts and lossless continuity/message projections."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    model_serializer,
    model_validator,
)

from .context import (
    CompactionProjection,
    ContextBudget,
    ContextMessage,
    ExecutionContinuityState,
)
from .context_history import (
    ContextRuntimeError,
    _goal_message_index,
    _json,
)
from .context_partition import (
    SummaryPartitionState,
)
from .model import ModelProfile
from .turn_control import ModelTurnControl

Messages = tuple[dict[str, JsonValue], ...]
READER_SUMMARY_POLICY_VERSION = "complete-reader-summary/v14"


class CompactionProjectionOverflow(ContextRuntimeError):
    """A valid summary cannot fit together with the retained task projection."""


def _complete_source_entry(entry):
    return (
        isinstance(entry.get("reader_path"), str) or entry.get("search_source") is True
        or entry.get("observed_archive") is True
        or entry.get("artifact_source") is True
        or entry.get("fresh_tool_observation") is True
        or entry.get("summary_retry_quote") == "complete-quoted-summary-retry/v1"
    )


class _Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CompactionPolicy(_Contract):
    # A disabled policy still admits and counts the complete request; it does
    # not authorize silent truncation or provider-overflow retries.
    enabled: bool = Field(default=True, strict=True, exclude_if=lambda value: value)
    trigger_ratio: float = Field(default=0.72, gt=0, le=1)
    target_ratio: float = Field(default=0.45, gt=0, lt=1)
    summary_max_tokens: int = Field(default=512, strict=True, ge=1)
    # Optional absolute ceiling. The default instead limits consecutive
    # compactions without a host-observed change or new reader evidence.
    max_compactions: int | None = Field(default=None, strict=True, ge=0)
    max_compactions_without_progress: int = Field(
        default=8,
        strict=True,
        ge=0,
        validation_alias=AliasChoices(
            "max_compactions_without_progress", "max_compactions_without_workspace_progress"
        ),
    )
    min_model_turns_between: int = Field(default=2, strict=True, ge=0)
    request_overhead_tokens: int = Field(default=0, strict=True, ge=0)
    summary_tool_output_max_chars: int = Field(default=2000, strict=True, ge=128)
    fresh_read_max_chars: int | None = Field(default=None, strict=True, ge=128)
    prune_tool_results: bool = Field(default=True, strict=True)
    prune_keep_recent_batches: int = Field(default=5, strict=True, ge=1)
    prune_min_saved_tokens: int = Field(default=256, strict=True, ge=1)
    summary_protected_tool_batches: int = Field(
        default=2,
        strict=True,
        ge=0,
        description="Legacy config field; unsummarized receipts are now always preserved.",
    )
    recent_tool_batch_max_tokens: int = Field(default=512, strict=True, ge=0)
    recent_tool_batch_count: int = Field(default=1, strict=True, ge=0)
    recent_tool_batches_total_max_tokens: int = Field(default=512, strict=True, ge=0)
    # Keep the last successful reads of distinct, still-current source files
    # as complete call/result groups. This is a bounded working set, separate
    # from the recency window that may contain only browser observations.
    read_working_set_count: int = Field(default=8, strict=True, ge=0)
    read_working_set_max_tokens: int = Field(default=6000, strict=True, ge=0)
    read_working_set_total_max_tokens: int = Field(default=16000, strict=True, ge=0)

    @model_validator(mode="after")
    def target_precedes_trigger(self) -> CompactionPolicy:
        if self.target_ratio >= self.trigger_ratio:
            raise ValueError("target_ratio must be below trigger_ratio")
        return self


class CompactionHistory(_Contract):
    abandoned_replay_digests: tuple[
        Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)], ...
    ] = Field(default=(), exclude_if=lambda value: not value)
    # Successful summaries in closed contract roots are disjoint from count,
    # which still identifies the current root's sealed projection. Old records
    # contain only count and retain their original serialization until rebased.
    prior_compactions: int = Field(default=0, strict=True, ge=0,
                                   exclude_if=lambda value: not value)
    count: int = Field(default=0, strict=True, ge=0)
    stagnant_count: int = Field(default=0, strict=True, ge=0)
    last_workspace_revision: str | None = None
    last_observed_progress_revision: int = Field(default=0, strict=True, ge=0)
    last_model_turn: int = Field(default=0, strict=True, ge=0)
    prefix_message_count: int = Field(default=0, strict=True, ge=0)
    prefix_digest: str = ""
    contract_digest: str = ""
    retained_message_refs: tuple[str, ...] = ()
    summary_text: str = ""
    tokenizer_id: str = ""
    provider_replay_projection_policy: Literal["anthropic-client-compaction/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    summary_completion_policy: Literal["bounded-committed-summary/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    summary_output_limited: bool = Field(
        default=False, strict=True, exclude_if=lambda value: not value
    )

    @property
    def total_compactions(self) -> int:
        return self.prior_compactions + self.count

    def reset_projection(self) -> dict[str, JsonValue]:
        """Discard obsolete source bindings without regranting consumed allowance."""
        preserved = {}
        if self.abandoned_replay_digests:
            preserved["abandoned_replay_digests"] = list(self.abandoned_replay_digests)
        if self.total_compactions:
            preserved.update(prior_compactions=self.total_compactions,
                             last_model_turn=self.last_model_turn)
        return preserved

    @model_validator(mode="after")
    def bounded_summary_requires_policy(self):
        if self.summary_output_limited and self.summary_completion_policy is None:
            raise ValueError("bounded summary history requires its completion policy")
        return self


class ToolResultPruning(_Contract):
    version: Literal["host-old-tool-results/v1"] = "host-old-tool-results/v1"
    source_message_refs: tuple[str, ...]
    tool_call_ids: tuple[str, ...]
    pre_input_tokens: int = Field(strict=True, ge=0)
    post_input_tokens: int = Field(strict=True, ge=0)
    saved_tokens: int = Field(strict=True, ge=1)


class PreparedContext(_Contract):
    instructions: str
    messages: Messages
    counted_input_tokens: int = Field(strict=True, ge=0)
    tokenizer_id: str
    replay_header_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", strict=True,
        exclude_if=lambda value: value is None,
    )
    turn_control: ModelTurnControl | None = None
    tool_result_pruning: ToolResultPruning | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    abandoned_replay_digests: tuple[
        Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)], ...
    ] = Field(default=(), exclude_if=lambda value: not value)


class CompactionRequest(_Contract):
    source_revision: int = Field(strict=True, ge=0)
    model_turn: int = Field(strict=True, ge=0)
    profile: ModelProfile
    continuity: ExecutionContinuityState
    source_context_digest: str
    source_message_count: int
    old_messages: tuple[ContextMessage, ...]
    retained_message_refs: tuple[str, ...]
    retained_messages: Messages
    budget: ContextBudget
    instructions: str
    replay_header_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", strict=True,
        exclude_if=lambda value: value is None,
    )
    summary_instructions: str
    summary_messages: Messages
    summary_counted_input_tokens: int
    summary_max_tokens: int
    summary_output_tokens: int = Field(strict=True, ge=1)
    tokenizer_id: str
    history: CompactionHistory
    source_reader_names: tuple[str, ...] = ()
    source_capture_reader_names: tuple[str, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    fresh_tool_observation_policy: Literal["complete-fresh-tool-group/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    fresh_tool_message_refs: tuple[str, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    fresh_text_fragment_policy: Literal["quoted-fresh-tool-text/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    protected_reader_call_ids: tuple[str, ...] = ()
    full_reader_quote: bool = False
    reader_summary_policy_version: (
        Literal[
            "complete-reader-summary/v4",
            "complete-reader-summary/v5",
            "complete-reader-summary/v6",
            "complete-reader-summary/v7",
            "complete-reader-summary/v8",
            "complete-reader-summary/v9",
            "complete-reader-summary/v10",
            "complete-reader-summary/v11",
            "complete-reader-summary/v12",
            "complete-reader-summary/v13",
            "complete-reader-summary/v14",
        ]
        | None
    ) = None
    summary_budget_mode: Literal["context_bounded_estimate"] | None = None
    summary_completion_policy: Literal["bounded-committed-summary/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    summary_output_limited: bool = Field(
        default=False, strict=True, exclude_if=lambda value: not value
    )
    chunk_index: int = Field(default=0, strict=True, ge=0)
    remaining_history_entries: tuple[dict[str, JsonValue], ...] = ()
    summary_fragments: tuple[str, ...] = ()
    summary_continuations: int = Field(default=0, strict=True, ge=0)
    turn_control: ModelTurnControl | None = None
    tool_media_projection_version: Literal["source-profile-tool-media/v1"] | None = None
    summary_partition_version: Literal["quoted-history-coverage/v1"] | None = None
    summary_partition: SummaryPartitionState | None = None

    @model_serializer(mode="wrap")
    def preserve_unversioned_wire(self, handler):
        result = handler(self)
        if self.reader_summary_policy_version is None:
            result.pop("reader_summary_policy_version", None)
        if self.summary_budget_mode is None:
            result.pop("summary_budget_mode", None)
        return result

    @model_validator(mode="after")
    def partition_protocol_matches_state(self):
        if (self.fresh_text_fragment_policy is not None
                and self.fresh_tool_observation_policy is None):
            raise ValueError("fresh text fragmentation requires source observation coverage")
        if self.fresh_tool_message_refs and self.fresh_tool_observation_policy is None:
            raise ValueError("fresh tool refs require their observation policy")
        if len(set(self.fresh_tool_message_refs)) != len(self.fresh_tool_message_refs):
            raise ValueError("fresh tool refs must be unique")
        if self.fresh_tool_observation_policy is not None and self.summary_partition is None:
            raise ValueError("fresh tool observation policy requires source coverage")
        if self.summary_output_limited and self.summary_completion_policy is None:
            raise ValueError("bounded summary request requires its completion policy")
        if (self.summary_partition_version is None) != (self.summary_partition is None):
            raise ValueError("compaction partition protocol and state must be present together")
        return self


class ContextPlan(_Contract):
    action: Literal["ready", "compact", "blocked"]
    reason: str
    prepared: PreparedContext | None = None
    request: CompactionRequest | None = None


class CompactionResult(_Contract):
    projection: CompactionProjection
    prepared: PreparedContext
    history: CompactionHistory


def source_context_digest(messages: Messages) -> str:
    return hashlib.sha256(_json(messages).encode()).hexdigest()


def _refs(messages: Messages) -> tuple[str, ...]:
    return tuple(
        f"message:{index}:{source_context_digest((message,))}"
        for index, message in enumerate(messages)
    )


def _summary(text: str) -> dict[str, JsonValue]:
    return {
        # This is previous assistant context, never a new user instruction.
        # Low-trust authority remains host metadata; the Kernel cannot accept
        # goal, permission or verdict changes from this historical text.
        "role": "assistant",
        "kind": "compaction_summary",
        "trust": "untrusted",
        "content": text,
    }


def _resume() -> dict[str, JsonValue]:
    return {
        "role": "user",
        "kind": "compaction_resume",
        "content": "Continue the current task from the summarized execution state.",
    }


def _summary_continuation() -> dict[str, JsonValue]:
    return {
        "role": "user",
        "content": (
            "The summary hit its output limit. Return only the exact missing suffix "
            "of the summary; do not repeat previous text or perform the user task."
        ),
    }


def _compacted_messages(text: str, messages: Messages, goal: str) -> Messages:
    # Keep the original goal before its historical assistant summary. Replaying
    # it after the summary would present an already executing task as a new
    # user request. The resume control is model-only, never a transcript turn.
    index = _goal_message_index(messages, goal)
    return (*messages[: index + 1], _summary(text), _resume(), *messages[index + 1 :])


def _model_continuity(state: ExecutionContinuityState) -> dict[str, JsonValue]:
    """Project bounded audit locators, never truncate the authoritative state.

    Receipts are durable host evidence references, not additional instructions
    or work to repeat. Keep a recent cursor window and a full-inventory digest;
    goal, acceptance, unresolved failures and pending protocol remain complete.
    The seal still validates the original, unabridged continuity object.
    """
    projected = state.model_dump(mode="json")
    # Keep older requests unchanged when the host supplied no target. A target
    # is actionable diagnosis; an absent one adds no information to context.
    for item in projected["acceptance_progress"]:
        if item.get("repair_target") is None:
            item.pop("repair_target", None)
    receipts = projected["tool_receipt_refs"]
    if len(receipts) > 8:
        projected["tool_receipt_count"] = len(receipts)
        projected["tool_receipt_refs_digest"] = hashlib.sha256(_json(receipts).encode()).hexdigest()
        projected["tool_receipt_refs"] = receipts[-8:]
    children = projected.get("accounted_subagent_run_ids", [])
    if len(children) > 8:
        projected["accounted_subagent_run_count"] = len(children)
        projected["accounted_subagent_run_ids_digest"] = hashlib.sha256(
            _json(children).encode()
        ).hexdigest()
        projected["accounted_subagent_run_ids"] = children[-8:]
    reads = projected.get("source_read_progress", [])
    if reads:
        projected["source_read_progress_count"] = len(reads)
        projected["source_read_progress_digest"] = hashlib.sha256(_json(reads).encode()).hexdigest()
        # Shared qualifiers and column names are encoded once. Repeating them
        # and opaque receipt locators for every page can itself force compaction
        # of a batch that fitted the original window. The full state is sealed.
        columns = (
            "path",
            "returned_lines",
            "configured_prefix_lines",
            "configured_next_start_line",
            "total_lines",
        )
        projected["source_read_progress"] = {
            "authority": "navigation_only",
            "source_freshness": "not_attested",
            "semantic_coverage": "not_attested",
            "columns": list(columns),
            "pages": [[item[key] for key in columns] for item in reads[-8:]],
        }
    return projected


def _summary_units(
    entries: tuple[dict[str, JsonValue], ...],
) -> tuple[tuple[dict[str, JsonValue], ...], ...]:
    units: list[list[dict[str, JsonValue]]] = []
    for entry in entries:
        if units and units[-1][0]["unit"] == entry["unit"]:
            units[-1].append(entry)
        else:
            units.append([entry])
    return tuple(tuple(unit) for unit in units)


def _full_reader_quote(messages, entries):
    content = messages[0]["content"]
    text = content[0]["text"] if isinstance(content, list) else content
    refs = {item["source_message_ref"] for item in json.loads(text)["transcript"]}
    return any(entry["ref"] in refs and _complete_source_entry(entry) for entry in entries)
