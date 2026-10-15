"""Pure quotation of contiguous text from already-executed source captures.

A source slice is historical data, never a new tool execution or live result.
The caller supplies a locked reader name; byte/coverage identities bind the
quoted range to the canonical receipt rather than trusting tool prose.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import Field

from .context_artifact_page import artifact_page_observation
from .context_history import ContextRuntimeError, _unambiguous_json_object
from .state import Contract
from .tool_observation import model_observation_message

SOURCE_FRAGMENT_POLICY = "captured-source-fragments/v1"
FRESH_TEXT_FRAGMENT_POLICY = "quoted-fresh-tool-text/v1"


class SourceTextPart(Contract):
    source_ref: str
    source_text_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    start_chars: int = Field(strict=True, ge=0)
    stop_chars: int = Field(strict=True, ge=1)
    total_chars: int = Field(strict=True, ge=1)
    chunk_index: int = Field(strict=True, ge=1)


def captured_source(message, arguments):
    if artifact_page_observation(message, arguments=arguments) is not None:
        return _unambiguous_json_object(message["content"])["content"]["content"] or None
    if message.get("role") != "tool" or "model_view" not in message:
        return None
    model_observation_message(message)  # Detect inconsistent persisted views.
    receipt = _unambiguous_json_object(message.get("content"))
    source = receipt.get("content") if receipt and receipt.get("status") == "ok" else None
    if not isinstance(source, dict) or source.get("ok") is not True:
        return None
    carrier = source.get("requested_source")
    if not isinstance(carrier, dict) or not isinstance(arguments, dict):
        return None
    path, body, span = (carrier.get(k) for k in ("path", "content", "read_range"))
    if (
        not isinstance(path, str) or not path or path != source.get("path")
        or path != arguments.get("path") or not isinstance(body, str) or not body
        or not isinstance(span, dict) or set(span) != {"start", "end"}
        or any(type(v) is not int for v in span.values())
        or type(source.get("total_lines")) is not int
        or not 1 <= span["start"] <= span["end"] <= source["total_lines"]
        or len(body.split("\n")) != span["end"] - span["start"] + 1
    ):
        return None
    # Numbered and unnumbered captures share exact character coordinates.
    # No source lines are selected by their semantic content.
    return body


def text_digest(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def source_text_carrier(entry, capture):
    """Find only the host-manifest's declared source field in quoted data."""
    field = capture.get("source_field", "content.requested_source.content")
    if field not in {"content.requested_source.content", "content.content", "content"}:
        raise ContextRuntimeError("compaction_source_fragment_identity_changed")
    carrier = entry["message"] if field == "content" else entry["message"]["content"]
    pieces = field.split(".")
    for key in pieces[:-1]:
        carrier = carrier[key]
    return carrier, pieces[-1]


def accepted_batch_parent_reference(parent, assistant_ref, source_batch_digest):
    """Refer to the already-accepted full parent without repeating its prose."""
    return {
        **parent,
        "message": {
            "role": "assistant", "source_message_ref": assistant_ref,
            "kind": "previously_quoted_tool_batch_parent",
            "source_batch_digest": source_batch_digest,
            "content": None,
        },
    }


def source_part_view(unit, state, manifest, plan, parts):
    """Rebuild a quote from canonical data and explicit ordered source parts."""
    batch = manifest["batches"][plan.assistant_ref]
    if tuple(entry["ref"] for entry in unit) != batch["refs"] or not parts:
        raise ContextRuntimeError("compaction_source_fragment_invalid")
    parent_index = manifest["dropped"].index(plan.assistant_ref)
    offset = max(0, state.covered_count - parent_index - 1)
    refs = batch["receipt_refs"][offset : offset + len(parts)]
    if tuple(part.source_ref for part in parts) != refs:
        raise ContextRuntimeError("compaction_source_fragment_selection_changed")
    parent = unit[0]
    if state.covered_count > parent_index:
        # The first accepted source quote already consumed the complete
        # assistant parent. Repeat its canonical reference, rather than its
        # prose and every call's arguments in each subsequent text piece.
        # This marker is historical data, never live assistant/tool protocol.
        parent = accepted_batch_parent_reference(
            parent, plan.assistant_ref, batch["source_batch_digest"]
        )
    selected, covered = [parent], []
    if state.covered_count == parent_index:
        covered.append(plan.assistant_ref)
    for index, (ref, part) in enumerate(zip(refs, parts)):
        capture = manifest.get("source_captures", {}).get(ref)
        if (
            capture is None or part.source_text_digest != capture["text_digest"]
            or part.total_chars != capture["total_chars"]
            or not part.start_chars < part.stop_chars <= part.total_chars
        ):
            raise ContextRuntimeError("compaction_source_fragment_identity_changed")
        previous = [p for p in state.source_parts if p.source_ref == ref]
        expected_start = previous[-1].stop_chars if previous else 0
        if part.start_chars != expected_start:
            raise ContextRuntimeError("compaction_source_fragment_gap_or_overlap")
        entry = json.loads(json.dumps(unit[1 + offset + index], ensure_ascii=False))
        carrier, field = source_text_carrier(entry, capture)
        body = carrier[field]
        if text_digest(body) != part.source_text_digest:
            raise ContextRuntimeError("compaction_source_fragment_identity_changed")
        carrier[field] = body[part.start_chars : part.stop_chars]
        selected.append(entry)
        if part.stop_chars == part.total_chars:
            covered.append(ref)
        elif index != len(parts) - 1:
            raise ContextRuntimeError("compaction_source_fragment_nonfinal_partial")
    frame = {
        "schema": "quoted-completed-source-fragments/v1",
        "source_batch_digest": batch["source_batch_digest"],
        "source_assistant_ref": plan.assistant_ref,
        "call_ids": batch["call_ids"],
        "source_parts": [part.model_dump(mode="json") for part in parts],
        "source_field": "content.requested_source.content",
        "excluded_receipts_are_already_executed": True,
        "partial_text_is_not_a_new_tool_result": True,
    }
    if any(manifest["source_captures"][part.source_ref].get("source_field") for part in parts):
        frame.pop("source_field")
        frame.update(
            schema="quoted-completed-source-fragments/v2",
            source_fields=[{
                "source_ref": part.source_ref,
                "field": manifest["source_captures"][part.source_ref].get(
                    "source_field", "content.requested_source.content"
                ),
            } for part in parts],
        )
    last = parts[-1]
    complete = refs[-1] == batch["receipt_refs"][-1] and last.stop_chars == last.total_chars
    return tuple(selected), frame, tuple(covered), complete


def validate_source_parts(state, manifest):
    """Check every acknowledged/pending range against its original source."""
    if (state.source_parts or state.pending_source_parts) and (
        state.source_fragment_policy != SOURCE_FRAGMENT_POLICY
    ):
        raise ContextRuntimeError("compaction_source_fragment_policy_changed")
    positions = {}
    chunks = {entry.chunk_index for entry in state.accepted}
    for pending, parts in (
        (False, state.source_parts), (True, state.pending_source_parts),
    ):
        for part in parts:
            capture = manifest.get("source_captures", {}).get(part.source_ref)
            if (
                capture is None or part.source_text_digest != capture["text_digest"]
                or part.total_chars != capture["total_chars"]
                or part.start_chars != positions.get(part.source_ref, 0)
                or not part.start_chars < part.stop_chars <= part.total_chars
                or (not pending and part.chunk_index not in chunks)
                or (pending and (state.pending is None
                                 or part.chunk_index != state.pending.chunk_index))
            ):
                raise ContextRuntimeError("compaction_source_fragment_gap_or_overlap")
            positions[part.source_ref] = part.stop_chars
    for ref in manifest["dropped"][: state.covered_count]:
        if ref in positions and positions[ref] != manifest["source_captures"][ref]["total_chars"]:
            raise ContextRuntimeError("compaction_source_fragment_incomplete")
