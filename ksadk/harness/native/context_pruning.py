"""Bound historical tool-output projections without changing the transcript."""

from __future__ import annotations

import hashlib
import json

from pydantic import JsonValue

from .context_history import _historical_message

Messages = tuple[dict[str, JsonValue], ...]


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _excerpt_payload(text: str, limit: int, *, kind: str) -> dict[str, JsonValue]:
    head = limit // 2
    tail = limit - head
    return {
        "type": kind,
        "original_chars": len(text),
        "omitted_chars": len(text) - limit,
        "payload_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "head": text[:head],
        "tail": text[-tail:],
    }


def _excerpt(text: str, limit: int, *, kind: str = "historical_tool_result_excerpt") -> str:
    if len(text) <= limit:
        return text
    return _json(_excerpt_payload(text, limit, kind=kind))


def _fresh_line_preview(source: dict, max_chars: int) -> dict | None:
    """Keep a continuous prefix of a recognized line-reader response.

    A historical head/tail excerpt is not a fresh source page: its middle is
    missing and its tail may start inside a line. Derive the visible range from
    complete returned lines, keeping the executed source range separately.
    Never advance past a line whose content did not fit in the model view.
    """
    start, end, total = (source.get(key) for key in ("start_line", "end_line", "total_lines"))
    read_range = source.get("read_range")
    count = source.get("line_count")
    if (
        any(type(value) is not int for value in (start, end, total, count))
        or not 1 <= start <= end <= total
        or count != end - start + 1
        or not isinstance(read_range, dict)
        or any(type(read_range.get(key)) is not int for key in ("start", "end"))
        or read_range != {"start": start, "end": end}
    ):
        return None
    text = source["content"]
    prefix = text[:max_chars]
    boundary = prefix.rfind("\n")
    complete_lines = prefix[:boundary].count("\n") + 1 if boundary >= 0 else 0
    if complete_lines >= count:
        return None
    visible = prefix[:boundary] if complete_lines else prefix
    visible_end = start + complete_lines - 1 if complete_lines else None
    copied = {
        **source,
        "content": visible,
        "source_read_range": source.get("source_read_range", dict(read_range)),
        "end_line": visible_end,
        "read_range": {"start": start, "end": visible_end} if complete_lines else None,
        "line_count": complete_lines,
        "next_start_line": visible_end + 1 if complete_lines else None,
        "preview_chars": len(visible),
        "partial": True,
        "truncated": True,
        "context_projection": {
            "type": "workspace_read_prefix",
            "original_chars": len(text),
            "omitted_chars": len(text) - len(visible),
            "payload_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        },
    }
    if complete_lines:
        copied["suggested_action"] = (
            f"This context view contains complete lines {start}-{visible_end}; "
            f"continue reading at line {visible_end + 1} for omitted source."
        )
    else:
        copied["partial_line"] = start
        copied["suggested_action"] = (
            f"Only part of line {start} fits in this context view. No line continuation "
            "can recover its missing text; use a bound search or other permitted "
            "inspection tool for the remaining part of this line."
        )
    return copied


def project_workspace_read_result(
    message: dict[str, JsonValue], *, path: str, max_chars: int
) -> dict[str, JsonValue] | None:
    """Project a host-identified read for either main or summary context.

    The caller authorizes the reader and obtains its path from the actual call.
    A source page stays structured historical data with an honest visible range;
    clipping the serialized receipt instead would hide source facts behind
    envelope text while leaving a misleading full range in its prefix.
    """
    if type(max_chars) is not int or max_chars < 128:
        raise ValueError("workspace read limit must be at least 128 characters")
    if message.get("role") != "tool" or not isinstance(message.get("content"), str):
        return None
    try:
        receipt = json.loads(message["content"])
        source = receipt.get("content")
        if (
            not path
            or receipt.get("status") != "ok"
            or not isinstance(source, dict)
            or source.get("ok") is not True
            or source.get("path") != path
            or not isinstance(source.get("content"), str)
        ):
            return None
        if len(source["content"]) > max_chars:
            preview = _fresh_line_preview(source, max_chars)
            if preview is not None:
                receipt["content"] = preview
            else:
                source["content"] = json.loads(
                    _excerpt(source["content"], max_chars, kind="workspace_read_excerpt")
                )
                source["truncated"] = True
        return {**message, "content": _json(receipt)}
    except (ValueError, TypeError, AttributeError):
        return None


def project_fresh_workspace_reads(
    messages: Messages,
    *,
    reader_names: frozenset[str],
    max_chars: int,
    protected_call_ids: frozenset[str] = frozenset(),
) -> Messages | None:
    """Bound source reads throughout the active view, keeping complete batches.

    Only host-declared source readers are eligible: the original source remains
    accessible through the same bound reader. Other results in a mixed batch,
    failures and media stay intact. Rebuild every view from the full receipts,
    so the next model turn cannot resurrect an earlier oversized observation.
    """
    projected, clipped, index = list(messages), False, 0
    try:
        while index < len(messages):
            assistant = messages[index]
            calls = assistant.get("tool_calls")
            if assistant.get("role") == "tool":
                return None
            if assistant.get("role") != "assistant" or not calls:
                index += 1
                continue
            by_id = {call["id"]: call for call in calls}
            end = index + 1
            while end < len(messages) and messages[end].get("role") == "tool":
                end += 1
            results = messages[index + 1 : end]
            if len(by_id) != len(calls) or len(results) != len(calls):
                return None
            seen = set()
            for position, message in enumerate(results, index + 1):
                call_id = message.get("tool_call_id")
                if call_id not in by_id or call_id in seen:
                    return None
                seen.add(call_id)
                call = by_id[call_id]
                if call_id in protected_call_ids:
                    continue
                if call["function"]["name"] not in reader_names:
                    continue
                if not isinstance(message.get("content"), str):
                    continue
                path = json.loads(call["function"]["arguments"]).get("path")
                if not isinstance(path, str):
                    continue
                view = project_workspace_read_result(message, path=path, max_chars=max_chars)
                if view is not None and view["content"] != _json(json.loads(message["content"])):
                    clipped = True
                    projected[position] = view
            index = end
        return tuple(projected) if clipped else None
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def project_tool_result(message: dict[str, JsonValue], *, max_chars: int) -> dict[str, JsonValue]:
    """Return a detached historical projection with bounded textual output."""
    if type(max_chars) is not int or max_chars < 128:
        raise ValueError("historical tool result limit must be at least 128 characters")
    copied = json.loads(_json(message))
    if copied.get("role") != "tool":
        return copied
    content = copied.get("content")
    if isinstance(content, str):
        copied["content"] = _excerpt(content, max_chars)
        return copied
    if not isinstance(content, list):
        return copied
    text = "\n".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )
    media = [block for block in content if isinstance(block, dict) and block.get("type") != "text"]
    copied["content"] = (
        [{"type": "text", "text": _excerpt(text, max_chars)}] if text else []
    ) + media
    return copied


def project_historical_tool_result(
    message: dict[str, JsonValue], *, max_chars: int
) -> tuple[dict, tuple[dict, ...]]:
    """Quote host-generated excerpts as data without another JSON string layer.

    Derive the excerpt from the authoritative source and the same host limit.
    Tool text never proves its own projection type. Live model projection and
    canonical receipts keep their existing string/media contracts.
    """
    projected = project_tool_result(message, max_chars=max_chars)
    quoted, images = _historical_message(projected)
    if message.get("role") != "tool":
        return quoted, images
    source = message.get("content")
    if isinstance(source, str):
        text = source
    elif isinstance(source, list):
        text = "\n".join(
            block.get("text", "")
            for block in source
            if isinstance(block, dict) and block.get("type") == "text"
        )
    else:
        return quoted, images
    if len(text) <= max_chars:
        return quoted, images
    excerpt = _excerpt_payload(text, max_chars, kind="historical_tool_result_excerpt")
    # Historical message fields are quoted JSON, not executable model blocks.
    # For media, only the generated first text excerpt changes representation;
    # the explicit image references and pixels are kept by _historical_message.
    quoted["content"] = excerpt if isinstance(source, str) else [excerpt, *quoted["content"][1:]]
    return quoted, images


def clear_tool_result(
    projected: dict[str, JsonValue], *, source_message_ref: str, payload_digest: str
) -> dict[str, JsonValue]:
    """Clear an older quoted result while retaining identity and explicit media."""
    copied = json.loads(_json(projected))
    marker = _json(
        {
            "type": "historical_tool_result_cleared",
            "source_message_ref": source_message_ref,
            "payload_digest": payload_digest,
        }
    )
    content = copied.get("content")
    if isinstance(content, list):
        media = [
            block for block in content if isinstance(block, dict) and block.get("type") != "text"
        ]
        copied["content"] = [{"type": "text", "text": marker}, *media]
    else:
        copied["content"] = marker
    return copied
