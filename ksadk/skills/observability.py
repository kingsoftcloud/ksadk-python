"""OTEL projection for typed Skill Runtime facts."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ksadk.skills.events import SkillEvent

_LIFECYCLE_STARTS = {
    "skill.load.started": "skill.load",
    "skill.execution.started": "skill.execution",
    "sandbox.session.created": "sandbox.session",
}
_LIFECYCLE_ENDS = {
    "skill.load.completed": "skill.load",
    "skill.load.failed": "skill.load",
    "skill.execution.completed": "skill.execution",
    "skill.execution.failed": "skill.execution",
    "sandbox.session.cleaned_up": "sandbox.session",
    "sandbox.session.cleanup_failed": "sandbox.session",
}


def project_skill_events(events: Iterable[SkillEvent], *, tracer: Any | None = None) -> None:
    """Project facts beneath the current tool span without affecting execution."""

    try:
        from opentelemetry import trace
        from opentelemetry.trace import Status, StatusCode

        active_tracer = tracer or trace.get_tracer("ksadk.skills")
        parent = trace.get_current_span()
        lifecycle_starts: dict[tuple[str, str], SkillEvent] = {}
        for event in events:
            attributes = _span_attributes(event)
            lifecycle = _LIFECYCLE_STARTS.get(event.event_type)
            if lifecycle:
                lifecycle_starts[(lifecycle, _lifecycle_id(event))] = event
                continue
            lifecycle = _LIFECYCLE_ENDS.get(event.event_type)
            if lifecycle:
                start = lifecycle_starts.pop((lifecycle, _lifecycle_id(event)), event)
                _project_span(
                    active_tracer,
                    event,
                    attributes,
                    start.started_at,
                    event.started_at,
                    Status,
                    StatusCode,
                )
            elif event.ended_at is not None:
                _project_span(
                    active_tracer,
                    event,
                    attributes,
                    event.started_at,
                    event.ended_at,
                    Status,
                    StatusCode,
                )
            elif parent.is_recording():
                parent.add_event(
                    event.event_type,
                    attributes=attributes,
                    timestamp=_nanoseconds(event.started_at),
                )
    except Exception:
        return


def _project_span(
    tracer: Any,
    event: SkillEvent,
    attributes: dict[str, str | int | float | bool | list[str]],
    started_at: float,
    ended_at: float,
    Status: Any,
    StatusCode: Any,
) -> None:
    span = tracer.start_span(event.event_type, start_time=_nanoseconds(started_at))
    for key, value in attributes.items():
        span.set_attribute(key, value)
    if event.status in {"failed", "timeout", "timed_out", "cleanup_failed"}:
        span.set_status(Status(StatusCode.ERROR, event.error_category or event.status))
    span.end(end_time=_nanoseconds(ended_at))


def _lifecycle_id(event: SkillEvent) -> str:
    return event.skill_invocation_id or event.runtime_id


def _span_attributes(event: SkillEvent) -> dict[str, str | int | float | bool | list[str]]:
    attributes: dict[str, str | int | float | bool | list[str]] = {
        "ksadk.skill.event_type": event.event_type,
        "ksadk.skill.status": event.status,
    }
    for name, value in (
        ("event_id", event.event_id),
        ("invocation_id", event.skill_invocation_id),
        ("runtime_id", event.runtime_id),
        ("run_id", event.run_id),
        ("binding_snapshot_id", event.binding_snapshot_id),
        ("decision_id", event.decision_id),
        ("error_code", event.error_code),
        ("error_category", event.error_category),
    ):
        if value:
            attributes[f"ksadk.skill.{name}"] = value
    if event.skill_ref is not None:
        attributes["ksadk.skill.id"] = event.skill_ref.skill_id
        attributes["ksadk.skill.version_id"] = event.skill_ref.version_id
        attributes["ksadk.skill.version"] = event.skill_ref.version
        attributes["ksadk.skill.name"] = event.skill_ref.name
    for key, value in event.attributes.items():
        normalized = _attribute_value(value)
        if normalized is not None:
            attributes[f"ksadk.skill.{key}"] = normalized
    return attributes


def _attribute_value(value: Any) -> str | int | float | bool | list[str] | None:
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return list(value)
    return None


def _nanoseconds(timestamp: float) -> int:
    return int(timestamp * 1_000_000_000)
