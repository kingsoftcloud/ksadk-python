"""Host coverage of quoted history; never split live tool protocol.

Persisted state inherits the Store/CAS trust boundary. Source/projection hashes
detect inconsistent views; they do not authenticate arbitrary rewritten state.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .context import _tool_groups
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
from .context_search import complete_search_observation
from .context_source_fragments import (
    FRESH_TEXT_FRAGMENT_POLICY,
    SOURCE_FRAGMENT_POLICY,
    SourceTextPart,
    accepted_batch_parent_reference,
    captured_source,
    source_part_view,
    text_digest,
    validate_source_parts,
)
from .tool_observation import (
    OBSERVED_HISTORY_VERSION,
    TYPED_HISTORY_VERSION,
    history_observation_message,
)


def projection_digest(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


class _Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CoverageRange(_Contract):
    start: int = Field(strict=True, ge=0)
    stop: int = Field(strict=True, ge=0)
    chunk_index: int = Field(strict=True, ge=0)
    projection_digest: str


class BatchPartition(_Contract):
    assistant_ref: str
    source_batch_digest: str
    receipts_per_fragment: int = Field(strict=True, ge=1)
    parent_projection: Literal["accepted-parent-reference/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class SummaryPartitionState(_Contract):
    version: Literal["quoted-history-coverage/v1"] = "quoted-history-coverage/v1"
    source_context_digest: str
    source_manifest_digest: str
    covered_count: int = Field(default=0, strict=True, ge=0)
    accepted: tuple[CoverageRange, ...] = ()
    pending: CoverageRange | None = None
    partitions: tuple[BatchPartition, ...] = ()
    source_fragment_policy: Literal["captured-source-fragments/v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    source_parts: tuple[SourceTextPart, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    pending_source_parts: tuple[SourceTextPart, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )


def source_manifest(
    messages, old_messages, retained_refs, *, capture_reader_names=(),
    fresh_tool_observation_policy=None, fresh_tool_message_refs=(),
    fresh_text_fragment_policy=None, fresh_text_excluded_tool_names=(),
    search_fragment_policy=None, capture_search_names=(),
):
    refs = tuple(
        f"message:{index}:{projection_digest((message,))}" for index, message in enumerate(messages)
    )
    raw = dict(zip(refs, messages))
    if search_fragment_policy not in {None, "complete-search-fragments/v1"} or (
        capture_search_names and search_fragment_policy is None
    ):
        raise ContextRuntimeError("compaction_search_fragment_policy_changed")
    if fresh_text_fragment_policy is not None and (
        fresh_text_fragment_policy != FRESH_TEXT_FRAGMENT_POLICY
        or fresh_tool_observation_policy != FRESH_TOOL_OBSERVATION_POLICY
    ):
        raise ContextRuntimeError("compaction_fresh_text_policy_changed")
    if fresh_tool_observation_policy is None:
        if fresh_tool_message_refs:
            raise ContextRuntimeError("compaction_fresh_tool_policy_missing")
    else:
        if fresh_tool_observation_policy != FRESH_TOOL_OBSERVATION_POLICY:
            raise ContextRuntimeError("compaction_fresh_tool_policy_changed")
        fresh = fresh_tool_group_refs(messages)
        active_refs = {item.message_id for item in old_messages}
        eligible = fresh if fresh.issubset(active_refs) else frozenset()
        expected = tuple(ref for ref in refs if ref in eligible)
        if tuple(fresh_tool_message_refs) != expected:
            raise ContextRuntimeError("compaction_fresh_tool_source_changed")
        kept = eligible.intersection(retained_refs)
        if kept and kept != eligible:
            raise ContextRuntimeError("compaction_partition_partial_retained_batch")
    for item in old_messages:
        original = raw.get(item.message_id)
        if original is None or item.payload_digest != projection_digest((original,)):
            raise ContextRuntimeError("compaction_partition_source_changed")
        if (
            item.role != original["role"]
            or item.tool_call_ids != tuple(call["id"] for call in original.get("tool_calls") or ())
            or item.tool_result_for != original.get("tool_call_id")
        ):
            raise ContextRuntimeError("compaction_partition_source_changed")
    dropped = tuple(
        item.message_id for item in old_messages if item.message_id not in retained_refs
    )
    groups, _, _ = _tool_groups(old_messages)
    batches, captures = {}, {}
    for group in groups:
        if group.intersection(dropped) and not group.issubset(dropped):
            raise ContextRuntimeError("compaction_partition_partial_retained_batch")
        if not group.issubset(dropped):
            continue
        members = tuple(ref for ref in dropped if ref in group)
        assistant_ref = members[0]
        receipt_refs = members[1:]
        calls = tuple(call["id"] for call in raw[assistant_ref]["tool_calls"])
        receipt_calls = tuple(raw[ref]["tool_call_id"] for ref in receipt_refs)
        if len(calls) != len(receipt_calls) or set(calls) != set(receipt_calls):
            raise ContextRuntimeError("compaction_partition_mapping_invalid")
        batches[assistant_ref] = {
            "refs": members,
            "receipt_refs": receipt_refs,
            "call_ids": calls,
            "source_batch_digest": projection_digest(tuple(raw[ref] for ref in members)),
        }
        observed = tuple(
            ref for ref in receipt_refs
            if isinstance(raw[ref].get("model_view"), dict)
            and raw[ref]["model_view"].get("version")
            in {OBSERVED_HISTORY_VERSION, TYPED_HISTORY_VERSION}
        )
        if observed:
            for ref in observed:
                history_observation_message(raw[ref])  # Validate the selected view.
            batches[assistant_ref]["observed_archive_refs"] = observed
        if capture_reader_names:
            calls_by_id = {
                call["id"]: call["function"] for call in raw[assistant_ref]["tool_calls"]
            }
            for ref in receipt_refs:
                function = calls_by_id[raw[ref]["tool_call_id"]]
                if function["name"] not in capture_reader_names:
                    continue
                body = captured_source(
                    raw[ref], arguments=_unambiguous_json_object(function["arguments"])
                )
                if body is not None:
                    captures[ref] = {"total_chars": len(body), "text_digest": text_digest(body)}
                    if artifact_page_observation(
                        raw[ref], arguments=_unambiguous_json_object(function["arguments"])
                    ) is not None:
                        captures[ref]["source_field"] = "content.content"
                        batches[assistant_ref].setdefault("artifact_page_refs", []).append(ref)
        if search_fragment_policy is not None:
            functions = {
                call["id"]: call["function"] for call in raw[assistant_ref]["tool_calls"]
            }
            for ref in receipt_refs:
                function = functions[raw[ref]["tool_call_id"]]
                if function["name"] not in capture_search_names:
                    continue
                observation = history_observation_message(raw[ref])
                if complete_search_observation(
                    observation, arguments=_unambiguous_json_object(function["arguments"])
                ) is None:
                    continue
                # Quote the entire authorized envelope as contiguous historical
                # text, including query, matches, context and truncation flags.
                # Each part is data, never a partial live search result.
                quoted, images = _historical_message(raw[ref])
                body = quoted.get("content")
                if isinstance(body, str) and body and not images:
                    captures[ref] = {
                        "total_chars": len(body), "text_digest": text_digest(body),
                        "source_field": "content",
                    }
        if fresh_text_fragment_policy is not None:
            functions = {
                call["id"]: call["function"] for call in raw[assistant_ref]["tool_calls"]
            }
            for ref in receipt_refs:
                if (
                    ref in captures or ref not in fresh_tool_message_refs
                    or functions[raw[ref]["tool_call_id"]]["name"]
                    in fresh_text_excluded_tool_names
                ):
                    continue
                quoted, images = _historical_message(raw[ref])
                body = quoted.get("content")
                if isinstance(body, str) and body and not images:
                    captures[ref] = {
                        "total_chars": len(body), "text_digest": text_digest(body),
                        "source_field": "content",
                    }
    manifest = {
        "source_context_digest": projection_digest(messages),
        "dropped": dropped,
        "payload_digests": tuple(projection_digest((raw[ref],)) for ref in dropped),
        "batches": batches,
    }
    if search_fragment_policy is not None:
        manifest["search_fragment_policy"] = search_fragment_policy
        manifest["capture_search_names"] = tuple(sorted(capture_search_names))
    if captures:
        manifest["source_captures"] = captures
    if fresh_tool_observation_policy is not None:
        manifest["fresh_tool_observation_policy"] = fresh_tool_observation_policy
        manifest["fresh_tool_message_refs"] = tuple(fresh_tool_message_refs)
    if fresh_text_fragment_policy is not None:
        manifest["fresh_text_fragment_policy"] = fresh_text_fragment_policy
        manifest["fresh_text_excluded_tool_names"] = tuple(sorted(fresh_text_excluded_tool_names))
    return manifest, raw


def new_coverage(manifest):
    return SummaryPartitionState(
        source_context_digest=manifest["source_context_digest"],
        source_manifest_digest=projection_digest(manifest),
    )


def validate_coverage(state, manifest, *, messages=None, chunk_index=None, raw=None, profile=None):
    state = SummaryPartitionState.model_validate_json(state.model_dump_json())
    if state.source_context_digest != manifest["source_context_digest"] or (
        state.source_manifest_digest != projection_digest(manifest)
    ):
        raise ContextRuntimeError("compaction_partition_manifest_changed")
    count, last_chunk = 0, -1
    for accepted in state.accepted:
        if (
            accepted.start != count
            or accepted.stop < count
            or (accepted.stop == count and not any(
                part.chunk_index == accepted.chunk_index for part in state.source_parts
            ))
            or (accepted.stop > len(manifest["dropped"]) or accepted.chunk_index <= last_chunk)
        ):
            raise ContextRuntimeError("compaction_partition_coverage_gap_or_overlap")
        count, last_chunk = accepted.stop, accepted.chunk_index
    if state.covered_count != count:
        raise ContextRuntimeError("compaction_partition_cursor_unacknowledged")
    validate_source_parts(state, manifest)
    plans = set()
    for plan in state.partitions:
        batch = manifest["batches"].get(plan.assistant_ref)
        if (
            plan.assistant_ref in plans
            or batch is None
            or (plan.source_batch_digest != batch["source_batch_digest"])
            or (plan.parent_projection is not None and not (
                batch.get("observed_archive_refs") or batch.get("artifact_page_refs")
            ))
        ):
            raise ContextRuntimeError("compaction_partition_plan_invalid")
        plans.add(plan.assistant_ref)
    if state.pending is not None:
        pending = state.pending
        if (
            pending.start != count
            or pending.stop < count
            or (pending.stop == count and not state.pending_source_parts)
            or (pending.stop > len(manifest["dropped"]) or pending.chunk_index <= last_chunk)
        ):
            raise ContextRuntimeError("compaction_partition_pending_invalid")
        if messages is not None and (
            pending.projection_digest != projection_digest(messages)
            or pending.chunk_index != chunk_index
        ):
            raise ContextRuntimeError("compaction_partition_projection_changed")
        if (
            messages is not None
            and quoted_coverage(messages, state, manifest, raw, profile=profile)
            != (manifest["dropped"][pending.start : pending.stop])
        ):
            raise ContextRuntimeError("compaction_partition_projection_coverage_changed")
    elif messages is not None:
        raise ContextRuntimeError("compaction_partition_pending_missing")
    return state


def expected_remaining_refs(state, manifest):
    suffix = manifest["dropped"][state.covered_count :]
    for plan in state.partitions:
        batch = manifest["batches"][plan.assistant_ref]
        if suffix and suffix[0] in batch["receipt_refs"]:
            # Keep the original complete source unit until its last quote.
            last = manifest["dropped"].index(batch["refs"][-1]) + 1
            return (*batch["refs"], *manifest["dropped"][last:])
    return suffix


def bind_pending(state, manifest, source_refs, messages, chunk_index, *, source_parts=()):
    state = validate_coverage(state, manifest)
    if state.pending is not None:
        raise ContextRuntimeError("compaction_partition_pending_already_exists")
    stop = state.covered_count + len(source_refs)
    if tuple(source_refs) != manifest["dropped"][state.covered_count : stop] or (
        not source_refs and not source_parts
    ):
        raise ContextRuntimeError("compaction_partition_selection_not_prefix")
    pending = CoverageRange(
        start=state.covered_count,
        stop=stop,
        chunk_index=chunk_index,
        projection_digest=projection_digest(messages),
    )
    updated = state.model_copy(update={
        "pending": pending,
        "pending_source_parts": source_parts,
        "source_fragment_policy": SOURCE_FRAGMENT_POLICY if source_parts
        else state.source_fragment_policy,
    })
    validate_source_parts(updated, manifest)
    return updated


def acknowledge(state, manifest, raw, messages, chunk_index, *, profile=None):
    state = validate_coverage(
        state, manifest, messages=messages, chunk_index=chunk_index, raw=raw, profile=profile
    )
    return state.model_copy(
        update={
            "covered_count": state.pending.stop,
            "accepted": (*state.accepted, state.pending),
            "pending": None,
            "source_parts": (*state.source_parts, *state.pending_source_parts),
            "pending_source_parts": (),
        }
    )


def require_complete(state, manifest):
    state = validate_coverage(state, manifest)
    if state.pending is not None or state.covered_count != len(manifest["dropped"]):
        raise ContextRuntimeError("compaction_chunks_incomplete")


def quoted_coverage(messages, state, manifest, raw, *, profile=None):
    # This is the host-owned outer quote envelope, not JSON found inside any
    # tool output or model-generated summary text. It grants no execution.
    try:
        content = messages[0]["content"]
        text = content[0]["text"] if isinstance(content, list) else content
        body = json.loads(text)
        transcript = body["transcript"]
        refs = tuple(message["source_message_ref"] for message in transcript)
        if body["trust"] != "untrusted":
            raise ValueError
    except (KeyError, TypeError, ValueError, IndexError):
        raise ContextRuntimeError("compaction_partition_quote_invalid") from None
    frame = body.get("fragment")
    if frame is None:
        for batch in manifest["batches"].values():
            members = set(batch["refs"])
            if members.intersection(refs) and not members.issubset(refs):
                raise ContextRuntimeError("compaction_partition_unmarked_partial_batch")
        _validate_fresh_quote(transcript, content, manifest, raw, profile=profile)
        return refs
    if not isinstance(frame, dict):
        raise ContextRuntimeError("compaction_partition_quote_invalid")
    plan = next(
        (
            plan
            for plan in state.partitions
            if plan.assistant_ref == frame.get("source_assistant_ref")
        ),
        None,
    )
    if plan is None or raw is None:
        raise ContextRuntimeError("compaction_partition_fragment_without_plan")
    batch = manifest["batches"][plan.assistant_ref]
    unit = tuple(
        {"ref": ref, "payload_digest": projection_digest((raw[ref],))} for ref in batch["refs"]
    )
    unit = raw_batch_entries(unit, raw, manifest=manifest)
    if frame.get("schema") in {
        "quoted-completed-source-fragments/v1", "quoted-completed-source-fragments/v2"
    }:
        if not state.pending_source_parts:
            raise ContextRuntimeError("compaction_source_fragment_pending_missing")
        selected, expected_frame, covered, _ = source_part_view(
            unit, state, manifest, plan, state.pending_source_parts
        )
    else:
        if state.pending_source_parts:
            raise ContextRuntimeError("compaction_source_fragment_frame_changed")
        selected, expected_frame, covered, _ = fragment_view(unit, state, manifest, plan)
    if frame != json.loads(_json(expected_frame)) or transcript != [
        entry["message"] for entry in selected
    ]:
        raise ContextRuntimeError("compaction_partition_fragment_source_changed")
    media = summary_media_blocks(selected, profile=profile, source_messages=raw)
    if (media and (not isinstance(content, list) or content[1:] != media)) or (
        not media and not isinstance(content, str)
    ):
        raise ContextRuntimeError("compaction_partition_fragment_media_changed")
    return covered


def _validate_fresh_quote(transcript, content, manifest, raw, *, profile):
    """Validate complete authorized source, not merely a pending ref digest."""
    fresh = set(manifest.get("fresh_tool_message_refs", ()))
    if not fresh.intersection(message["source_message_ref"] for message in transcript):
        return
    if manifest.get("fresh_tool_observation_policy") != FRESH_TOOL_OBSERVATION_POLICY:
        raise ContextRuntimeError("compaction_fresh_tool_policy_missing")
    entries = []
    for message in transcript:
        ref = message["source_message_ref"]
        if raw is None or ref not in raw:
            raise ContextRuntimeError("compaction_fresh_tool_source_changed")
        quoted, images = _historical_message(raw[ref])
        expected = {**quoted, "source_message_ref": ref}
        if ref in fresh and _json(message) not in {
            _json(expected),
            _json(compact_historical_protocol(expected, max_prose_chars=None)),
        }:
            raise ContextRuntimeError("compaction_fresh_tool_quote_changed")
        entries.append({
            "ref": ref, "message": message, "images": list(images),
            "payload_digest": projection_digest((raw[ref],)),
        })
    media = summary_media_blocks(entries, profile=profile, source_messages=raw)
    if (media and (not isinstance(content, list) or content[1:] != media)) or (
        not media and not isinstance(content, str)
    ):
        raise ContextRuntimeError("compaction_fresh_tool_media_changed")


def raw_batch_entries(unit, raw, *, manifest=None):
    result = []
    for entry in unit:
        quoted, images = _historical_message(raw[entry["ref"]])
        message = compact_historical_protocol(quoted)
        capture = (manifest or {}).get("source_captures", {}).get(entry["ref"], {})
        if capture.get("source_field") == "content":
            message["content"] = quoted["content"]
        # Keep the complete parent prose, calls and all original tool content.
        if quoted.get("role") == "assistant":
            message["content"] = quoted.get("content")
            message.pop("content_omitted_chars", None)
        result.append(
            {
                **entry,
                "message": {**message, "source_message_ref": entry["ref"]},
                "images": list(images),
            }
        )
    return tuple(result)


def fragment_view(unit, state, manifest, plan, *, receipt_offset=None):
    batch = manifest["batches"][plan.assistant_ref]
    parent_index = manifest["dropped"].index(plan.assistant_ref)
    offset = (
        max(0, state.covered_count - parent_index - 1) if receipt_offset is None else receipt_offset
    )
    total = len(batch["receipt_refs"])
    # Coverage acknowledgement owns the cursor. Text captures can consume a
    # different number of receipts, and later quotes can require a narrower
    # packing width. Neither must realign or replay an accepted prefix.
    # bind_pending/validate_coverage still require the exact canonical prefix.
    if not 0 <= offset < total:
        raise ContextRuntimeError("compaction_partition_fragment_cursor_invalid")
    stop = min(offset + plan.receipts_per_fragment, total)
    assistant = unit[0]
    if plan.parent_projection is not None and offset:
        assistant = accepted_batch_parent_reference(
            assistant, plan.assistant_ref, batch["source_batch_digest"]
        )
    receipts = unit[1 + offset : 1 + stop]
    if tuple(entry["ref"] for entry in unit) != batch["refs"]:
        raise ContextRuntimeError("compaction_partition_remaining_changed")
    frame = {
        "schema": (
            "quoted-completed-tool-batch-fragment/v2"
            if plan.parent_projection is not None else "quoted-completed-tool-batch-fragment/v1"
        ),
        "source_batch_digest": batch["source_batch_digest"],
        "source_assistant_ref": plan.assistant_ref,
        "total_calls": len(batch["call_ids"]),
        "call_ids": batch["call_ids"],
        "fragment_index": offset // plan.receipts_per_fragment,
        "fragment_count": (total + plan.receipts_per_fragment - 1) // plan.receipts_per_fragment,
        "included_receipts": [
            {
                "source_ref": entry["ref"],
                "source_payload_digest": entry["payload_digest"],
                "tool_call_id": entry["message"]["tool_call_id"],
            }
            for entry in receipts
        ],
        "excluded_receipts_are_already_executed": True,
        **({"parent_projection": plan.parent_projection,
            "parent_already_accepted": offset > 0} if plan.parent_projection is not None else {}),
    }
    covered = tuple(entry["ref"] for entry in receipts)
    if offset == 0:
        covered = (assistant["ref"], *covered)
    return (assistant, *receipts), frame, covered, stop == total
