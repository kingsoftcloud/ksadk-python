"""Fail-closed completion checks for managed parent/child execution."""

from __future__ import annotations

import re
from collections.abc import Iterable

from ksadk.harness.events import EventType, RuntimeEvent

_TERMINAL_CHILD_STATUSES = {
    "succeeded",
    "completed",
    "done",
    "failed",
    "error",
    "cancelled",
    "canceled",
}

_CONTINUATION_PREFACE = re.compile(
    r"(?:\b(?:let\s+me|i(?:'ll|\s+will|\s+am\s+going\s+to))\s+(?:now\s+)?"
    r"(?:read|call|check|inspect|search|create|write|fix|run|delegate|continue)\b|"
    r"(?:现在)?让我(?:先)?(?:读取|调用|检查|查看|检索|创建|编写|修复|运行|执行|委派|继续)|"
    r"(?:接下来|下一步)我(?:会|将|要)(?:先)?(?:读取|调用|检查|查看|检索|创建|编写|修复|运行|执行|委派|继续))",
    re.IGNORECASE,
)


def unfinished_delegations(events: Iterable[RuntimeEvent]) -> dict[str, str]:
    """Return dynamically delegated calls lacking a durable terminal event."""
    states: dict[str, tuple[str, str]] = {}
    for event in events:
        if event.event_type != EventType.RUN_PROGRESS:
            continue
        payload = event.payload
        if payload.get("kind") != "subagent.event":
            continue
        call_id = str(payload.get("call_id") or "").strip()
        if not call_id:
            continue
        status = str(payload.get("status") or "running").lower()
        label = str(payload.get("label") or call_id)
        states[call_id] = (status, label)
    return {
        call_id: label
        for call_id, (status, label) in states.items()
        if status not in _TERMINAL_CHILD_STATUSES
    }


def looks_like_unfinished_answer(text: str) -> bool:
    """Identify a provider answer that only announces its next tool action."""

    return bool(_CONTINUATION_PREFACE.search(text or ""))


__all__ = ["looks_like_unfinished_answer", "unfinished_delegations"]
