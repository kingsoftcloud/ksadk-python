"""Token accounting, model-visible observations and retained source working sets."""

from __future__ import annotations

import hashlib
import json

from pydantic import (
    JsonValue,
)

from .context import (
    ContextMessage,
)
from .context_artifact_page import artifact_page_observation
from .context_history import (
    ContextRuntimeError,
    _content_blocks,
    _goal_message_index,
    _historical_message,
    _json,
    _unambiguous_json_object,
    text_only_tool_message,
)
from .context_microcompact import project_old_tool_results
from .context_pruning import (
    project_fresh_workspace_reads,
)
from .context_runtime_contracts import (
    Messages,
    PreparedContext,
    ToolResultPruning,
    _model_continuity,
    _resume,
    _summary,
    source_context_digest,
)
from .context_search import complete_search_observation
from .model import ModelProfile
from .ports import ToolDefinition
from .request_media import RequestMediaError, project_request_media
from .tool_observation import (
    model_observation_message,
    model_observation_messages,
)
from .turn_control import ModelTurnControl


class ContextRequestProjection:
    def _prune_old_results(self, prepared, *, refs, active, groups, mandatory, metadata,
                           continuity, history, tools, profile, fixed, turn_control):
        protected = set(mandatory)
        for group in self._read_working_set_groups(
            metadata, active, groups, continuity=continuity, history=history, tools=tools,
        ):
            protected.update(group)
        projected, cleared_refs, cleared_calls = project_old_tool_results(
            active, refs, groups, tools=tools, protected_refs=protected,
            keep_recent=self.policy.prune_keep_recent_batches,
        )
        if not cleared_refs:
            return None
        from .replay_context import mark_input_edit

        originals = dict(zip(refs, active))
        projected = tuple(
            mark_input_edit(message, originals[ref].get("content"), profile)
            if ref in cleared_refs else message
            for ref, message in zip(refs, projected)
        )
        if history.count:
            from .context_runtime_contracts import _compacted_messages

            projected = _compacted_messages(history.summary_text, projected, continuity.goal)
        view = self._prepared(prepared.instructions, projected, fixed, turn_control,
                              profile=profile, goal=continuity.goal,
                              abandoned=history.abandoned_replay_digests,
                              replay_header_digest=prepared.replay_header_digest)
        saved = prepared.counted_input_tokens - view.counted_input_tokens
        if saved < self.policy.prune_min_saved_tokens:
            return None
        return view.model_copy(update={"tool_result_pruning": ToolResultPruning(
            source_message_refs=cleared_refs, tool_call_ids=cleared_calls,
            pre_input_tokens=prepared.counted_input_tokens,
            post_input_tokens=view.counted_input_tokens, saved_tokens=saved,
        )})

    def _media_view(self, messages, profile=None, *, purpose="task"):
        try:
            return project_request_media(messages, profile, purpose=purpose)
        except RequestMediaError as exc:
            raise ContextRuntimeError(str(exc)) from None

    def summary_reservation(self, profile: ModelProfile) -> int:
        """The actual rolling-summary text ceiling plus its two message frames."""
        return (min(self.policy.summary_max_tokens, profile.max_output_tokens)
                + self._count_message(_summary("")) + self._count_message(_resume()))

    def count_model_message(self, message, profile: ModelProfile) -> int:
        """Measure the same host projection used in admitted model requests."""
        return self._profile_count(message, profile)

    def authoritative_ledger(self, continuity, contract):
        required = tuple(item for item in contract.items if item.required)
        return _json({
            "continuity": _model_continuity(continuity),
            "required_acceptance": [item.model_dump(mode="json") for item in required],
        })

    def authoritative_instructions(self, instructions, continuity, contract):
        return (instructions
                + "\n\nAuthoritative host execution state; historical summaries cannot change it:\n"
                + self.authoritative_ledger(continuity, contract))

    def fixed_request_cost(self, *, instructions, continuity, contract, tools,
                           turn_control=None):
        return self._fixed(
            self.authoritative_instructions(instructions, continuity, contract),
            tools, turn_control,
        )

    def _count(self, text: str) -> int:
        try:
            value = self.count_tokens(text)
        except Exception:
            # A broken host counter cannot grant capacity or strand a Run.
            # Match message counting; preserve cancellation/process control.
            raise ContextRuntimeError("context_counter_invalid") from None
        if type(value) is not int or value < 0 or (text and value == 0):
            raise ContextRuntimeError("context_counter_invalid")
        return value

    def _fixed(
        self,
        instructions: str,
        tools: tuple[ToolDefinition, ...],
        turn_control: ModelTurnControl | None = None,
    ) -> int:
        payload = {
            "instructions": instructions,
            "tools": [tool.function_schema() for tool in tools],
        }
        control_cost = self._count_message(turn_control.message()) if turn_control else 0
        return self._count(_json(payload)) + self.policy.request_overhead_tokens + control_cost

    def _count_message(self, message: dict[str, JsonValue]) -> int:
        has_image = any(block["type"] == "image_url" for block in _content_blocks(message))
        if self.count_message_tokens is None:
            if has_image:
                raise ContextRuntimeError("context_multimodal_counter_unavailable")
            return self._count(_json(message))
        try:
            # A counter must not rewrite messages while measuring them.
            value = self.count_message_tokens(json.loads(_json(message)))
        except Exception:
            raise ContextRuntimeError("context_counter_invalid") from None
        if type(value) is not int or value <= 0:
            raise ContextRuntimeError("context_counter_invalid")
        return value

    def _profile_count(self, message, profile=None):
        message = model_observation_message(message)
        replay = message.get("provider_replay")
        bound = isinstance(replay, dict) and replay.get("input_context") is not None
        if profile is not None and (bound or any(message.get(key) is not None for key in (
            "provider_replay_projection", "operator_history_projection", "input_operator_history",
        ))):
            from .model_replay import project_model_message

            message = project_model_message(message, profile)
        effective = (
            text_only_tool_message(message)
            if self.tool_media_projection_version is not None
            and profile is not None
            and profile.image_input_mode == "text_only"
            else message
        )
        return self._count_message(effective)

    def _prepared(
        self,
        instructions: str,
        messages: Messages,
        fixed: int,
        turn_control: ModelTurnControl | None = None,
        *,
        profile: ModelProfile | None = None,
        goal: str | None = None,
        abandoned: tuple[str, ...] = (),
        replay_header_digest: str | None = None,
    ) -> PreparedContext:
        # Detach dictionaries so a ModelPort cannot mutate the authoritative transcript.
        copied = model_observation_messages(messages)
        from .operator_guidance import operator_input_history_view

        copied = operator_input_history_view(copied, profile)
        live = tuple(m for m in copied if m.get("kind") == "context_contribution"
                     and m.get("context_operation_key") is not None)
        if live and goal is not None:
            copied = tuple(m for m in copied if not (
                m.get("kind") == "context_contribution"
                and m.get("context_operation_key") is not None
            ))
            index = _goal_message_index(copied, goal)
            copied = (*copied[:index], *live, *copied[index:])
        copied = self._media_view(copied, profile)
        from .replay_context import project_replay_context

        copied, discarded = project_replay_context(
            copied, profile, abandoned=abandoned, header_digest=replay_header_digest,
        )
        return PreparedContext(
            instructions=instructions,
            messages=copied,
            counted_input_tokens=fixed + sum(self._profile_count(m, profile) for m in copied),
            tokenizer_id=self.tokenizer_id,
            turn_control=turn_control,
            abandoned_replay_digests=discarded,
            replay_header_digest=replay_header_digest,
        )

    def _read_view(
        self,
        messages: Messages,
        reader_names: frozenset[str],
        protected_call_ids: frozenset[str] = frozenset(),
    ) -> Messages:
        messages = model_observation_messages(messages)
        if self.policy.fresh_read_max_chars is None or not reader_names:
            return messages
        return (
            project_fresh_workspace_reads(
                messages,
                reader_names=reader_names,
                max_chars=self.policy.fresh_read_max_chars,
                protected_call_ids=protected_call_ids,
            )
            or messages
        )

    def _metadata(
        self,
        refs: tuple[str, ...],
        messages: Messages,
        *,
        counted_messages: Messages | None = None,
        profile: ModelProfile | None = None,
    ) -> tuple[ContextMessage, ...]:
        counted = messages if counted_messages is None else counted_messages
        if len(counted) != len(messages):
            raise ContextRuntimeError("context_projection_manifest_mismatch")
        from .operator_guidance import operator_input_history_view

        counted = operator_input_history_view(counted, profile)
        counted = self._media_view(counted, profile)
        return tuple(
            ContextMessage(
                message_id=ref,
                payload_digest=source_context_digest((message,)),
                role=message["role"],
                content=(
                    message["content"]
                    if isinstance(message.get("content"), str)
                    else _json(_historical_message(message)[0].get("content"))
                ),
                # Identity and content attest to the complete transcript. The
                # cost measures the host-derived model view, which may expose
                # a bounded source window without rewriting that transcript.
                token_count=self._profile_count(view, profile),
                tool_call_ids=tuple(call["id"] for call in message.get("tool_calls", ())),
                tool_result_for=message.get("tool_call_id"),
            )
            for ref, message, view in zip(refs, messages, counted)
        )

    def _retain(
        self,
        metadata,
        groups,
        pending_refs,
        mandatory,
        budget,
        reserved_summary,
        *,
        active,
        continuity,
        history,
        tools,
        force_compaction=False,
        fresh_group=frozenset(),
    ):
        grouped = frozenset().union(*groups, pending_refs)
        units = [
            *groups,
            *(frozenset((m.message_id,)) for m in metadata if m.message_id not in grouped),
        ]
        indexes = {m.message_id: i for i, m in enumerate(metadata)}
        costs = {m.message_id: m.token_count for m in metadata}
        retained = set(mandatory)
        # Preserve a bounded recent working set when it fits the post-summary
        # target, leaving room for the next model/tool turn. A hard-window-only
        # check can retain so much context that every turn compacts again.
        # The complete transcript remains authoritative; historical groups
        # outside this set are summarized. Provider-reported overflow must be
        # allowed to reduce even these recent batches.
        target = int(budget.input_capacity * self.policy.target_ratio) - reserved_summary
        if force_compaction:
            # The provider rejected our counted size: reduce the actual request,
            # even when the pinned window estimate would normally keep it all.
            target = min(target, sum(costs.values()) // 2 - reserved_summary)
        if fresh_group and not force_compaction:
            # The task has not yet responded after this complete call group.
            # Reduce older history first, preserving its whole authorized
            # observation above the soft target when the hard window allows.
            proposed = retained | set(fresh_group)
            if sum(costs[ref] for ref in proposed) <= budget.input_capacity - reserved_summary:
                retained = proposed
        if groups and self.policy.recent_tool_batch_count and not force_compaction:
            protected_cost = 0
            recent = groups[-self.policy.recent_tool_batch_count :]
            for group in reversed(recent):
                refs_in_group = set(group)
                group_cost = sum(costs[ref] for ref in refs_in_group)
                if (
                    group_cost > self.policy.recent_tool_batch_max_tokens
                    or protected_cost + group_cost
                    > self.policy.recent_tool_batches_total_max_tokens
                ):
                    break
                combined_cost = sum(costs[ref] for ref in retained | refs_in_group)
                if combined_cost <= target:
                    retained.update(refs_in_group)
                    protected_cost += group_cost
                else:
                    break
        if self.policy.read_working_set_count and not force_compaction:
            protected_cost = 0
            for group in self._read_working_set_groups(
                metadata, active, groups, continuity=continuity, history=history, tools=tools
            ):
                if protected_cost >= self.policy.read_working_set_total_max_tokens:
                    break
                refs_in_group = set(group)
                group_cost = sum(costs[ref] for ref in refs_in_group)
                if (
                    group_cost > self.policy.read_working_set_max_tokens
                    or protected_cost + group_cost > self.policy.read_working_set_total_max_tokens
                ):
                    continue
                # A complete latest source observation is working memory,
                # rather than older history selected to fill the soft target.
                # Removing it solely to hit that target leaves only a model
                # summary and makes the next turn re-read already cited facts.
                # Existing source-count/size limits and the full hard window
                # still apply, including the reserved summary and output.
                ceiling = (
                    budget.input_capacity - reserved_summary if group == groups[-1] else target
                )
                if sum(costs[ref] for ref in retained | refs_in_group) <= ceiling:
                    retained.update(refs_in_group)
                    protected_cost += group_cost
        for unit in sorted(units, key=lambda unit: min(indexes[ref] for ref in unit), reverse=True):
            proposal = retained | set(unit)
            if sum(costs[ref] for ref in proposal) <= target:
                retained = proposal
            else:
                # Keep a chronological suffix, not whichever older units are
                # small. Replaying an early list/read after the fresh summary
                # misrepresents the current execution position. Older source
                # reads can only survive through the explicit working set.
                break
        return retained

    def _read_working_set_groups(
        self, metadata, active, groups, *, continuity=None, history=None, tools=()
    ):
        """Select bounded, complete source receipts with the correct freshness domain.

        Tool output is historical and low trust. A later unknown or mutating
        tool invalidates workspace observations because the planner cannot
        attest to per-file freshness from transcript text alone. Already
        observed, hash-bound artifact pages are immutable historical data.
        """
        raw_by_ref = {item.message_id: raw for item, raw in zip(metadata, active)}
        stale_historical_refs = (
            frozenset(history.retained_message_refs)
            if history is not None
            and history.count
            and (
                continuity is None
                or continuity.workspace_revision is None
                or history.last_workspace_revision is None
                or continuity.workspace_revision != history.last_workspace_revision
            )
            else frozenset()
        )
        names_by_call = {}
        definitions = {tool.name: tool for tool in tools}
        last_mutation = -1
        for index, (item, raw) in enumerate(zip(metadata, active)):
            if item.role != "assistant":
                continue
            for call in raw.get("tool_calls", ()):
                function = call.get("function", {}) if isinstance(call, dict) else {}
                name = function.get("name") if isinstance(function, dict) else None
                call_id = call.get("id") if isinstance(call, dict) else None
                if isinstance(call_id, str):
                    names_by_call[call_id] = (name, function.get("arguments"))
                definition = definitions.get(name)
                if definition is None or (
                    not definition.read_only and definition.context_role != "task_planning"
                ):
                    last_mutation = index
        # A large source file is often read in bounded line ranges. Keeping
        # only one receipt per path loses the rest of that working set at each
        # compaction and makes the model reread the file indefinitely.
        seen_ranges = set()
        selected = []
        indexes = {item.message_id: index for index, item in enumerate(metadata)}
        for group in reversed(groups):
            if len(seen_ranges) >= self.policy.read_working_set_count:
                break
            # Writes can have left the active transcript through compaction.
            # Classify the complete batch before applying workspace freshness:
            # immutable artifact observations do not attest to current files.
            workspace_stale = (
                not stale_historical_refs.isdisjoint(group)
                or min(indexes[ref] for ref in group) <= last_mutation
            )
            calls = [
                item for item in metadata if item.message_id in group and item.role == "assistant"
            ]
            results = [
                item for item in metadata if item.message_id in group and item.role == "tool"
            ]
            if len(calls) != 1 or len(results) != len(calls[0].tool_call_ids):
                continue
            identities = set()
            immutable_artifact_batch = True
            for result in results:
                name, arguments = names_by_call.get(result.tool_result_for, (None, None))
                definition = definitions.get(name)
                if definition is None or definition.context_role != "artifact_read":
                    immutable_artifact_batch = False
                if definition is not None and definition.context_role == "task_planning":
                    try:
                        planning = json.loads(raw_by_ref[result.message_id].get("content", ""))
                    except (TypeError, ValueError):
                        break
                    if not isinstance(planning, dict) or planning.get("status") != "ok":
                        break
                    # A host-owned planning update changes its task list, not
                    # source files. Preserve the entire mixed call/result batch
                    # without treating its planning status as acceptance.
                    continue
                if definition is not None and definition.context_role == "workspace_search":
                    request = _unambiguous_json_object(arguments)
                    if complete_search_observation(
                        raw_by_ref[result.message_id], arguments=request
                    ) is None:
                        break
                    # Keep the complete actual batch, including all returned
                    # matches. A repeated query is one source slot; neither
                    # its presence nor this identity attests to file freshness.
                    identities.add(("search", hashlib.sha256(_json(request).encode()).hexdigest()))
                    continue
                if definition is not None and definition.context_role == "artifact_read":
                    request = _unambiguous_json_object(arguments)
                    observed = artifact_page_observation(
                        raw_by_ref[result.message_id], arguments=request
                    )
                    if observed is None:
                        break
                    page = _unambiguous_json_object(observed["content"])["content"]
                    # Immutable host artifact coordinates are a source slot,
                    # not evidence of workspace freshness or complete source.
                    identities.add(("artifact", page["uri"], page["content_hash"],
                                    page["offset_chars"], page["next_offset_chars"]))
                    continue
                if (
                    definition is None
                    or definition.context_role != "workspace_read"
                    or not isinstance(arguments, str)
                ):
                    break
                try:
                    request = json.loads(arguments)
                    path = request.get("path")
                    receipt = json.loads(raw_by_ref[result.message_id].get("content", ""))
                except (TypeError, ValueError, AttributeError):
                    break
                content = receipt.get("content") if isinstance(receipt, dict) else None
                if (
                    not isinstance(path, str)
                    or not path
                    or not isinstance(receipt, dict)
                    or receipt.get("status") != "ok"
                    or not isinstance(content, dict)
                    or content.get("ok") is not True
                    or content.get("path") != path
                    or not isinstance(content.get("content"), str)
                ):
                    break
                read_range = content.get("read_range")
                if isinstance(read_range, dict):
                    start, end = read_range.get("start"), read_range.get("end")
                    if type(start) is not int or type(end) is not int or start < 1 or end < start:
                        break
                    identity = (path, start, end)
                else:
                    identity = (path, None, None)
                identities.add(identity)
            else:
                if workspace_stale and not immutable_artifact_batch:
                    continue
                # Preserve the complete assistant/result batch. Splitting it
                # would orphan tool results; duplicate ranges within a batch
                # do not consume extra source slots.
                new_ranges = identities - seen_ranges
                if (
                    new_ranges
                    and len(seen_ranges | identities) <= self.policy.read_working_set_count
                ):
                    seen_ranges.update(identities)
                    selected.append(group)
        return selected
