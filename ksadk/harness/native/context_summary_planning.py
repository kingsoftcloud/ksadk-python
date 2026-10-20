"""Bounded summary quote construction and complete-source partition planning."""

from __future__ import annotations

import json

from pydantic import (
    JsonValue,
)

from .context import (
    _tool_groups,
)
from .context_artifact_page import artifact_page_observation
from .context_fresh_tools import FRESH_TOOL_OBSERVATION_POLICY, fresh_tool_group_refs
from .context_history import (
    ContextRuntimeError,
    _historical_message,
    _json,
    _unambiguous_json_object,
    compact_historical_protocol,
    summary_media_blocks,
)
from .context_partition import (
    BatchPartition,
    bind_pending,
    expected_remaining_refs,
    fragment_view,
    new_coverage,
    raw_batch_entries,
    source_manifest,
    validate_coverage,
)
from .context_pruning import (
    project_historical_tool_result,
    project_workspace_read_result,
)
from .context_runtime_contracts import (
    READER_SUMMARY_POLICY_VERSION,
    CompactionRequest,
    ContextPlan,
    Messages,
    _complete_source_entry,
    _full_reader_quote,
    _summary_continuation,
    _summary_units,
    source_context_digest,
)
from .context_search import complete_search_observation
from .context_source_fragments import (
    FRESH_TEXT_FRAGMENT_POLICY,
    SourceTextPart,
    source_part_view,
    source_text_carrier,
)
from .context_summary_prompt import replacement_output_directive, rolling_work_state_instructions
from .model import ModelProfile
from .tool_observation import (
    history_observation_message,
)


class ContextSummaryPlanning:
    def _summary_directive(self, profile):
        if self.summary_prompt_policy not in {"rolling-work-state/v2", "rolling-work-state/v3"}:
            return ()
        limit = min(self.policy.summary_max_tokens, profile.max_output_tokens) if profile else (
            self.policy.summary_max_tokens
        )
        return (replacement_output_directive(
            limit, self.summary_budget_unit,
            preserve_evidence_scope=self.summary_prompt_policy == "rolling-work-state/v3",
        ),)

    def _summary_request_fits(self, counted: int, profile: ModelProfile) -> bool:
        reserve = 0
        if (profile.max_continuations
                and self.summary_completion_policy != "bounded-committed-summary/v1"):
            # A protocol-finished bounded summary is sealed at its output
            # ceiling instead of appending continuation turns. Reserve those
            # extra turns only for the continuation-based completion policy.
            reserve = (
                min(self.policy.summary_max_tokens, profile.max_output_tokens)
                + (
                    self._count_message({"role": "assistant", "content": ""})
                    + self._count_message(_summary_continuation())
                )
                * profile.max_continuations
            )
        output = self._summary_output_cap(counted, profile, full_reader_quote=False)
        return counted + output + reserve <= profile.context_window

    def _summary_unit_fits(self, counted, profile, entries):
        if any(
            entry.get("source_capture") is True or entry.get("observed_archive") is True
            or (entry.get("fresh_tool_observation") is True
                and not isinstance(entry.get("reader_path"), str)
                and entry.get("search_source") is not True
                and entry.get("artifact_source") is not True)
            or entry.get("summary_retry_quote") == "complete-quoted-summary-retry/v1"
            for entry in entries
        ):
            # Captured text is divisible historical data. Keep the original
            # output/continuation allocation instead of shrinking it to the
            # last few tokens beside an oversized indivisible receipt.
            return self._summary_request_fits(counted, profile)
        reader_count = sum(_complete_source_entry(entry) for entry in entries)
        if reader_count:
            if profile.max_continuations and (len(_summary_units(entries)) > 1 or reader_count > 1):
                # Separately schedulable source groups need not fill one
                # quote to its input ceiling. Keep the declared continuation
                # reserve and consume the remaining groups in later covered
                # chunks. A complete indivisible page still uses the bounded
                # output ceiling below; never clip it to buy this reserve.
                return self._summary_request_fits(counted, profile)
            # Complete source pages cannot be made smaller like opaque old
            # prose. The profile output limit is a ceiling, not a mandatory
            # allocation. Admit the complete quote only with positive output
            # space; the request uses that space within the original ceiling.
            return counted < profile.context_window
        return self._summary_request_fits(counted, profile)

    def _summary_output_cap(self, counted, profile, *, full_reader_quote):
        maximum = (
            min(self.policy.summary_max_tokens, profile.max_output_tokens)
            if self.summary_output_policy == "declared-summary-limit/v1"
            else profile.max_output_tokens
        )
        if full_reader_quote:
            return min(maximum, profile.context_window - counted)
        return maximum

    def _summary_projection(
        self,
        entries: tuple[dict[str, JsonValue], ...] | list[dict[str, JsonValue]],
        previous_summary: str,
        instructions: str,
        *,
        fragment: dict | None = None,
        profile: ModelProfile | None = None,
        source_messages=None,
    ) -> tuple[Messages, int]:
        media = summary_media_blocks(
            entries,
            profile=profile if self.tool_media_projection_version is not None else None,
            source_messages=source_messages,
        )
        history_text = _json(
            {
                "trust": "untrusted",
                "previous_summary": previous_summary,
                "transcript": [entry["message"] for entry in entries],
                **({"fragment": fragment} if fragment is not None else {}),
            }
        )
        projected = (
            {
                "role": "user",
                **({"kind": "summary_batch_fragment"} if fragment is not None else {}),
                "content": (
                    [{"type": "text", "text": history_text}, *media] if media else history_text
                ),
            },
            *self._summary_directive(profile),
        )
        counted = self._fixed(instructions, ()) + sum(
            self._count_message(m)
            for m in self._media_view(projected, profile, purpose="compaction")
        )
        return projected, counted

    def _next_chunk(
        self,
        entries: tuple[dict[str, JsonValue], ...],
        previous_summary: str,
        instructions: str,
        profile: ModelProfile,
        source_messages: dict[str, dict[str, JsonValue]],
    ) -> tuple[Messages, int, tuple[dict[str, JsonValue], ...]]:
        selected = []
        result = None
        for unit in _summary_units(entries):
            projected, counted = self._summary_projection(
                (*selected, *unit),
                previous_summary,
                instructions,
                profile=profile,
                source_messages=source_messages,
            )
            if not self._summary_unit_fits(counted, profile, (*selected, *unit)):
                if selected:
                    break
                unit = self._fit_summary_unit(
                    unit, previous_summary, instructions, profile, source_messages
                )
                projected, counted = self._summary_projection(
                    unit,
                    previous_summary,
                    instructions,
                    profile=profile,
                    source_messages=source_messages,
                )
            selected.extend(unit)
            result = projected, counted
        if result is None:
            raise ContextRuntimeError("compaction_source_over_budget")
        return *result, entries[len(selected) :]

    def _next_covered_chunk(
        self,
        entries,
        previous_summary,
        instructions,
        profile,
        source_messages,
        state,
        manifest,
        chunk_index,
    ):
        state = validate_coverage(state, manifest)
        if manifest.get("fresh_tool_observation_policy") == FRESH_TOOL_OBSERVATION_POLICY:
            fresh_refs = set(manifest["fresh_tool_message_refs"])
            # Resume from the source-bound selection, not a mutable packing
            # hint in persisted remaining entries. Lost hints cannot authorize
            # shortening a still-unobserved receipt after cold recovery.
            entries = tuple(
                {**entry, "fresh_tool_observation": (
                    entry["ref"] in fresh_refs and entry["message"].get("role") == "tool"
                )}
                for entry in entries
            )
        if tuple(entry["ref"] for entry in entries) != expected_remaining_refs(state, manifest):
            raise ContextRuntimeError("compaction_partition_remaining_changed")
        selected, selected_refs = [], []
        result = None
        for original_unit in _summary_units(entries):
            unit = original_unit
            parent = unit[0]["ref"]
            partition = next(
                (plan for plan in state.partitions if plan.assistant_ref == parent), None
            )
            if partition is not None:
                if selected:
                    break
                return self._partition_chunk(
                    unit,
                    entries,
                    previous_summary,
                    instructions,
                    profile,
                    source_messages,
                    state,
                    manifest,
                    partition,
                    chunk_index,
                )
            projected, counted = self._summary_projection(
                (*selected, *unit),
                previous_summary,
                instructions,
                profile=profile,
                source_messages=source_messages,
            )
            if not self._summary_unit_fits(counted, profile, (*selected, *unit)):
                if selected:
                    break
                try:
                    unit = self._fit_summary_unit(
                        unit, previous_summary, instructions, profile, source_messages
                    )
                except ContextRuntimeError as error:
                    if (
                        str(error) != "compaction_source_over_budget"
                        or parent not in manifest["batches"]
                    ):
                        raise
                    batch = manifest["batches"][parent]
                    width = max(1, (len(batch["receipt_refs"]) + 1) // 2)
                    while True:
                        partition = BatchPartition(
                            assistant_ref=parent,
                            source_batch_digest=batch["source_batch_digest"],
                            receipts_per_fragment=width,
                            parent_projection=(
                                "accepted-parent-reference/v1"
                                if (
                                    batch.get("observed_archive_refs")
                                    or batch.get("artifact_page_refs")
                                )
                                else None
                            ),
                        )
                        proposed = state.model_copy(
                            update={"partitions": (*state.partitions, partition)}
                        )
                        try:
                            return self._partition_chunk(
                                original_unit,
                                entries,
                                previous_summary,
                                instructions,
                                profile,
                                source_messages,
                                proposed,
                                manifest,
                                partition,
                                chunk_index,
                            )
                        except ContextRuntimeError as failure:
                            if str(failure) != "compaction_source_over_budget" or width == 1:
                                raise
                            width = max(1, width // 2)
                projected, counted = self._summary_projection(
                    unit,
                    previous_summary,
                    instructions,
                    profile=profile,
                    source_messages=source_messages,
                )
            selected.extend(unit)
            selected_refs.extend(entry["ref"] for entry in original_unit)
            result = projected, counted
        if result is None:
            raise ContextRuntimeError("compaction_source_over_budget")
        state = bind_pending(state, manifest, tuple(selected_refs), result[0], chunk_index)
        return *result, entries[len(selected_refs) :], state

    def _preflight_summary_units(
        self, entries, instructions, profile, source_messages, state, manifest
    ):
        for unit in _summary_units(entries):
            try:
                self._fit_summary_unit(unit, "", instructions, profile, source_messages)
            except ContextRuntimeError as error:
                parent = unit[0]["ref"]
                if (
                    str(error) != "compaction_source_over_budget"
                    or parent not in manifest["batches"]
                ):
                    raise
                full_unit = raw_batch_entries(unit, source_messages, manifest=manifest)
                batch = manifest["batches"][parent]
                plan = BatchPartition(
                    assistant_ref=parent,
                    source_batch_digest=batch["source_batch_digest"],
                    receipts_per_fragment=1,
                    parent_projection=(
                        "accepted-parent-reference/v1"
                        if batch.get("observed_archive_refs") or batch.get("artifact_page_refs")
                        else None
                    ),
                )
                for offset in range(len(batch["receipt_refs"])):
                    quote, frame, _, _ = fragment_view(
                        full_unit, state, manifest, plan, receipt_offset=offset
                    )
                    _, counted = self._summary_projection(
                        quote,
                        "",
                        instructions,
                        fragment=frame,
                        profile=profile,
                        source_messages=source_messages,
                    )
                    if not self._summary_unit_fits(counted, profile, quote):
                        ref = batch["receipt_refs"][offset]
                        capture = manifest.get("source_captures", {}).get(ref)
                        if capture is None:
                            raise ContextRuntimeError("compaction_source_over_budget")
                        staged = state.model_copy(update={"covered_count": (
                            manifest["dropped"].index(parent) + 1 + offset
                        )})
                        part = SourceTextPart(
                            source_ref=ref, source_text_digest=capture["text_digest"],
                            start_chars=0, stop_chars=1, total_chars=capture["total_chars"],
                            chunk_index=1,
                        )
                        quote, frame, _, _ = source_part_view(
                            full_unit, staged, manifest, plan, (part,)
                        )
                        _, counted = self._summary_projection(
                            quote, "", instructions, fragment=frame, profile=profile,
                            source_messages=source_messages,
                        )
                        if not self._summary_request_fits(counted, profile):
                            raise ContextRuntimeError("compaction_source_over_budget")

    def _partition_chunk(
        self,
        unit,
        entries,
        previous_summary,
        instructions,
        profile,
        source_messages,
        state,
        manifest,
        partition,
        chunk_index,
    ):
        # This path quotes full raw source only after the existing whole-unit
        # minimum could not fit. No raw/live transcript group is fragmented.
        full_unit = raw_batch_entries(unit, source_messages, manifest=manifest)
        batch = manifest["batches"][partition.assistant_ref]
        parent_index = manifest["dropped"].index(partition.assistant_ref)
        offset = max(0, state.covered_count - parent_index - 1)
        current_ref = batch["receipt_refs"][offset]
        if current_ref in manifest.get("source_captures", {}):
            return self._source_text_chunk(
                full_unit, unit, entries, previous_summary, instructions, profile,
                source_messages, state, manifest, partition, chunk_index,
            )
        while True:
            quoted, frame, covered, last_part = fragment_view(full_unit, state, manifest, partition)
            projected, counted = self._summary_projection(
                quoted,
                previous_summary,
                instructions,
                fragment=frame,
                profile=profile,
                source_messages=source_messages,
            )
            if self._summary_unit_fits(counted, profile, quoted):
                break
            if partition.receipts_per_fragment == 1:
                raise ContextRuntimeError("compaction_source_over_budget")
            # A text prefix may fit even when the following archived receipts
            # do not. Repack only unacknowledged receipts; keep their complete
            # observations, the accepted prefix and the provider cap intact.
            partition = partition.model_copy(update={
                "receipts_per_fragment": max(1, partition.receipts_per_fragment // 2),
            })
        state = state.model_copy(update={"partitions": tuple(
            partition if plan.assistant_ref == partition.assistant_ref else plan
            for plan in state.partitions
        )})
        state = bind_pending(state, manifest, covered, projected, chunk_index)
        remaining = entries[len(unit) :] if last_part else entries
        return projected, counted, remaining, state

    def _source_text_chunk(
        self, full_unit, unit, entries, previous_summary, instructions, profile,
        source_messages, state, manifest, partition, chunk_index,
    ):
        batch = manifest["batches"][partition.assistant_ref]
        parent_index = manifest["dropped"].index(partition.assistant_ref)
        offset = max(0, state.covered_count - parent_index - 1)
        parts, result = [], None

        def measure(proposed):
            quote, frame, covered, last = source_part_view(
                full_unit, state, manifest, partition, tuple(proposed)
            )
            projected, counted = self._summary_projection(
                quote, previous_summary, instructions, fragment=frame, profile=profile,
                source_messages=source_messages,
            )
            return projected, counted, covered, last

        for ref in batch["receipt_refs"][offset:]:
            capture = manifest.get("source_captures", {}).get(ref)
            if capture is None:
                break
            accepted = [part for part in state.source_parts if part.source_ref == ref]
            start = accepted[-1].stop_chars if accepted else 0
            candidate = SourceTextPart(
                source_ref=ref, source_text_digest=capture["text_digest"],
                start_chars=start, stop_chars=capture["total_chars"],
                total_chars=capture["total_chars"], chunk_index=chunk_index,
            )
            proposal = measure((*parts, candidate))
            if self._summary_request_fits(proposal[1], profile):
                parts.append(candidate)
                result = proposal
                continue
            low, high = start, candidate.total_chars
            while low < high:
                middle = (low + high + 1) // 2
                narrowed = candidate.model_copy(update={"stop_chars": middle})
                trial = measure((*parts, narrowed))
                if self._summary_request_fits(trial[1], profile):
                    low = middle
                else:
                    high = middle - 1
            if low > start:
                # Prefer complete lines; an oversized individual line can
                # still progress through explicit contiguous character parts.
                entry = next(item for item in full_unit if item["ref"] == ref)
                carrier, field = source_text_carrier(entry, capture)
                body = carrier[field]
                boundary = body.rfind("\n", start, low)
                stop = boundary + 1 if boundary >= start else low
                narrowed = candidate.model_copy(update={"stop_chars": stop})
                parts.append(narrowed)
                result = measure(parts)
            break
        if result is None or not self._summary_request_fits(result[1], profile):
            raise ContextRuntimeError("compaction_source_over_budget")
        projected, counted, covered, last = result
        state = bind_pending(
            state, manifest, covered, projected, chunk_index, source_parts=tuple(parts)
        )
        return projected, counted, entries[len(unit):] if last else entries, state

    def _fit_summary_unit(self, unit, previous_summary, instructions, profile, source_messages):
        """Fit quoted tool text without breaking a batch or altering receipts.

        The transcript remains authoritative. Each shorter excerpt is made
        from its original source, never by recursively clipping an excerpt.
        Calls, media, source references and digests are preserved. If those
        mandatory parts alone cannot fit, the existing block still applies.
        """
        projected_unit = unit
        limit = self.policy.summary_tool_output_max_chars
        while True:
            _, counted = self._summary_projection(
                projected_unit,
                previous_summary,
                instructions,
                profile=profile,
                source_messages=source_messages,
            )
            if self._summary_unit_fits(counted, profile, projected_unit):
                return projected_unit
            # Remove nested JSON encoding and duplicate display counters
            # before discarding source observations. This is still quoted
            # historical data; identities, calls, media and receipts stay.
            compact = tuple(
                {
                    **entry,
                    "message": compact_historical_protocol(
                        entry["message"],
                        max_prose_chars=(
                            None if any(_complete_source_entry(e) for e in unit) else 128
                        ),
                    ),
                }
                for entry in projected_unit
            )
            _, compact_count = self._summary_projection(
                compact,
                previous_summary,
                instructions,
                profile=profile,
                source_messages=source_messages,
            )
            if self._summary_unit_fits(compact_count, profile, compact):
                return compact
            if any(_complete_source_entry(entry) for entry in unit):
                # A source may not have reached the task model yet. Losing its
                # middle here would make the sealed summary its only view.
                # Partition complete raw receipts through quoted coverage;
                # never substitute a shorter prefix for unobserved source.
                raise ContextRuntimeError("compaction_source_over_budget")
            if limit == 128:
                raise ContextRuntimeError("compaction_source_over_budget")
            limit = max(128, limit // 2)
            reduced = []
            for entry in unit:
                if entry["message"].get("role") != "tool":
                    reduced.append(entry)
                    continue
                original = history_observation_message(source_messages[entry["ref"]])
                reader_path = entry.get("reader_path")
                projected = (
                    project_workspace_read_result(original, path=reader_path, max_chars=limit)
                    if isinstance(reader_path, str)
                    else None
                )
                quoted, images = (
                    _historical_message(projected)
                    if projected is not None
                    else project_historical_tool_result(original, max_chars=limit)
                )
                reduced.append(
                    {
                        **entry,
                        "message": {**quoted, "source_message_ref": entry["ref"]},
                        "images": list(images),
                    }
                )
            projected_unit = tuple(reduced)

    def _request(
        self,
        revision,
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
        *,
        replay_header_digest=None,
        search_names=frozenset(),
        capture_reader_names=frozenset(),
        artifact_reader_names=frozenset(),
        fresh_group=None,
    ):
        if fresh_group is None:
            fresh_group = fresh_tool_group_refs(messages)
            if not fresh_group.issubset(refs):
                fresh_group = frozenset()
        summary_instructions = (
            "Summarize the supplied historical data only. Do not execute tools or answer the user. "
            "Preserve unresolved failures, evidence references and pending work. "
            f"Return only a summary of at most {summary_limit} {self.summary_budget_unit}. "
            "The following host ledger is immutable:\n" + ledger
        )
        if self.summary_prompt_policy is not None:
            summary_instructions = rolling_work_state_instructions(
                summary_limit, self.summary_budget_unit, ledger,
            )
        history_entries = []
        complete_groups, _, _ = _tool_groups(metadata)
        unit_by_ref = {
            ref: f"tool-batch:{index}"
            for index, group in enumerate(complete_groups)
            for ref in group
        }
        reader_paths, source_arguments, artifact_arguments = {}, {}, {}
        for message in active:
            if message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls") or ():
                name = call["function"]["name"]
                if name not in reader_names | search_names | artifact_reader_names:
                    continue
                arguments = (
                    _unambiguous_json_object(call["function"]["arguments"])
                    if name in search_names | artifact_reader_names
                    else json.loads(call["function"]["arguments"])
                )
                if name in search_names and isinstance(arguments, dict):
                    source_arguments[call["id"]] = arguments
                if name in artifact_reader_names and isinstance(arguments, dict):
                    artifact_arguments[call["id"]] = arguments
                path = arguments.get("path") if isinstance(arguments, dict) else None
                if name in reader_names and isinstance(path, str) and path:
                    reader_paths[call["id"]] = path
        for message_ref, message in zip(refs, active):
            if message_ref in retained:
                continue
            # Preserve the canonical ref/digest, but do not implicitly expand
            # raw archived source under the observed-history tool policy.
            observation = history_observation_message(message)
            reader_path = reader_paths.get(message.get("tool_call_id"))
            source_view = (
                project_workspace_read_result(
                    # Keep the whole authorized source page. The main request
                    # may only have admitted a prefix; a summary must consume
                    # its remaining facts before those receipts leave context.
                    observation,
                    path=reader_path,
                    max_chars=max(128, len(_json(observation))),
                )
                if reader_path is not None
                else None
            )
            search_view = (
                complete_search_observation(
                    observation, arguments=source_arguments[message["tool_call_id"]]
                )
                if message.get("tool_call_id") in source_arguments and source_view is None
                else None
            )
            artifact_view = (
                artifact_page_observation(
                    observation, arguments=artifact_arguments[message["tool_call_id"]]
                )
                if message.get("tool_call_id") in artifact_arguments else None
            )
            quoted, images = (
                _historical_message(
                    source_view if source_view is not None
                    else search_view if search_view is not None
                    else artifact_view if artifact_view is not None else observation
                )
                if source_view is not None or search_view is not None or artifact_view is not None
                or observation is not message
                or message_ref in fresh_group
                else project_historical_tool_result(
                    observation, max_chars=self.policy.summary_tool_output_max_chars
                )
            )
            history_entries.append(
                {
                    "ref": message_ref,
                    "message": {**quoted, "source_message_ref": message_ref},
                    "images": list(images),
                    "payload_digest": source_context_digest((message,)),
                    "unit": unit_by_ref.get(message_ref, message_ref),
                    **({"reader_path": reader_path} if source_view is not None else {}),
                    **({"search_source": True} if search_view is not None else {}),
                    **({"artifact_source": True} if artifact_view is not None else {}),
                    **({"observed_archive": True} if observation is not message else {}),
                    **({"fresh_tool_observation": True}
                       if message_ref in fresh_group and message.get("role") == "tool" else {}),
                }
            )

        if reader_names or search_names or artifact_reader_names or fresh_group:
            # Normalize quoted protocol before aggregate packing, not only
            # when an individual unit overflows. Nested JSON encoding can
            # otherwise force extra summary calls even with complete payloads
            # fitting in one request. This is low-trust historical data; full
            # prose, raw receipt authority, refs, digests and media stay intact.
            history_entries = [
                {
                    **entry,
                    "message": compact_historical_protocol(entry["message"], max_prose_chars=None),
                }
                for entry in history_entries
            ]
        retained_refs = tuple(ref for ref in refs if ref in retained)
        fresh_refs = tuple(ref for ref in refs if ref in fresh_group)
        manifest, source_messages = source_manifest(
            messages, metadata, retained_refs, capture_reader_names=capture_reader_names,
            search_fragment_policy=self.search_fragment_policy,
            capture_search_names=search_names if self.search_fragment_policy is not None else (),
            fresh_tool_observation_policy=FRESH_TOOL_OBSERVATION_POLICY,
            fresh_tool_message_refs=fresh_refs,
            fresh_text_fragment_policy=FRESH_TEXT_FRAGMENT_POLICY,
            fresh_text_excluded_tool_names=reader_names | search_names | artifact_reader_names,
        )
        if manifest.get("source_captures"):
            history_entries = [
                {**entry, **({"source_capture": True}
                            if entry["ref"] in manifest["source_captures"] else {})}
                for entry in history_entries
            ]
        summary_messages, summary_input = self._summary_projection(
            history_entries,
            history.summary_text,
            summary_instructions,
            profile=profile,
            source_messages=source_messages,
        )
        # Active history consists of new messages and previously retained
        # messages, neither of which has been consumed by an earlier summary.
        # Clearing an old result here would discard its observations before
        # summarization. Fit excerpts and chunk complete batches instead.
        chunk_index = 0
        remaining: tuple[dict[str, JsonValue], ...] = ()
        partition_state = new_coverage(manifest)
        if not self._summary_unit_fits(summary_input, profile, history_entries):
            chunk_index = 1
            self._preflight_summary_units(
                tuple(history_entries),
                summary_instructions,
                profile,
                source_messages,
                partition_state,
                manifest,
            )
            summary_messages, summary_input, remaining, partition_state = self._next_covered_chunk(
                tuple(history_entries),
                history.summary_text,
                summary_instructions,
                profile,
                source_messages,
                partition_state,
                manifest,
                chunk_index,
            )
        else:
            partition_state = bind_pending(
                partition_state,
                manifest,
                tuple(entry["ref"] for entry in history_entries),
                summary_messages,
                chunk_index,
            )
        full_reader_quote = _full_reader_quote(summary_messages, history_entries)
        summary_output_tokens = self._summary_output_cap(
            summary_input, profile, full_reader_quote=full_reader_quote
        )
        request = CompactionRequest(
            source_revision=revision,
            model_turn=model_turn,
            profile=profile,
            continuity=continuity,
            source_context_digest=source_context_digest(messages),
            source_message_count=len(messages),
            old_messages=metadata,
            retained_message_refs=retained_refs,
            retained_messages=tuple(raw for ref, raw in zip(refs, active) if ref in retained),
            budget=budget,
            instructions=instructions,
            replay_header_digest=replay_header_digest,
            summary_instructions=summary_instructions,
            summary_messages=summary_messages,
            summary_counted_input_tokens=summary_input,
            summary_max_tokens=summary_limit,
            summary_output_tokens=summary_output_tokens,
            tokenizer_id=self.tokenizer_id,
            tool_media_projection_version=self.tool_media_projection_version,
            history=history,
            source_reader_names=tuple(sorted(reader_names | search_names | artifact_reader_names)),
            source_capture_reader_names=tuple(sorted(capture_reader_names)),
            search_fragment_policy=self.search_fragment_policy,
            source_capture_search_names=(
                tuple(sorted(search_names)) if self.search_fragment_policy is not None else ()
            ),
            fresh_tool_observation_policy=FRESH_TOOL_OBSERVATION_POLICY,
            fresh_tool_message_refs=fresh_refs,
            fresh_text_fragment_policy=FRESH_TEXT_FRAGMENT_POLICY,
            protected_reader_call_ids=tuple(sorted(protected_readers)),
            full_reader_quote=full_reader_quote,
            reader_summary_policy_version=READER_SUMMARY_POLICY_VERSION,
            summary_budget_mode=(
                "context_bounded_estimate"
                if self.summary_budget_mode == "context_bounded_estimate"
                else None
            ),
            summary_completion_policy=self.summary_completion_policy,
            compaction_message_projection=self.compaction_message_projection,
            summary_prompt_policy=self.summary_prompt_policy,
            chunk_index=chunk_index,
            remaining_history_entries=remaining,
            turn_control=turn_control,
            summary_partition_version="quoted-history-coverage/v1",
            summary_partition=partition_state,
        )
        return ContextPlan(action="compact", reason="context_pressure", request=request)
