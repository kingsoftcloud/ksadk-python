"""Separate a canonical executed receipt from its host-selected model view.

Hashes detect inconsistent persisted projections, not hostile Store rewrites.
Only the reducer creates this top-level field from a locked tool policy. Tool
payload text cannot create it or acquire execution/verification authority.
"""

from __future__ import annotations

import hashlib
import json

from .model import ModelImageContent, ModelTextContent

VERSION = "canonical-tool-model-view/v1"
OBSERVED_HISTORY_VERSION = "canonical-tool-model-view/v2"
TYPED_HISTORY_VERSION = "canonical-tool-model-view/v3"


def executed_tool_message(
    result, *, call_id, definition=None, receipt_ref=None, goal_revision=0,
    tool_call=0, hook_metadata=None,
):
    """Use the same canonical observation for a single or completed batch member."""
    if result.model_observation is not None and (
        definition is None or definition.result_model_policy != result.model_observation.version
    ):
        raise ValueError("tool_model_policy_mismatch")
    canonical = (
        definition is not None
        and definition.result_archive_policy in {
            "archived-model-view/v3", "archived-model-view/v4", "archived-model-view/v5",
        }
        and result.status == "ok" and result.model_content is not None
    )
    message = {
        "role": "tool", "tool_call_id": call_id,
        "content": (
            result.model_content
            if result.status == "ok" and result.model_content is not None and not canonical
            else json.dumps(
                {"status": result.status, "content": result.content}, ensure_ascii=False,
            )
        ),
    }
    if receipt_ref is not None:
        message.update(receipt_ref=receipt_ref, receipt_goal_revision=goal_revision,
                       receipt_tool_call=tool_call)
    message.update(hook_metadata or {})
    if result.artifact_refs:
        message["artifact_refs"] = [ref.model_dump(mode="json") for ref in result.artifact_refs]
    if result.content_blocks:
        message["content"] = [
            {"type": "text", "text": message["content"]},
            *(block.model_dump(mode="json") for block in result.content_blocks),
        ]
    if canonical:
        message = seal_model_view(
            message, result.model_content,
            observed_history=definition.result_archive_policy in {
                "archived-model-view/v4", "archived-model-view/v5",
            },
        )
    return (seal_typed_model_view(message, result.model_observation)
            if result.model_observation is not None else message)


def append_executed_tool_message(messages, message):
    """Seal once, retaining declaration order independently of completion events."""
    insertion = len(messages)
    if message.get("receipt_ref") is not None:
        found = []
        for index in range(len(messages) - 1, -1, -1):
            item = messages[index]
            if item.get("role") == "assistant" and item.get("tool_calls"):
                break
            if (item.get("role") == "tool"
                    and item.get("receipt_ref") == message["receipt_ref"]
                    and item.get("tool_call_id") == message["tool_call_id"]):
                found.append(item)
            if (item.get("role") == "tool"
                    and item.get("receipt_ref") == message["receipt_ref"]
                    and item.get("receipt_tool_call", 0) > message.get("receipt_tool_call", 0)):
                insertion = min(insertion, index)
        if found:
            if len(found) != 1 or found[0] != message:
                raise ValueError("tool_member_observation_conflict")
            return messages
    return (*messages[:insertion], message, *messages[insertion:])


def ordered_tool_artifact_refs(refs, messages, receipt_ref, baseline):
    """Keep pre-batch references, then member attachments in declaration order."""
    if baseline is None:
        return refs  # An old intent does not authorize a reconstructed baseline.
    available = {ref["uri"]: ref for ref in refs}
    if any(uri not in available for uri in baseline):
        raise ValueError("parallel_artifact_baseline_missing")
    batch = []
    for item in reversed(messages):
        if item.get("role") == "assistant" and item.get("tool_calls"):
            break
        if item.get("role") == "tool" and item.get("receipt_ref") == receipt_ref:
            batch.append(item)
    order = dict.fromkeys((*baseline, *(
        ref["uri"] for item in reversed(batch) for ref in item.get("artifact_refs", ())
    ), *available))
    return tuple(available[uri] for uri in order)


def parallel_result_violation(result, *, delegated):
    usage = result.resource_usage
    return (
        result.status in {"approval_required", "uncertain"}
        or bool(result.workspace_changed_paths)
        or not delegated and bool(usage.model_calls or usage.tool_calls or usage.child_run_ids)
        or delegated and not usage.child_run_ids and bool(usage.model_calls)
    )


def _digest(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def seal_model_view(message, content, *, observed_history=False):
    return {
        **message,
        "model_view": {
            "version": OBSERVED_HISTORY_VERSION if observed_history else VERSION,
            "source_digest": _digest(message["content"]),
            "view_digest": _digest(content),
            "content": content,
        },
    }


def seal_typed_model_view(message, observation):
    content = observation.message_content()
    return {
        **message,
        "model_view": {
            "version": TYPED_HISTORY_VERSION,
            "source_digest": _digest(message["content"]),
            "view_digest": _digest(content),
            "content": content,
        },
    }


def _typed_model_content(content):
    if isinstance(content, str):
        blocks = ({"type": "text", "text": content},)
    elif isinstance(content, list) and content:
        blocks = tuple(content)
    else:
        raise ValueError("tool_model_view_invalid")
    validated = tuple(
        (ModelImageContent if block.get("type") == "image_url" else ModelTextContent)
        .model_validate(block).model_dump(mode="json")
        for block in blocks if isinstance(block, dict)
    )
    if len(validated) != len(blocks) or validated[0]["type"] != "text":
        raise ValueError("tool_model_view_invalid")
    text_bytes = sum(len(b["text"].encode()) for b in validated if b["type"] == "text")
    images = [b for b in validated if b["type"] == "image_url"]
    if not 0 < text_bytes <= 65536 or len(images) > 8:
        raise ValueError("tool_model_view_invalid")
    # Rehydration admits only the contract's absolute ceiling. Each live Run
    # already governed media against its own (possibly smaller) pinned limit.
    if sum(len(json.dumps(b, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode()) for b in images) > 64 * 1024 * 1024:
        raise ValueError("tool_model_view_invalid")
    return validated


def model_observation_message(message):
    """Project without I/O or mutating the authoritative receipt."""
    copied = json.loads(json.dumps(message, ensure_ascii=False))
    view = copied.pop("model_view", None)
    if view is None:
        return copied
    if isinstance(view, dict) and view.get("version") == TYPED_HISTORY_VERSION:
        try:
            if (
                copied.get("role") != "tool"
                or set(view) != {"version", "source_digest", "view_digest", "content"}
                or view["source_digest"] != _digest(copied.get("content"))
                or view["view_digest"] != _digest(view["content"])
            ):
                raise ValueError
            blocks = _typed_model_content(view["content"])
            source = json.loads(copied["content"])
            projected = json.loads(blocks[0]["text"])
            if (
                not isinstance(source, dict) or not isinstance(projected, dict)
                or source.get("status") not in {"ok", "error"}
                or projected.get("status") != source["status"]
                or set(source) != {"status", "content"}
                or set(projected) != {"status", "content"}
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ValueError("tool_model_view_invalid") from None
        copied["content"] = view["content"]
        return copied
    if (
        copied.get("role") != "tool"
        or not isinstance(view, dict)
        or set(view) != {"version", "source_digest", "view_digest", "content"}
        or view["version"] not in {VERSION, OBSERVED_HISTORY_VERSION}
        or not isinstance(view["content"], str)
        or not 0 < len(view["content"].encode("utf-8")) <= 65536
        or view["source_digest"] != _digest(copied.get("content"))
        or view["view_digest"] != _digest(view["content"])
    ):
        raise ValueError("tool_model_view_invalid")
    source = copied.get("content")
    if isinstance(source, list):
        if not source or source[0].get("type") != "text":
            raise ValueError("tool_model_view_invalid")
        source = source[0].get("text")
    try:
        original, projected = json.loads(source), json.loads(view["content"])
        if any(
            not isinstance(value, dict) or value.get("status") != "ok" or "content" not in value
            for value in (original, projected)
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("tool_model_view_invalid") from None
    # A textual alternate view cannot resurrect auxiliary oversized text. The
    # exact text stays canonical; explicit image blocks keep their own budget.
    blocks = copied.get("content")
    copied["content"] = (
        [{"type": "text", "text": view["content"]},
         *(block for block in blocks if block.get("type") == "image_url")]
        if isinstance(blocks, list) else view["content"]
    )
    return copied


def model_observation_messages(messages):
    return tuple(model_observation_message(message) for message in messages)


def history_observation_message(message):
    """The locked receipt policy selects its historical observation consumer.

    v1 deliberately quotes the canonical result. v2 quotes the complete
    bounded model observation, including unread ranges and artifact access;
    archiving raw text does not implicitly schedule that text for the model.
    """
    view = message.get("model_view")
    return (
        model_observation_message(message)
        if isinstance(view, dict)
        and view.get("version") in {OBSERVED_HISTORY_VERSION, TYPED_HISTORY_VERSION} else message
    )
