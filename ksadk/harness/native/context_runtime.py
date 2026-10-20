"""Host-owned request planning and compaction sealing; stable public entry points.

Projection, partition construction and immutable contracts live in focused
modules. The caller still owns model I/O, durable intents and Run budgets."""

from __future__ import annotations

import hashlib as hashlib
import json as json
from typing import Callable as Callable
from typing import Literal as Literal

from pydantic import AliasChoices as AliasChoices
from pydantic import BaseModel as BaseModel
from pydantic import ConfigDict as ConfigDict
from pydantic import Field as Field
from pydantic import JsonValue as JsonValue
from pydantic import model_serializer as model_serializer
from pydantic import model_validator as model_validator

from .completion import TaskContract as TaskContract
from .context import CompactionEnvelope as CompactionEnvelope
from .context import CompactionProjection as CompactionProjection
from .context import CompactionRejected as CompactionRejected
from .context import ContextBudget as ContextBudget
from .context import ContextMessage as ContextMessage
from .context import ExecutionContinuityState as ExecutionContinuityState
from .context import _tool_groups as _tool_groups
from .context import continuity_digest as continuity_digest
from .context import validate_compaction as validate_compaction
from .context_artifact_page import artifact_page_observation as artifact_page_observation
from .context_artifact_page import complete_artifact_call_ids as complete_artifact_call_ids
from .context_compaction_projection import seal_compaction_projection as seal_compaction_projection
from .context_fresh_tools import fresh_tool_group_refs
from .context_history import ContextRuntimeError as ContextRuntimeError
from .context_history import _content_blocks as _content_blocks
from .context_history import _goal_message_index
from .context_history import _historical_message as _historical_message
from .context_history import _json as _json
from .context_history import _unambiguous_json_object as _unambiguous_json_object
from .context_history import compact_historical_protocol as compact_historical_protocol
from .context_history import summary_media_blocks as summary_media_blocks
from .context_history import text_only_tool_message as text_only_tool_message
from .context_partition import BatchPartition as BatchPartition
from .context_partition import SummaryPartitionState as SummaryPartitionState
from .context_partition import acknowledge as acknowledge
from .context_partition import bind_pending as bind_pending
from .context_partition import expected_remaining_refs as expected_remaining_refs
from .context_partition import fragment_view as fragment_view
from .context_partition import new_coverage as new_coverage
from .context_partition import raw_batch_entries as raw_batch_entries
from .context_partition import require_complete as require_complete
from .context_partition import source_manifest as source_manifest
from .context_partition import validate_coverage as validate_coverage
from .context_pruning import project_fresh_workspace_reads as project_fresh_workspace_reads
from .context_pruning import project_historical_tool_result as project_historical_tool_result
from .context_pruning import project_workspace_read_result as project_workspace_read_result
from .context_request_projection import ContextRequestProjection
from .context_runtime_contracts import (
    READER_SUMMARY_POLICY_VERSION as READER_SUMMARY_POLICY_VERSION,
)
from .context_runtime_contracts import (
    CompactionHistory as CompactionHistory,
)
from .context_runtime_contracts import (
    CompactionPolicy as CompactionPolicy,
)
from .context_runtime_contracts import (
    CompactionProjectionOverflow as CompactionProjectionOverflow,
)
from .context_runtime_contracts import (
    CompactionRequest as CompactionRequest,
)
from .context_runtime_contracts import (
    CompactionResult as CompactionResult,
)
from .context_runtime_contracts import (
    ContextPlan as ContextPlan,
)
from .context_runtime_contracts import (
    Messages as Messages,
)
from .context_runtime_contracts import (
    PreparedContext as PreparedContext,
)
from .context_runtime_contracts import (
    _compacted_messages as _compacted_messages,
)
from .context_runtime_contracts import (
    _complete_source_entry as _complete_source_entry,
)
from .context_runtime_contracts import (
    _Contract as _Contract,
)
from .context_runtime_contracts import (
    _full_reader_quote as _full_reader_quote,
)
from .context_runtime_contracts import (
    _model_continuity as _model_continuity,
)
from .context_runtime_contracts import (
    _refs as _refs,
)
from .context_runtime_contracts import (
    _resume as _resume,
)
from .context_runtime_contracts import (
    _summary as _summary,
)
from .context_runtime_contracts import (
    _summary_continuation as _summary_continuation,
)
from .context_runtime_contracts import (
    _summary_units as _summary_units,
)
from .context_runtime_contracts import (
    source_context_digest as source_context_digest,
)
from .context_search import complete_search_call_ids as complete_search_call_ids
from .context_search import complete_search_observation as complete_search_observation
from .context_source_fragments import SourceTextPart as SourceTextPart
from .context_source_fragments import source_part_view as source_part_view
from .context_source_fragments import source_text_carrier as source_text_carrier
from .context_summary_planning import ContextSummaryPlanning
from .model import ModelProfile as ModelProfile
from .model import ModelReplayMetadata as ModelReplayMetadata
from .model_replay import replay_binding_failure as replay_binding_failure
from .model_replay import replay_fields as replay_fields
from .ports import ToolDefinition as ToolDefinition
from .ports import digest as digest
from .tool_observation import history_observation_message as history_observation_message
from .tool_observation import model_observation_message as model_observation_message
from .tool_observation import model_observation_messages as model_observation_messages
from .turn_control import ModelTurnControl as ModelTurnControl
from .user_document import user_document_indexes

_MAX_SUMMARY_CHUNKS = 8


class ContextPlanner(ContextRequestProjection, ContextSummaryPlanning):
    def __init__(
        self,
        *,
        count_tokens: Callable[[str], int],
        tokenizer_id: str,
        policy: CompactionPolicy | None = None,
        count_message_tokens: Callable[[dict[str, JsonValue]], int] | None = None,
        tool_media_projection_version: Literal["source-profile-tool-media/v1"] | None = None,
        summary_budget_unit: Literal["tokens", "estimated tokens", "UTF-8 bytes"] = "tokens",
        summary_budget_mode: Literal["strict", "context_bounded_estimate"] = "strict",
        summary_completion_policy: Literal["bounded-committed-summary/v1"] | None = None,
        compaction_message_projection: Literal["current-user-after-summary/v1"] | None = None,
        session_history_projection: Literal["sealed-session-summary/v1"] | None = None,
        summary_prompt_policy: Literal[
            "rolling-work-state/v1", "rolling-work-state/v2", "rolling-work-state/v3",
        ] | None = None,
        subagent_continuity_policy: Literal["accounted_child_runs/v1"] | None = None,
        source_progress_policy: Literal[
            "receipt-source-progress/v2", "receipt-source-progress/v3",
            "receipt-source-progress/v4", "receipt-source-progress/v5",
        ] | None = None,
        execution_budget_policy: Literal["prepared-execution-budget/v1"] | None = None,
        summary_output_policy: Literal["declared-summary-limit/v1"] | None = None,
        delivery_feedback_policy: Literal["admitted-delivery-continuation/v1"] | None = None,
        task_goal_projection_policy: Literal[
            "retained-user-goal/v1", "retained-user-goal/v2",
        ] | None = None,
        search_fragment_policy: Literal["complete-search-fragments/v1"] | None = None,
    ) -> None:
        if (
            not callable(count_tokens)
            or not isinstance(tokenizer_id, str)
            or not tokenizer_id.strip()
        ):
            raise ValueError("a host token counter and tokenizer_id are required")
        if count_message_tokens is not None and not callable(count_message_tokens):
            raise ValueError("message token counter must be callable")
        if summary_budget_unit not in {"tokens", "estimated tokens", "UTF-8 bytes"}:
            raise ValueError("unsupported summary budget unit")
        if summary_budget_mode not in {"strict", "context_bounded_estimate"} or (
            summary_budget_mode == "context_bounded_estimate"
            and summary_budget_unit != "estimated tokens"
        ):
            raise ValueError("context-bounded summary admission requires an estimated counter")
        if tool_media_projection_version not in {None, "source-profile-tool-media/v1"}:
            raise ValueError("unsupported tool media projection version")
        self.tool_media_projection_version = tool_media_projection_version
        self.count_tokens = count_tokens
        # Effective source projection versions the counter input, not only
        # its arithmetic. Existing history and ContextDriver binding inherit it.
        self.tokenizer_id = (
            f"{tokenizer_id}|{tool_media_projection_version}"
            if tool_media_projection_version
            else tokenizer_id
        )
        # tokenizer_id versions BOTH counters, including image cost/framing for
        # the pinned provider projection. The host owns that model-specific math.
        self.count_message_tokens = count_message_tokens
        self.summary_budget_unit = summary_budget_unit
        self.summary_budget_mode = summary_budget_mode
        if summary_completion_policy not in {None, "bounded-committed-summary/v1"}:
            raise ValueError("unsupported summary completion policy")
        self.summary_completion_policy = summary_completion_policy
        if compaction_message_projection not in {None, "current-user-after-summary/v1"}:
            raise ValueError("unsupported compaction message projection")
        self.compaction_message_projection = compaction_message_projection
        if session_history_projection not in {None, "sealed-session-summary/v1"} or (
            session_history_projection is not None
            and compaction_message_projection != "current-user-after-summary/v1"
        ):
            raise ValueError("session summary projection requires current-user projection")
        self.session_history_projection = session_history_projection
        if summary_prompt_policy not in {
            None, "rolling-work-state/v1", "rolling-work-state/v2", "rolling-work-state/v3",
        }:
            raise ValueError("unsupported summary prompt policy")
        self.summary_prompt_policy = summary_prompt_policy
        if subagent_continuity_policy not in {None, "accounted_child_runs/v1"}:
            raise ValueError("unsupported subagent continuity policy")
        self.subagent_continuity_policy = subagent_continuity_policy
        if source_progress_policy not in {
            None, "receipt-source-progress/v2", "receipt-source-progress/v3",
            "receipt-source-progress/v4", "receipt-source-progress/v5",
        }:
            raise ValueError("unsupported source progress policy")
        self.source_progress_policy = source_progress_policy
        if execution_budget_policy not in {None, "prepared-execution-budget/v1"}:
            raise ValueError("unsupported execution budget policy")
        self.execution_budget_policy = execution_budget_policy
        if summary_output_policy not in {None, "declared-summary-limit/v1"}:
            raise ValueError("unsupported summary output policy")
        self.summary_output_policy = summary_output_policy
        if delivery_feedback_policy not in {None, "admitted-delivery-continuation/v1"}:
            raise ValueError("unsupported delivery feedback policy")
        self.delivery_feedback_policy = delivery_feedback_policy
        if task_goal_projection_policy not in {
            None, "retained-user-goal/v1", "retained-user-goal/v2",
        }:
            raise ValueError("unsupported task goal projection policy")
        self.task_goal_projection_policy = task_goal_projection_policy
        if search_fragment_policy not in {None, "complete-search-fragments/v1"}:
            raise ValueError("unsupported search fragment policy")
        self.search_fragment_policy = search_fragment_policy
        self.policy = policy or CompactionPolicy()

    def plan(
        self,
        *,
        source_revision: int,
        model_turn: int,
        profile: ModelProfile,
        instructions: str,
        tools: tuple[ToolDefinition, ...],
        context_tools: tuple[ToolDefinition, ...] | None = None,
        messages: Messages,
        continuity: ExecutionContinuityState,
        contract: TaskContract,
        history: CompactionHistory | None = None,
        force_compaction: bool = False,
        turn_control: ModelTurnControl | None = None,
        allow_compaction: bool = True,
    ) -> ContextPlan:
        try:
            return self._plan(
                source_revision=source_revision,
                model_turn=model_turn,
                profile=profile,
                instructions=instructions,
                tools=tools,
                context_tools=tools if context_tools is None else context_tools,
                messages=messages,
                continuity=continuity,
                contract=contract,
                history=history or CompactionHistory(),
                force_compaction=force_compaction,
                turn_control=turn_control,
                allow_compaction=allow_compaction,
            )
        except ContextRuntimeError as exc:
            return ContextPlan(action="blocked", reason=str(exc))
        except (CompactionRejected, ValueError, KeyError, TypeError):
            return ContextPlan(action="blocked", reason="context_protocol_invalid")

    def _active(
        self,
        messages: Messages,
        continuity: ExecutionContinuityState,
        history: CompactionHistory,
    ) -> tuple[tuple[str, ...], Messages, str]:
        refs = _refs(messages)
        if history.has_projection:
            if (history.inherited_summary_source is not None
                    and history.inherited_summary_source.policy != self.session_history_projection):
                raise ContextRuntimeError("context_session_history_projection_changed")
            if history.compaction_message_projection != self.compaction_message_projection:
                raise ContextRuntimeError("context_compaction_message_projection_changed")
            if history.summary_prompt_policy != self.summary_prompt_policy:
                raise ContextRuntimeError("context_summary_prompt_policy_changed")
            if history.summary_completion_policy != self.summary_completion_policy:
                raise ContextRuntimeError("context_summary_completion_policy_changed")
            if history.tokenizer_id != self.tokenizer_id:
                raise ContextRuntimeError("context_tokenizer_changed")
            prefix = messages[: history.prefix_message_count]
            if (
                len(prefix) != history.prefix_message_count
                or source_context_digest(prefix) != history.prefix_digest
                or history.contract_digest != continuity.contract_digest
            ):
                raise ContextRuntimeError("context_history_changed")
            if (
                not history.summary_text.strip()
                or not set(history.retained_message_refs).issubset(
                    refs[: history.prefix_message_count]
                )
                or len(set(history.retained_message_refs)) != len(history.retained_message_refs)
            ):
                raise ContextRuntimeError("context_history_invalid")
            indexes = [
                i
                for i, ref in enumerate(refs)
                if i >= history.prefix_message_count or ref in history.retained_message_refs
            ]
        else:
            indexes = list(range(len(messages)))
        # A correction creates a new source receipt. Keep the old transcript
        # and compaction prefix immutable, but do not present superseded data
        # as the current goal's Context. Other history remains ordinary input.
        indexes = [
            i for i in indexes
            if messages[i].get("kind") != "context_contribution"
            or messages[i].get("goal_revision") == continuity.goal_revision
        ]
        indexes = [
            i for i in indexes
            if messages[i].get("kind") != "context_contribution"
            or messages[i].get("context_operation_key") is None
            or messages[i].get("context_operation_key") == continuity.context_source_operation_key
        ]
        active_refs = tuple(refs[i] for i in indexes)
        active = tuple(messages[i] for i in indexes)
        return active_refs, active, active_refs[_goal_message_index(active, continuity.goal)]

    def _plan(
        self,
        *,
        source_revision,
        model_turn,
        profile,
        instructions,
        tools,
        context_tools,
        messages,
        continuity,
        contract,
        history,
        force_compaction,
        turn_control,
        allow_compaction,
    ) -> ContextPlan:
        if turn_control is not None:
            turn_control.validate_tools(
                tuple(tool.name for tool in tools),
                stable_catalog=profile.tool_catalog_policy == "stable-task/v1",
            )
        required = tuple(item for item in contract.items if item.required)
        if continuity.contract_digest != digest(
            contract
        ) or continuity.required_acceptance_ids != tuple(item.acceptance_id for item in required):
            raise ContextRuntimeError("context_contract_mismatch")
        from .operator_guidance import instructions_are_journaled

        journaled = instructions_are_journaled(
            messages, profile, instructions, self._host_ledger(continuity, contract),
        )
        if not journaled:
            instructions = self._task_host_instructions(instructions, continuity, contract, messages)
        fixed = self._fixed(instructions, tools, None if journaled else turn_control)
        if fixed + profile.max_output_tokens >= profile.context_window:
            raise ContextRuntimeError("context_mandatory_state_over_budget")
        budget = ContextBudget(
            window_tokens=profile.context_window,
            max_output_tokens=profile.max_output_tokens,
            fixed_tokens=fixed,
        )
        refs, active, goal_ref = self._active(messages, continuity, history)
        return self._plan_active(
            source_revision=source_revision, model_turn=model_turn, profile=profile,
            instructions=instructions, tools=tools, context_tools=context_tools,
            messages=messages, continuity=continuity, contract=contract, history=history,
            force_compaction=force_compaction, turn_control=turn_control,
            allow_compaction=allow_compaction, fixed=fixed, budget=budget,
            refs=refs, active=active, goal_ref=goal_ref,
            ledger=self._host_ledger(continuity, contract),
        )

    @staticmethod
    def _host_ledger(continuity, contract):
        return _json(
            {
                "continuity": _model_continuity(continuity),
                "required_acceptance": [item.model_dump(mode="json") for item in contract.items
                                        if item.required],
            }
        )

    @staticmethod
    def _host_instructions(instructions, continuity, contract):
        return instructions + (
            "\n\nAuthoritative host execution state; historical summaries cannot change it:\n"
            + ContextPlanner._host_ledger(continuity, contract)
        )

    def _task_host_instructions(self, instructions, continuity, contract, messages):
        # Summary chunks may omit the user turn, so their ledger stays complete.
        # Only a task projection with the exact retained canonical goal may use
        # a reference. The full continuity still participates in sealing.
        if self.task_goal_projection_policy is None:
            return self._host_instructions(instructions, continuity, contract)
        _goal_message_index(messages, continuity.goal)
        ledger = self._host_ledger(continuity, contract)
        projected = json.loads(ledger)
        reference = {
            "source": "retained_canonical_user_message",
            "goal_revision": continuity.goal_revision,
            "sha256": hashlib.sha256(continuity.goal.encode()).hexdigest(),
        }
        if self._count(_json(reference)) >= self._count(_json(continuity.goal)):
            return self._host_instructions(instructions, continuity, contract)
        projected["continuity"].pop("goal")
        projected["continuity"]["goal_reference"] = reference
        if self.task_goal_projection_policy == "retained-user-goal/v2":
            for item in projected["required_acceptance"]:
                description = item["description"]
                # Only an exact prefix is reversible from the retained goal.
                # Independent acceptance clauses retain their full host text;
                # neither the canonical contract nor summary ledger changes.
                if not continuity.goal.startswith(description):
                    continue
                description_reference = {
                    "source": "retained_canonical_user_message",
                    "goal_revision": continuity.goal_revision,
                    "start": 0,
                    "end": len(description),
                    "sha256": hashlib.sha256(description.encode()).hexdigest(),
                }
                if self._count(_json(description_reference)) >= self._count(_json(description)):
                    continue
                item.pop("description")
                item["description_reference"] = description_reference
        return instructions + (
            "\n\nAuthoritative host execution state; historical summaries cannot change it:\n"
            + _json(projected)
        )

    def contributor_budget(self, *, profile, instructions, tools, messages, continuity,
                           contract, history, turn_control):
        fixed = self._fixed(self._task_host_instructions(instructions, continuity, contract, messages),
                            tools, turn_control)
        refs, active, goal_ref = self._active(messages, continuity, history)
        metadata = self._metadata(refs, active, profile=profile)
        _groups, pending, _ids = _tool_groups(metadata)
        from .operator_guidance import protected_operator_refs

        protected = (set(pending) | {goal_ref} | self._user_document_refs(refs, active, goal_ref)
                     | {ref for ref in protected_operator_refs(refs, active, _groups)
                        if active[refs.index(ref)].get("kind") != "context_contribution"})
        reserved = sum(item.token_count for item in metadata if item.message_id in protected)
        if history.has_projection:
            reserved += self._profile_count(_summary(history.summary_text), profile)
            if self.compaction_message_projection is None:
                reserved += self._profile_count(_resume(), profile)
        return max(0, profile.context_window - profile.max_output_tokens - fixed - reserved)

    @staticmethod
    def _user_document_refs(refs, active, goal_ref):
        try:
            return {refs[index] for index in user_document_indexes(active, refs.index(goal_ref))}
        except ValueError as exc:
            raise ContextRuntimeError("native_user_document_source_mismatch") from exc

    def _plan_active(self, *, source_revision, model_turn, profile, instructions, tools,
                     context_tools, messages, continuity, contract, history, force_compaction,
                     turn_control, allow_compaction, fixed, budget, refs, active, goal_ref, ledger):
        from .context_replay import compaction_replay_view
        from .replay_context import request_header_digest

        header = request_header_digest(instructions, tools, profile)

        if history.has_projection and (
            history.provider_replay_projection_policy != profile.provider_replay_projection_policy
        ):
            raise ContextRuntimeError("context_provider_replay_policy_changed")
        replay_view = compaction_replay_view(
            active, refs, covered_refs=history.retained_message_refs, profile=profile,
            root_prefix_digest=history.prefix_digest,
        )
        reader_names = frozenset(
            tool.name
            for tool in context_tools
            if tool.read_only and tool.context_role == "workspace_read"
        )
        search_names = frozenset(
            tool.name
            for tool in context_tools
            if tool.read_only and tool.context_role == "workspace_search"
        )
        artifact_reader_names = frozenset(
            tool.name for tool in context_tools
            if tool.read_only and tool.context_role == "artifact_read"
        )
        capture_reader_names = frozenset(
            tool.name for tool in context_tools
            if tool.name in reader_names and tool.result_archive_policy == "archived-model-view/v3"
        ) | artifact_reader_names
        metadata = self._metadata(refs, active, counted_messages=replay_view, profile=profile)
        groups, pending_refs, pending_ids = _tool_groups(metadata)
        fresh_group = fresh_tool_group_refs(messages)
        if not fresh_group.issubset(refs):
            # A prior summary may already cover the original trailing group.
            fresh_group = frozenset()
        if pending_ids != frozenset(continuity.pending_tool_call_ids):
            raise ContextRuntimeError("context_pending_tool_mismatch")
        summary_limit = min(self.policy.summary_max_tokens, profile.max_output_tokens)
        reserved_summary = self.summary_reservation(profile)
        from .operator_guidance import protected_operator_refs

        mandatory = (
            set(pending_refs) | {goal_ref} | protected_operator_refs(refs, active, groups)
            | self._user_document_refs(refs, active, goal_ref)
            | {ref for ref, raw in zip(refs, active)
               if raw.get("kind") == "context_contribution"
               and raw.get("goal_revision") == continuity.goal_revision}
            | {ref for ref, raw in zip(refs, active)
               if raw.get("kind") == "background_child_observation"
               and raw.get("child_run_id") in continuity.pending_subagent_refs}
        )
        hook_refs = {raw.get("hook_operation_key"): ref for ref, raw in zip(refs, active)
                     if raw.get("kind") == "hook_context"
                     and raw.get("hook_goal_revision") == continuity.goal_revision}
        guidance_refs = {raw.get("input_id"): ref for ref, raw in zip(refs, active)
                         if raw.get("role") == "user" and raw.get("kind") == "user_guidance"
                         and raw.get("input_goal_revision") == continuity.goal_revision}
        if not set(continuity.pending_guidance_ids).issubset(guidance_refs):
            raise ContextRuntimeError("context_pending_guidance_missing")
        mandatory |= {guidance_refs[key] for key in continuity.pending_guidance_ids}
        if not set(continuity.pending_hook_operation_keys).issubset(hook_refs):
            raise ContextRuntimeError("context_pending_hook_missing")
        mandatory |= {hook_refs[key] for key in continuity.pending_hook_operation_keys}
        protected_readers = frozenset()
        if groups and self.policy.read_working_set_count and not force_compaction:
            latest = groups[-1]
            definitions = {tool.name: tool for tool in context_tools}
            calls = [
                call
                for ref, raw in zip(refs, active)
                if ref in latest
                for call in raw.get("tool_calls", ())
            ]
            group_cost = sum(m.token_count for m in metadata if m.message_id in latest)
            if (
                group_cost <= self.policy.read_working_set_max_tokens
                and group_cost <= self.policy.read_working_set_total_max_tokens
                and sum(m.token_count for m in metadata if m.message_id in latest | mandatory)
                + reserved_summary
                <= budget.input_capacity
                and all(
                    (definition := definitions.get(call["function"]["name"])) is not None
                    and (definition.read_only or definition.context_role == "task_planning")
                    for call in calls
                )
            ):
                # Compaction should reduce earlier history before clipping a
                # latest, bounded complete source batch that already fits.
                # A provider-reported overflow deliberately disables this.
                protected_readers = frozenset(
                    call["id"] for call in calls if call["function"]["name"] in reader_names
                )
        read_view = self._read_view(replay_view, reader_names, protected_readers)
        if read_view != active:
            metadata = self._metadata(refs, active, counted_messages=read_view, profile=profile)
        visible = (
            _compacted_messages(
                history.summary_text, replay_view, continuity.goal,
                projection=self.compaction_message_projection,
            )
            if history.has_projection
            else replay_view
        )
        prepared = self._prepared(instructions, visible, fixed, turn_control,
                                  profile=profile, goal=continuity.goal,
                                  abandoned=history.abandoned_replay_digests,
                                  replay_header_digest=header)
        if (self.policy.enabled and self.policy.prune_tool_results and not force_compaction
                and prepared.counted_input_tokens - budget.fixed_tokens
                >= budget.input_capacity * self.policy.trigger_ratio):
            pruned = self._prune_old_results(
                prepared, refs=refs, active=replay_view, groups=groups, mandatory=mandatory,
                metadata=metadata, continuity=continuity, history=history, tools=context_tools,
                profile=profile, fixed=fixed, turn_control=turn_control,
            )
            if (pruned is not None and pruned.counted_input_tokens - budget.fixed_tokens
                    < budget.input_capacity * self.policy.trigger_ratio):
                return ContextPlan(action="ready", reason="historical_tool_results_within_budget",
                                   prepared=pruned)
        if not self.policy.enabled:
            if (force_compaction or prepared.counted_input_tokens + profile.max_output_tokens
                    > profile.context_window):
                raise ContextRuntimeError("context_compaction_disabled")
            return ContextPlan(
                action="ready", reason="context_compaction_disabled", prepared=prepared
            )
        if not allow_compaction:
            # A soft context target cannot spend a model call reserved for
            # delivery. The existing reader view is a deterministic projection,
            # not a summary call. Apply the same authorized view as ordinary
            # planning before refusing a complete next request. Call/result
            # groups, protected reads, search results and receipts stay intact.
            if (not force_compaction and self.policy.fresh_read_max_chars is not None
                    and prepared.counted_input_tokens + profile.max_output_tokens
                    > profile.context_window):
                observed = self._read_view(visible, reader_names, protected_readers)
                if observed != visible:
                    view = self._prepared(
                        instructions, observed, fixed, turn_control, profile=profile,
                        goal=continuity.goal, abandoned=history.abandoned_replay_digests,
                        replay_header_digest=header,
                    )
                    if view.counted_input_tokens + profile.max_output_tokens <= profile.context_window:
                        return ContextPlan(
                            action="ready", reason="finalization_reader_observations_within_budget",
                            prepared=view,
                        )
            if (force_compaction or prepared.counted_input_tokens + profile.max_output_tokens
                    > profile.context_window):
                raise ContextRuntimeError("context_finalization_budget_unavailable")
            return ContextPlan(
                action="ready", reason="context_finalization_reserved", prepared=prepared
            )
        fresh_search_ids = (
            complete_search_call_ids(
                [raw for ref, raw in zip(refs, active) if ref in groups[-1]],
                tool_names=search_names,
            )
            if groups
            else frozenset()
        )
        fresh_artifact_ids = (
            complete_artifact_call_ids(
                [raw for ref, raw in zip(refs, active) if ref in groups[-1]],
                tool_names=artifact_reader_names,
            ) if groups else frozenset()
        )
        if (
            not force_compaction
            and groups
            and (fresh_group or refs[-1] in groups[-1])
            and (
                fresh_group
                or fresh_search_ids
                or fresh_artifact_ids
                or any(
                    call["function"]["name"] in reader_names
                    for ref, raw in zip(refs, active)
                    if ref in groups[-1]
                    for call in raw.get("tool_calls", ())
                )
            )
            and prepared.counted_input_tokens + profile.max_output_tokens <= profile.context_window
        ):
            # The soft compaction target must not hide a just-returned source
            # batch that fits the locked complete request. Provider overflow
            # still takes the existing reduction path.
            return ContextPlan(
                action="ready",
                reason=(
                    "fresh_search_complete_request_fits"
                    if fresh_search_ids
                    else "fresh_artifact_complete_request_fits"
                    if fresh_artifact_ids
                    else "fresh_reader_complete_request_fits"
                    if any(
                        call["function"]["name"] in reader_names
                        for ref, raw in zip(refs, active) if ref in groups[-1]
                        for call in raw.get("tool_calls", ())
                    )
                    else "fresh_tool_complete_request_fits"
                ),
                prepared=prepared,
            )
        if (
            not force_compaction
            and prepared.counted_input_tokens - budget.fixed_tokens
            < budget.input_capacity * self.policy.trigger_ratio
        ):
            return ContextPlan(action="ready", reason="context_within_budget", prepared=prepared)
        if not force_compaction and self.policy.fresh_read_max_chars is not None:
            observed = self._read_view(visible, reader_names, protected_readers)
            if observed != visible:
                view = self._prepared(instructions, observed, fixed, turn_control,
                                      profile=profile, goal=continuity.goal,
                                      abandoned=history.abandoned_replay_digests,
                                      replay_header_digest=header)
                if view.counted_input_tokens + profile.max_output_tokens <= profile.context_window:
                    return ContextPlan(
                        action="ready",
                        reason="fresh_reader_observations_within_budget",
                        prepared=view,
                    )
                # Growing history must not progressively starve the newest
                # source observation. Reduce old history through compaction
                # rather than shrinking all fresh windows down to 128 chars.
        # The host's summary ceiling and the provider's output-token cap are
        # different units. Reasoning tokens can consume the provider cap before
        # any visible summary appears; enforce the host ceiling only when the
        # visible summary is sealed. Only the actual hard request window may
        # reduce a complete Reader quote's provider output cap.
        mandatory_cost = sum(m.token_count for m in metadata if m.message_id in mandatory)
        if mandatory_cost + reserved_summary > budget.input_capacity:
            summary_frames = reserved_summary - summary_limit
            if (self.summary_budget_mode != "context_bounded_estimate"
                    or mandatory_cost + summary_frames >= budget.input_capacity):
                raise ContextRuntimeError("context_mandatory_state_over_budget")
            # An estimated summary target is a ceiling, not a minimum
            # allocation. Permit reducing history when a smaller nonempty
            # summary can fit beside all mandatory state. Keep the declared
            # summary/provider output ceilings; sealing must still admit the
            # actual complete projection under the original task output cap.
            reserved_summary = budget.input_capacity - mandatory_cost
        # A provider-limited summary may exceed an estimated host token ceiling.
        # Keeping every active message also keeps that existing summary. Account
        # for its actual projection when choosing the retained working set, so
        # a fresh complete tool batch can be summarized instead of being kept
        # beside an oversized summary and misclassified as mandatory state.
        retention_summary = reserved_summary
        if history.has_projection:
            retention_summary = max(
                retention_summary,
                self._profile_count(_summary(history.summary_text), profile)
                + (self._profile_count(_resume(), profile)
                   if self.compaction_message_projection is None else 0),
            )
        retained = self._retain(
            metadata,
            groups,
            pending_refs,
            mandatory,
            budget,
            retention_summary,
            active=active,
            continuity=continuity,
            history=history,
            tools=context_tools,
            force_compaction=force_compaction,
            fresh_group=fresh_group,
        )
        dropped = tuple(raw for ref, raw in zip(refs, active) if ref not in retained)
        if not dropped:
            if force_compaction:
                raise ContextRuntimeError("context_overflow_no_reducible_history")
            if prepared.counted_input_tokens + profile.max_output_tokens > profile.context_window:
                raise ContextRuntimeError("context_mandatory_state_over_budget")
            return ContextPlan(
                action="ready", reason="context_no_reducible_history", prepared=prepared
            )
        if (
            self.policy.max_compactions is not None
            and history.total_compactions >= self.policy.max_compactions
        ):
            raise ContextRuntimeError("context_compaction_limit")
        observed_progress = history.count > 0 and (
            continuity.observed_progress_revision > history.last_observed_progress_revision
            or (
                continuity.workspace_revision is not None
                and continuity.workspace_revision != history.last_workspace_revision
            )
        )
        if (
            history.count
            and not observed_progress
            and history.stagnant_count >= self.policy.max_compactions_without_progress
        ):
            raise ContextRuntimeError("context_compaction_no_observed_progress")
        if (
            history.total_compactions
            and model_turn - history.last_model_turn < self.policy.min_model_turns_between
        ):
            raise ContextRuntimeError("context_rapid_refill")
        result = self._request(
            source_revision,
            model_turn,
            profile,
            instructions,
            messages,
            continuity,
            history,
            metadata,
            refs,
            active,
            retained,
            dropped,
            budget,
            summary_limit,
            ledger,
            turn_control,
            reader_names,
            protected_readers,
            search_names=search_names,
            capture_reader_names=capture_reader_names,
            artifact_reader_names=artifact_reader_names,
            replay_header_digest=header,
            fresh_group=fresh_group,
        )
        if (
            result.request is not None and not force_compaction
            and self.summary_budget_mode == "context_bounded_estimate"
            and self.summary_completion_policy == "bounded-committed-summary/v1"
        ):
            candidate = self._post_summary_read_candidate(
                metadata, active, groups, retained, fresh_group,
                continuity=continuity, history=history, tools=context_tools,
            )
            if candidate:
                result = result.model_copy(update={"request": result.request.model_copy(
                    update={"post_summary_read_refs": candidate},
                )})
        return result


from .compaction_protocol import CompactionProtocol


class CompactionController(CompactionProtocol):
    def __init__(self, planner: ContextPlanner) -> None:
        self.planner = planner


    def advance(
        self,
        request: CompactionRequest,
        summary_text: str,
        *,
        current_revision: int,
        current_continuity: ExecutionContinuityState,
        current_messages: Messages,
    ) -> CompactionRequest | CompactionResult:
        self._validate_source(request, current_revision, current_continuity, current_messages)
        summary_text = "".join((*request.summary_fragments, summary_text))
        self._validate_summary(request, summary_text)
        request = self._acknowledge_partition(request, current_messages)
        if not request.remaining_history_entries:
            return self.seal(
                request,
                summary_text,
                current_revision=current_revision,
                current_continuity=current_continuity,
                current_messages=current_messages,
            )
        chunk_limit = _MAX_SUMMARY_CHUNKS
        if request.summary_partition is not None:
            manifest, raw = source_manifest(
                current_messages, request.old_messages, request.retained_message_refs,
                capture_reader_names=request.source_capture_reader_names,
                search_fragment_policy=request.search_fragment_policy,
                capture_search_names=request.source_capture_search_names,
                fresh_tool_observation_policy=request.fresh_tool_observation_policy,
                fresh_tool_message_refs=request.fresh_tool_message_refs,
                fresh_text_fragment_policy=request.fresh_text_fragment_policy,
                fresh_text_excluded_tool_names=request.source_reader_names,
            )
            # Each acknowledged partition consumes validated original refs.
            # A long closed history needs more than eight chunks even when
            # its tools have no reader role. Bound by the finite sealed source;
            # keep the ordinary Run model budget and partition checks intact.
            chunk_limit = max(chunk_limit, len(manifest["dropped"]))
            if any(
                call["function"]["name"] in request.source_reader_names
                for partition in request.summary_partition.partitions
                for call in raw[partition.assistant_ref].get("tool_calls", ())
            ) or (
                request.fresh_text_fragment_policy is not None
                and any(
                    ref in manifest.get("source_captures", {})
                    for partition in request.summary_partition.partitions
                    for ref in manifest["batches"][partition.assistant_ref]["receipt_refs"]
                )
            ):
                # A validated quote consumes an original ref or advances a
                # captured text cursor. Both are finite; every request still
                # consumes the unchanged Run model budget.
                chunk_limit = max(chunk_limit, len(manifest["dropped"]) + sum(
                    capture["total_chars"]
                    for capture in manifest.get("source_captures", {}).values()
                ))
        if request.chunk_index < 1 or request.chunk_index >= chunk_limit:
            raise ContextRuntimeError("compaction_chunk_limit")
        partition_state = request.summary_partition
        if partition_state is None:
            projected, counted, remaining = self.planner._next_chunk(
                request.remaining_history_entries,
                summary_text,
                request.summary_instructions,
                request.profile,
                dict(zip(_refs(current_messages), current_messages)),
            )
        else:
            manifest, source_messages = source_manifest(
                current_messages, request.old_messages, request.retained_message_refs,
                capture_reader_names=request.source_capture_reader_names,
                search_fragment_policy=request.search_fragment_policy,
                capture_search_names=request.source_capture_search_names,
                fresh_tool_observation_policy=request.fresh_tool_observation_policy,
                fresh_tool_message_refs=request.fresh_tool_message_refs,
                fresh_text_fragment_policy=request.fresh_text_fragment_policy,
                fresh_text_excluded_tool_names=request.source_reader_names,
            )
            projected, counted, remaining, partition_state = self.planner._next_covered_chunk(
                request.remaining_history_entries,
                summary_text,
                request.summary_instructions,
                request.profile,
                source_messages,
                partition_state,
                manifest,
                request.chunk_index + 1,
            )
        full_reader_quote = _full_reader_quote(projected, request.remaining_history_entries)
        output_cap = self.planner._summary_output_cap(
            counted, request.profile, full_reader_quote=full_reader_quote
        )
        next_request = CompactionRequest.model_validate(
            {
                **request.model_dump(mode="json"),
                "summary_messages": projected,
                "summary_counted_input_tokens": counted,
                "remaining_history_entries": remaining,
                "chunk_index": request.chunk_index + 1,
                "summary_partition": partition_state,
                "full_reader_quote": full_reader_quote,
                "summary_output_tokens": output_cap,
                "summary_fragments": (),
                "summary_continuations": 0,
            }
        )
        if next_request.fresh_tool_observation_policy is not None:
            # Verify queued source before its next dispatch, not only after
            # the provider responds. Rehashed, shortened remaining entries
            # must never become an authorized fresh observation request.
            self._validate_source(
                next_request, current_revision, current_continuity, current_messages
            )
        return next_request
