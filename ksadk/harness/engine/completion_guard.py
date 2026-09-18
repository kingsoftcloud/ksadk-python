"""Fail-closed completion checks for managed parent/child execution."""

from __future__ import annotations

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


__all__ = ["unfinished_delegations"]
