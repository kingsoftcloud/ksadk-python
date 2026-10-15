"""Durable host evidence for browser observations and interaction failures."""

from __future__ import annotations

import hashlib
import json

from pydantic import JsonValue

from .ports import ToolResult

_INTERACTIVE_ACTIONS = frozenset({"click", "fill", "press", "reload"})


def advance_browser_observation(
    previous: dict[str, JsonValue] | None,
    *,
    role: str,
    tool_call: int,
    result: ToolResult,
) -> dict[str, JsonValue] | None:
    """Remember only a successful screenshot from the host-bound browser port."""
    if role != "browser" or result.status != "ok" or not isinstance(result.content, dict):
        return previous
    content = result.content
    entry = content.get("entry_path")
    screenshot = content.get("screenshot_path")
    if (
        content.get("ok") is not True
        or not isinstance(entry, str)
        or not entry
        or not isinstance(screenshot, str)
        or not screenshot
    ):
        return previous
    receipt = json.dumps(
        {
            "entry_path": entry,
            "screenshot_path": screenshot,
            "browser_session_id": content.get("browser_session_id"),
            "browser_action_seq": content.get("browser_action_seq"),
            "tool_call": tool_call,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return {
        "entry_path": entry,
        "tool_call": tool_call,
        "evidence_ref": "sha256:" + hashlib.sha256(receipt).hexdigest(),
        "vision_description_status": str(content.get("vision_description_status") or "not_bound"),
    }


def advance_interaction_observation(
    previous: dict[str, JsonValue] | None,
    *,
    role: str,
    tool_name: str,
    tool_call: int,
    result: ToolResult,
) -> dict[str, JsonValue] | None:
    """Clear a page failure only after a successful action on the same entry.

    A later screenshot or host-only reload is insufficient: it can hide an
    error triggered by the user's original click or key press. The receipt is
    host supplied, and only a host-tagged browser port participates.
    """
    if role != "browser" or not isinstance(result.content, dict):
        return previous
    content = result.content
    entry = content.get("entry_path")
    if not isinstance(entry, str) or not entry:
        return previous
    errors = (
        item[:300]
        for key in ("page_errors", "console_errors", "request_failures")
        for group in (content.get(key),)
        if isinstance(group, list)
        for item in group
        if isinstance(item, str) and item
    )
    error = next(errors, None)
    if result.status == "error" and error is not None:
        receipt = json.dumps(
            {"entry_path": entry, "tool_call": tool_call, "content": content},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        return {
            "tool_name": tool_name,
            "entry_path": entry,
            "tool_call": tool_call,
            "error": error,
            "evidence_ref": "sha256:" + hashlib.sha256(receipt).hexdigest(),
        }
    actions = content.get("actions")
    interactive = isinstance(actions, list) and any(
        isinstance(action, dict) and action.get("type") in _INTERACTIVE_ACTIONS
        for action in actions
    )
    if (
        previous is not None
        and result.status == "ok"
        and content.get("ok") is True
        and content.get("actions_ok") is True
        and interactive
        and previous.get("tool_name") == tool_name
        and previous.get("entry_path") == entry
    ):
        return None
    return previous


__all__ = ["advance_browser_observation", "advance_interaction_observation"]
