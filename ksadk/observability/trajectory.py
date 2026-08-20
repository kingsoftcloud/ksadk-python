"""Stable UI projection for canonical RuntimeEvents."""

from __future__ import annotations

import json
from typing import Any

from ksadk.events.runtime_event import EventType, RuntimeEvent

PROJECTION_VERSION = 1

_TOOL_EVENTS = {EventType.TOOL_CALL_BEGIN, EventType.TOOL_CALL_END}
_APPROVAL_EVENTS = {EventType.APPROVAL_REQUESTED, EventType.APPROVAL_RESOLVED}
_ARTIFACT_EVENTS = {EventType.ARTIFACT_CREATED, EventType.ARTIFACT_UPDATED}
_MODEL_EVENTS = {
    EventType.MODEL_CALL_BEGIN,
    EventType.MODEL_CALL_FIRST_TOKEN,
    EventType.MODEL_CALL_END,
}
_TEXT_EVENTS = {EventType.TEXT_DELTA, EventType.TEXT_COMPLETED}
_REASONING_EVENTS = {EventType.REASONING_DELTA, EventType.REASONING_COMPLETED}
_MESSAGE_EVENTS = _MODEL_EVENTS | _TEXT_EVENTS | _REASONING_EVENTS | {EventType.USAGE_REPORTED}
_TURN_EVENTS = {EventType.TURN_STARTED, EventType.TURN_COMPLETED}
_STEP_EVENTS = {EventType.STEP_STARTED, EventType.STEP_COMPLETED}
_CONTEXT_EVENTS = {
    EventType.REASONING_DELTA,
    EventType.REASONING_COMPLETED,
    EventType.CONTEXT_COMPACTION_STARTED,
    EventType.CONTEXT_COMPACTION_COMPLETED,
}


def _record_id(event: RuntimeEvent) -> str:
    if event.event_type == EventType.USER_MESSAGE:
        scope = event.payload.get("message_id") or event.event_id
        return f"user:{scope}"
    if event.event_type in _TOOL_EVENTS and event.payload.get("call_id"):
        return f"tool:{event.payload['call_id']}"
    if event.event_type in _MESSAGE_EVENTS:
        scope = (
            event.payload.get("model_call_id")
            or event.step_id
            or event.payload.get("message_id")
            or event.turn_id
            or event.invocation_id
        )
        return f"assistant:{scope}"
    if event.event_type in _CONTEXT_EVENTS:
        scope = event.step_id or event.turn_id or event.invocation_id
        return f"context:{scope}:compaction"
    if event.event_type in _APPROVAL_EVENTS and event.payload.get("approval_id"):
        return f"approval:{event.payload['approval_id']}"
    if event.event_type in _ARTIFACT_EVENTS and event.payload.get("name"):
        return f"artifact:{event.payload['name']}:{event.payload.get('version', '')}"
    if event.event_type in _TURN_EVENTS and event.turn_id:
        return f"turn:{event.turn_id}"
    if event.event_type in _STEP_EVENTS and event.step_id:
        return f"step:{event.step_id}"
    return f"system:{event.event_id}"


def _category(event_type: str) -> str:
    if event_type == EventType.USER_MESSAGE:
        return "user"
    if event_type in _TOOL_EVENTS:
        return "tool"
    if event_type in _MESSAGE_EVENTS:
        return "assistant"
    if event_type in _CONTEXT_EVENTS:
        return "context"
    if event_type in _APPROVAL_EVENTS:
        return "approval"
    if event_type in _ARTIFACT_EVENTS:
        return "artifact"
    return "system"


def _status(event: RuntimeEvent) -> str | None:
    payload_status = event.payload.get("status")
    if isinstance(payload_status, str) and payload_status:
        return payload_status
    if event.event_type.endswith((".begin", ".started", ".requested")):
        return "running"
    if event.event_type.endswith((".end", ".completed", ".resolved")):
        return "completed"
    if event.event_type == EventType.RUN_FAILED:
        return "failed"
    if event.event_type == EventType.RUN_CANCELED:
        return "canceled"
    if event.event_type == EventType.RUN_INTERRUPTED:
        return "interrupted"
    if event.event_type == EventType.MODEL_CALL_FIRST_TOKEN:
        return "running"
    return None


def _duration_ms(event: RuntimeEvent) -> float | int | None:
    value = event.payload.get("duration_ms")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _summary(event: RuntimeEvent) -> str:
    if event.event_type == EventType.USER_MESSAGE:
        return str(event.payload["text"])
    if event.event_type in _MESSAGE_EVENTS:
        return "Message"
    for key in ("name", "model", "summary", "text", "status"):
        value = event.payload.get(key)
        if isinstance(value, str) and value:
            return value
    return event.event_type


def project_trajectory_event(event: RuntimeEvent) -> dict[str, Any]:
    """Project one immutable fact into the versioned Studio display contract."""
    return {
        "projectionVersion": PROJECTION_VERSION,
        "seqId": event.seq_id,
        "eventId": event.event_id,
        "recordId": _record_id(event),
        "type": event.event_type,
        "category": _category(event.event_type),
        "turnId": event.turn_id,
        "stepId": event.step_id,
        "timestamp": event.timestamp,
        "status": _status(event),
        "durationMs": _duration_ms(event),
        "summary": _summary(event),
        "details": dict(event.payload),
        "source": event.to_dict(),
    }


def encode_sse(value: dict[str, Any], *, event_id: int) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"id: {event_id}\nevent: runtime_event\ndata: {data}\n\n"


__all__ = ["PROJECTION_VERSION", "encode_sse", "project_trajectory_event"]
