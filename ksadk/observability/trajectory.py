"""Stable UI trajectory projection for canonical RuntimeEvents.

The durable store owns only RuntimeEvent/v2 facts. This module derives its
compact trajectory shape from the Studio v2 projection; it must not reach into
the read-only RuntimeEvent/v1 compatibility model.
"""

from __future__ import annotations

import json
from typing import Any

from ksadk.events.canonical import RuntimeEvent, dump_runtime_event
from ksadk.studio.run_service import project_runtime_event

PROJECTION_VERSION = 1


def _record_id(event: RuntimeEvent, event_type: str, data: dict[str, Any]) -> str:
    if event_type.startswith(("message.", "thinking.")):
        return f"assistant:{data.get('itemId') or event.scope_id}"
    if event_type.startswith(("tool.", "command.")):
        return f"tool:{data.get('callId') or data.get('itemId') or event.event_id}"
    if event_type.startswith("approval."):
        return f"approval:{data.get('approvalId') or data.get('itemId') or event.event_id}"
    if event_type.startswith("checkpoint."):
        return f"checkpoint:{data.get('checkpointId') or data.get('itemId') or event.event_id}"
    if event_type.startswith("a2ui.surface."):
        return f"surface:{data.get('surfaceId') or data.get('itemId') or event.event_id}"
    if event_type.startswith("context.compaction."):
        return f"context:{event.scope_id}:compaction"
    return f"system:{event.event_id}"


def _category(event_type: str) -> str:
    if event_type.startswith(("message.", "thinking.")):
        return "assistant"
    if event_type.startswith(("tool.", "command.")):
        return "tool"
    if event_type.startswith("approval."):
        return "approval"
    if event_type.startswith("context.compaction."):
        return "context"
    if event_type.startswith("artifact."):
        return "artifact"
    return "system"


def _status(event_type: str, data: dict[str, Any]) -> str | None:
    value = data.get("status")
    if isinstance(value, str) and value:
        return value
    if event_type.endswith((".started", ".delta", ".requested", ".progress")):
        return "running"
    if event_type.endswith((".completed", ".resolved")):
        return "completed"
    if event_type.endswith(".failed"):
        return "failed"
    if event_type.endswith(".cancelled"):
        return "canceled"
    if event_type.endswith(".interrupted"):
        return "interrupted"
    return None


def _summary(event_type: str, data: dict[str, Any]) -> str:
    if event_type.startswith(("message.", "thinking.")):
        return "Message"
    for key in ("tool", "command", "message", "reason", "error"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return event_type


def project_trajectory_event(event: RuntimeEvent) -> dict[str, Any]:
    """Project one immutable v2 fact into the stable trajectory display shape."""

    event_type, data = project_runtime_event(event)
    details = dict(data)
    details.pop("runtimeEvent", None)
    return {
        "projectionVersion": PROJECTION_VERSION,
        "seqId": event.seq,
        "eventId": event.event_id,
        "recordId": _record_id(event, event_type, details),
        "type": event_type,
        "category": _category(event_type),
        "turnId": None,
        "stepId": None,
        "timestamp": event.timestamp,
        "status": _status(event_type, details),
        "durationMs": details.get("durationMs"),
        "summary": _summary(event_type, details),
        "details": details,
        "source": dump_runtime_event(event),
    }


def encode_sse(value: dict[str, Any], *, event_id: int) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"id: {event_id}\nevent: runtime_event\ndata: {data}\n\n"


__all__ = ["PROJECTION_VERSION", "encode_sse", "project_trajectory_event"]
