"""Repartition failed, unacknowledged summary input without dropping history.

A smaller quote is a new bounded attempt, not evidence that an empty output was
caused by input overflow. The caller retains failure receipts and owns budgets.
"""

from __future__ import annotations

import json

from .context_history import _historical_message, summary_media_blocks
from .context_partition import bind_pending, projection_digest, source_manifest
from .context_runtime import CompactionController, CompactionRequest, ContextPlanner
from .context_runtime_contracts import _summary_units
from .tool_observation import history_observation_message

SUMMARY_RETRY_POLICY = "complete-quoted-summary-retry/v1"


def repartition_failed_summary(
    planner: ContextPlanner,
    request: CompactionRequest,
    *,
    current_revision,
    current_continuity,
    current_messages,
) -> CompactionRequest | None:
    """Move a suffix of whole pending units back into the persistent queue.

    Previously acknowledged ranges and summaries stay unchanged. A fragment,
    continuation or single atomic batch cannot be made smaller by this policy.
    In particular, a partial tool batch is never presented as complete source.
    """
    CompactionController(planner)._validate_source(
        request, current_revision, current_continuity, current_messages
    )
    state = request.summary_partition
    if (
        state is None or state.pending is None or state.pending_source_parts
        or request.summary_continuations or request.summary_fragments
        or len(request.summary_messages) != 1
    ):
        return None
    content = request.summary_messages[0]["content"]
    body = json.loads(content[0]["text"] if isinstance(content, list) else content)
    if body.get("fragment") is not None:
        return None
    manifest, raw = source_manifest(
        current_messages, request.old_messages, request.retained_message_refs,
        capture_reader_names=request.source_capture_reader_names,
        fresh_tool_observation_policy=request.fresh_tool_observation_policy,
        fresh_tool_message_refs=request.fresh_tool_message_refs,
        fresh_text_fragment_policy=request.fresh_text_fragment_policy,
        fresh_text_excluded_tool_names=request.source_reader_names,
    )
    units_by_ref = {
        ref: parent for parent, batch in manifest["batches"].items() for ref in batch["refs"]
    }
    entries = []
    for message in body["transcript"]:
        ref = message["source_message_ref"]
        _, images = _historical_message(history_observation_message(raw[ref]))
        entries.append({
            "ref": ref, "message": message, "images": list(images),
            "payload_digest": projection_digest((raw[ref],)),
            "unit": units_by_ref.get(ref, ref),
            # This quote already passed source validation. A later packing
            # pass must not shorten its source text to fit a bigger batch.
            "summary_retry_quote": SUMMARY_RETRY_POLICY,
        })
    media = summary_media_blocks(
        entries, profile=request.profile if planner.tool_media_projection_version else None,
        source_messages=raw,
    )
    if (content[1:] if isinstance(content, list) else []) != media:
        return None
    units = _summary_units(tuple(entries))
    if len(units) < 2:
        return None
    selected = tuple(entry for unit in units[:max(1, len(units) // 2)] for entry in unit)
    messages, counted = planner._summary_projection(
        selected, body["previous_summary"], request.summary_instructions,
        profile=request.profile, source_messages=raw,
    )
    if (
        counted >= request.summary_counted_input_tokens
        or counted + request.summary_output_tokens > request.profile.context_window
    ):
        return None
    # Zero names an unsegmented request; once a suffix is queued it enters
    # the existing chunk protocol and its unchanged finite chunk limit.
    chunk_index = max(1, request.chunk_index)
    pending = bind_pending(
        state.model_copy(update={"pending": None}), manifest,
        tuple(entry["ref"] for entry in selected), messages, chunk_index,
    )
    return request.model_copy(update={
        "summary_messages": messages,
        "summary_counted_input_tokens": counted,
        "summary_partition": pending,
        "chunk_index": chunk_index,
        "remaining_history_entries": (
            *entries[len(selected):], *request.remaining_history_entries,
        ),
    })
