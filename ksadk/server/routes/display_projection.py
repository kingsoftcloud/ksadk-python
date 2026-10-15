"""Pure display projection for persisted session events."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .models import _truncate_session_text


def _first_text_part(parts: list[Any] | None) -> str:
    """Return the text of the first part with content_type == 'text'."""
    if not isinstance(parts, list):
        return ""
    for part in parts:
        if isinstance(part, Mapping) and part.get("content_type") == "text":
            return str(part.get("text") or "")
    return ""


def _first_tool_name(parts: list[Any] | None) -> str:
    """Return the name from the first tool_call part."""
    if not isinstance(parts, list) or not parts:
        return ""
    part = parts[0]
    if isinstance(part, Mapping):
        return str(part.get("name") or "")
    return ""


def _first_tool_result(parts: list[Any] | None) -> str:
    """Return the result of the first tool_result part as a string."""
    if not isinstance(parts, list) or not parts:
        return ""
    part = parts[0]
    if not isinstance(part, Mapping):
        return ""
    result = part.get("result")
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False)


def _derive_display_text(content: Mapping[str, Any]) -> str:
    """Derive a display string for Content.parts[0].text from a canonical runtime event.

    For canonical runtime events (family=runtime/v2) the Content dict carries a
    ``runtime_event`` payload but no top-level ``parts``.  This helper extracts a
    short human-readable string so that frontends reading ``Content.parts[0].text``
    continue to display meaningful text without changes.
    """
    runtime_event = content.get("runtime_event")
    if not isinstance(runtime_event, Mapping):
        return str(content.get("text") or "")

    event_type = str(runtime_event.get("event_type") or "")
    item_kind = str(runtime_event.get("item_kind") or "")

    if event_type == "item.updated":
        update = runtime_event.get("update")
        if not isinstance(update, Mapping):
            return ""
        if item_kind in ("message", "reasoning"):
            return str(update.get("text") or "")
        if item_kind == "tool_call":
            name = str(update.get("name") or "")
            return f"调用工具：{name}" if name else ""
        return ""

    if event_type == "item.started":
        initial = runtime_event.get("initial")
        parts = initial.get("parts") if isinstance(initial, Mapping) else None
        if item_kind == "tool_call":
            name = _first_tool_name(parts)
            return f"调用工具：{name}" if name else ""
        if item_kind in ("message", "reasoning"):
            return _first_text_part(parts)
        return ""

    if event_type == "item.completed":
        snapshot = runtime_event.get("snapshot")
        parts = snapshot.get("parts") if isinstance(snapshot, Mapping) else None
        if item_kind == "message":
            return _first_text_part(parts)
        if item_kind == "tool_result":
            result = _first_tool_result(parts)
            return _truncate_session_text(result) if result else ""
        if item_kind == "tool_call":
            name = _first_tool_name(parts)
            return f"工具调用完成：{name}" if name else ""
        return ""

    if event_type == "item.snapshot_replaced":
        snapshot = runtime_event.get("snapshot")
        parts = snapshot.get("parts") if isinstance(snapshot, Mapping) else None
        if item_kind in ("message", "reasoning"):
            return _first_text_part(parts)
        return ""

    if event_type == "item.failed":
        error = runtime_event.get("error")
        if isinstance(error, Mapping):
            return str(error.get("message") or "执行失败")
        return "执行失败"

    if event_type == "run.started":
        return "运行开始"
    if event_type == "run.progress":
        return str(runtime_event.get("message") or "")
    if event_type == "run.completed":
        return "运行完成"
    if event_type == "run.failed":
        error = runtime_event.get("error")
        if isinstance(error, Mapping):
            return str(error.get("message") or "运行失败")
        return "运行失败"
    if event_type == "run.interrupted":
        return str(runtime_event.get("reason") or "运行已中断")
    if event_type == "run.canceled":
        return str(runtime_event.get("reason") or "运行已取消")

    if event_type == "interaction.requested":
        request = runtime_event.get("request")
        if isinstance(request, Mapping):
            kind = str(request.get("kind") or "approval")
            return f"请求审批：{kind}"
        return "请求审批"

    if event_type == "interaction.resolved":
        response = runtime_event.get("response")
        if isinstance(response, Mapping):
            return str(response.get("decision") or "")
        return ""

    if event_type == "continuation.created":
        return f"创建恢复点：{runtime_event.get('continuation_kind') or ''}"
    if event_type == "continuation.resumed":
        return f"恢复执行：{runtime_event.get('continuation_kind') or ''}"

    if event_type == "context.compaction.started":
        return "上下文压缩开始"
    if event_type == "context.compaction.completed":
        return "上下文压缩完成"

    if event_type == "usage.reported":
        total = runtime_event.get("total_tokens")
        if total is not None:
            return f"用量上报：{total} tokens"
        return "用量上报"

    return ""
