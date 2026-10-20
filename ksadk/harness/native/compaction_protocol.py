"""Validate, continue and seal compaction receipts against their immutable source."""

from __future__ import annotations

import json as json

from .context import ExecutionContinuityState as ExecutionContinuityState
from .context import _tool_groups as _tool_groups
from .context_compaction_projection import seal_compaction_projection
from .context_fresh_tools import fresh_tool_group_refs
from .context_history import ContextRuntimeError as ContextRuntimeError
from .context_partition import acknowledge as acknowledge
from .context_partition import require_complete as require_complete
from .context_partition import source_manifest as source_manifest
from .context_partition import validate_coverage as validate_coverage
from .context_runtime_contracts import (
    READER_SUMMARY_POLICY_VERSION as READER_SUMMARY_POLICY_VERSION,
)
from .context_runtime_contracts import CompactionRequest as CompactionRequest
from .context_runtime_contracts import CompactionResult as CompactionResult
from .context_runtime_contracts import Messages as Messages
from .context_runtime_contracts import _complete_source_entry as _complete_source_entry
from .context_runtime_contracts import _summary_continuation as _summary_continuation
from .context_runtime_contracts import source_context_digest as source_context_digest
from .model import ModelReplayMetadata as ModelReplayMetadata
from .model_replay import replay_binding_failure as replay_binding_failure
from .model_replay import replay_fields as replay_fields


class CompactionProtocol:
    """Validate, continue and seal compaction receipts against their immutable source."""

    def continue_summary(
        self,
        request: CompactionRequest,
        fragment: str,
        *,
        current_revision: int,
        current_continuity: ExecutionContinuityState,
        current_messages: Messages,
        provider_replay: ModelReplayMetadata | None = None,
    ) -> CompactionRequest:
        self._validate_source(request, current_revision, current_continuity, current_messages)
        if failure := replay_binding_failure(request.profile, provider_replay, required=True):
            raise ContextRuntimeError(failure)
        if request.summary_continuations >= request.profile.max_continuations:
            raise ContextRuntimeError("compaction_summary_continuation_exhausted")
        fragments = (*request.summary_fragments, fragment)
        self._validate_summary(request, "".join(fragments))
        messages = (
            *request.summary_messages,
            {"role": "assistant", "content": fragment, **replay_fields(provider_replay)},
            _summary_continuation(),
        )
        counted = self.planner._fixed(request.summary_instructions, ()) + sum(
            self.planner._count_message(message)
            for message in self.planner._media_view(messages, request.profile, purpose="compaction")
        )
        output_limit = (
            min(request.summary_output_tokens, request.profile.context_window - counted)
            if request.full_reader_quote
            else request.summary_output_tokens
        )
        if output_limit <= 0 or counted + output_limit > request.profile.context_window:
            raise ContextRuntimeError("context_summary_continuation_over_budget")
        return CompactionRequest.model_validate(
            {
                **request.model_dump(mode="json"),
                "summary_messages": messages,
                "summary_counted_input_tokens": counted,
                "summary_output_tokens": output_limit,
                "summary_fragments": fragments,
                "summary_continuations": request.summary_continuations + 1,
            }
        )

    def _is_partition_quote(self, message):
        if message.get("kind") == "summary_batch_fragment":
            return True
        content = message.get("content")
        text = content[0].get("text") if isinstance(content, list) and content else content
        try:
            body = json.loads(text)
        except (ValueError, TypeError):
            return False
        # Only the host's outer quote envelope; never scan nested tool text.
        return isinstance(body, dict) and body.get("fragment") is not None

    def _base_summary_messages(self, request):
        return (
            request.summary_messages[: -2 * request.summary_continuations]
            if request.summary_continuations
            else request.summary_messages
        )

    def _acknowledge_partition(self, request, messages):
        state = request.summary_partition
        if state is None or state.pending is None:
            return request
        manifest, raw = source_manifest(
            messages,
            request.old_messages,
            request.retained_message_refs,
            capture_reader_names=request.source_capture_reader_names,
            search_fragment_policy=request.search_fragment_policy,
            capture_search_names=request.source_capture_search_names,
            fresh_tool_observation_policy=request.fresh_tool_observation_policy,
            fresh_tool_message_refs=request.fresh_tool_message_refs,
            fresh_text_fragment_policy=request.fresh_text_fragment_policy,
            fresh_text_excluded_tool_names=request.source_reader_names,
        )
        state = acknowledge(
            state,
            manifest,
            raw,
            self._base_summary_messages(request),
            request.chunk_index,
            profile=request.profile if self.planner.tool_media_projection_version else None,
        )
        return request.model_copy(update={"summary_partition": state})

    def _validate_source(
        self,
        request: CompactionRequest,
        current_revision: int,
        current_continuity: ExecutionContinuityState,
        current_messages: Messages,
    ) -> None:
        if current_continuity != request.continuity:
            raise ContextRuntimeError("context_continuity_changed")
        if (
            type(current_revision) is not int
            or current_revision < request.source_revision
            or source_context_digest(current_messages) != request.source_context_digest
        ):
            raise ContextRuntimeError("context_source_changed")
        if request.tool_media_projection_version != self.planner.tool_media_projection_version:
            raise ContextRuntimeError("context_media_projection_changed")
        if request.tokenizer_id != self.planner.tokenizer_id:
            raise ContextRuntimeError("context_tokenizer_changed")
        if (request.summary_budget_mode or "strict") != self.planner.summary_budget_mode:
            raise ContextRuntimeError("context_summary_policy_changed")
        if request.summary_completion_policy != self.planner.summary_completion_policy:
            raise ContextRuntimeError("context_summary_completion_policy_changed")
        if request.compaction_message_projection != self.planner.compaction_message_projection:
            raise ContextRuntimeError("context_compaction_message_projection_changed")
        if request.search_fragment_policy != self.planner.search_fragment_policy:
            raise ContextRuntimeError("context_search_fragment_policy_changed")
        if request.summary_prompt_policy != self.planner.summary_prompt_policy:
            raise ContextRuntimeError("context_summary_prompt_policy_changed")
        if request.post_summary_read_refs:
            groups, _, _ = _tool_groups(request.old_messages)
            candidate = frozenset(request.post_summary_read_refs)
            cost = sum(m.token_count for m in request.old_messages if m.message_id in candidate)
            if (
                request.summary_budget_mode != "context_bounded_estimate"
                or request.summary_completion_policy != "bounded-committed-summary/v1"
                or not self.planner.policy.read_working_set_count
                or not groups
                or candidate != groups[-1]
                or candidate != fresh_tool_group_refs(current_messages)
                or len(candidate) != len(request.post_summary_read_refs)
                or any(group.intersection(request.retained_message_refs) for group in groups)
                or cost > self.planner.policy.read_working_set_max_tokens
                or cost > self.planner.policy.read_working_set_total_max_tokens
            ):
                raise ContextRuntimeError("context_post_summary_read_candidate_changed")
        if self.planner.summary_prompt_policy in {
            "rolling-work-state/v2",
            "rolling-work-state/v3",
        } and request.summary_messages[1:2] != self.planner._summary_directive(request.profile):
            raise ContextRuntimeError("context_summary_output_directive_changed")
        if request.fresh_tool_observation_policy is not None and (
            request.summary_partition is None
            or request.summary_partition_version != "quoted-history-coverage/v1"
        ):
            raise ContextRuntimeError("compaction_partition_state_missing")
        if (
            request.source_reader_names
            or request.full_reader_quote
            or any(_complete_source_entry(e) for e in request.remaining_history_entries)
        ) and request.reader_summary_policy_version != READER_SUMMARY_POLICY_VERSION:
            raise ContextRuntimeError("context_reader_policy_changed")
        if request.summary_partition is None:
            if any(
                self._is_partition_quote(message)
                for message in self._base_summary_messages(request)
            ):
                raise ContextRuntimeError("compaction_partition_state_missing")
        else:
            manifest, raw = source_manifest(
                current_messages,
                request.old_messages,
                request.retained_message_refs,
                capture_reader_names=request.source_capture_reader_names,
                search_fragment_policy=request.search_fragment_policy,
                capture_search_names=request.source_capture_search_names,
                fresh_tool_observation_policy=request.fresh_tool_observation_policy,
                fresh_tool_message_refs=request.fresh_tool_message_refs,
                fresh_text_fragment_policy=request.fresh_text_fragment_policy,
                fresh_text_excluded_tool_names=request.source_reader_names,
            )
            state = request.summary_partition
            validate_coverage(
                state,
                manifest,
                messages=self._base_summary_messages(request) if state.pending else None,
                chunk_index=request.chunk_index,
                raw=raw,
                profile=request.profile if self.planner.tool_media_projection_version else None,
            )
            if state.pending is None:
                require_complete(state, manifest)

    def _validate_summary(self, request: CompactionRequest, summary_text: str) -> None:
        if not isinstance(summary_text, str) or not summary_text.strip():
            raise ContextRuntimeError("context_summary_empty")
        # Exact counters retain their strict summary-size contract. A declared
        # host estimate is a reduction target, not provider output usage: the
        # same provider-limited response can exceed that estimate slightly.
        # Transient text is bounded here; continuation and sealing separately
        # admit the complete projected request against the original window.
        limit = (
            request.budget.input_capacity
            if request.summary_budget_mode == "context_bounded_estimate"
            else request.summary_max_tokens
        )
        if self.planner._count(summary_text) > limit:
            raise ContextRuntimeError("context_summary_budget_exceeded")

    def seal(
        self,
        request: CompactionRequest,
        summary_text: str,
        *,
        current_revision: int,
        current_continuity: ExecutionContinuityState,
        current_messages: Messages,
    ) -> CompactionResult:
        self._validate_source(request, current_revision, current_continuity, current_messages)
        self._validate_summary(request, summary_text)
        if request.remaining_history_entries:
            raise ContextRuntimeError("compaction_chunks_incomplete")
        request = self._acknowledge_partition(request, current_messages)
        if request.summary_partition is not None:
            manifest, _ = source_manifest(
                current_messages,
                request.old_messages,
                request.retained_message_refs,
                capture_reader_names=request.source_capture_reader_names,
                search_fragment_policy=request.search_fragment_policy,
                capture_search_names=request.source_capture_search_names,
                fresh_tool_observation_policy=request.fresh_tool_observation_policy,
                fresh_tool_message_refs=request.fresh_tool_message_refs,
                fresh_text_fragment_policy=request.fresh_text_fragment_policy,
                fresh_text_excluded_tool_names=request.source_reader_names,
            )
            require_complete(request.summary_partition, manifest)
        return self._seal_projection(request, summary_text, current_continuity, current_messages)

    def _seal_projection(self, request, summary_text, current_continuity, current_messages):
        return seal_compaction_projection(
            self.planner, request, summary_text, current_continuity, current_messages
        )
