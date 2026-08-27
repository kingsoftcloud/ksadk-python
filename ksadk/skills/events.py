"""Typed, redacted Skill Runtime observability facts."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from ksadk.skills.models import ContentHash, SkillRef

SKILL_EVENT_SCHEMA_VERSION = 1
SKILL_EVENT_FILE_ENV = "KSADK_SKILL_EVENT_FILE"

_EVENT_TYPES = frozenset(
    {
        "skill.candidates.resolved",
        "skill.selection.completed",
        "skill.selection.skipped",
        "skill.package.cache_hit",
        "skill.package.downloaded",
        "skill.package.hash_verified",
        "skill.package.extracted",
        "skill.manifest.parsed",
        "skill.load.started",
        "skill.load.completed",
        "skill.load.failed",
        "sandbox.session.created",
        "sandbox.session.cleaned_up",
        "sandbox.session.cleanup_failed",
        "skill.execution.started",
        "skill.execution.completed",
        "skill.execution.failed",
        "skill.artifact.created",
        "skill.result.consumed",
        "sandbox.envelope.rejected",
    }
)
_INVOCATION_EVENTS = frozenset(
    event_type
    for event_type in _EVENT_TYPES
    if event_type.startswith("skill.load.")
    or event_type.startswith("skill.execution.")
    or event_type in {"skill.artifact.created", "skill.result.consumed"}
)
_ALLOWED_ATTRIBUTE_KEYS = frozenset(
    {
        "artifact_name",
        "artifact_ref",
        "agent_step_id",
        "availability",
        "cache_hit",
        "candidate_count",
        "candidate_skill_ids",
        "has_description",
        "mime_type",
        "reason",
        "selected_skill_ids",
        "selection_receipt_id",
        "size_bytes",
    }
)


@dataclass(frozen=True)
class BoundSkillRef:
    skill_ref: SkillRef
    space_id: str


@dataclass(frozen=True)
class SkillBinding:
    binding_snapshot_id: str
    candidates: tuple[BoundSkillRef, ...]

    def __post_init__(self) -> None:
        if not self.binding_snapshot_id:
            raise ValueError("binding_snapshot_id is required")


@dataclass(frozen=True)
class PlannedSkillInvocation:
    skill_ref: SkillRef
    skill_invocation_id: str

    def __post_init__(self) -> None:
        if not self.skill_ref.skill_id or not self.skill_ref.version_id:
            raise ValueError("planned SkillRef requires skill_id and version_id")
        if not self.skill_invocation_id:
            raise ValueError("skill_invocation_id is required")


@dataclass(frozen=True)
class SkillInvocationPlan:
    binding_snapshot_id: str
    decision_id: str
    entries: tuple[PlannedSkillInvocation, ...]

    def __post_init__(self) -> None:
        if not self.binding_snapshot_id:
            raise ValueError("binding_snapshot_id is required")
        ids = [entry.skill_invocation_id for entry in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("skill_invocation_id values must be unique")


@dataclass(frozen=True)
class SkillExecutionContext:
    run_id: str = ""
    trace_id: str = ""
    binding: SkillBinding | None = None
    decision_id: str = ""
    selected_skill_ids: tuple[str, ...] = ()
    agent_step_id: str = ""
    selection_receipt_id: str = ""


def build_skill_invocation_plan(
    binding: SkillBinding,
    *,
    selected_skill_ids: tuple[str, ...] | list[str],
    decision_id: str = "",
) -> SkillInvocationPlan:
    """Create a trusted per-request plan from an immutable candidate snapshot."""

    candidates = {
        candidate.skill_ref.skill_id: candidate.skill_ref for candidate in binding.candidates
    }
    selected = tuple(str(skill_id) for skill_id in selected_skill_ids)
    if len(selected) != len(set(selected)):
        raise ValueError("selected skill IDs must be unique")
    entries: list[PlannedSkillInvocation] = []
    for skill_id in selected:
        skill_ref = candidates.get(skill_id)
        if skill_ref is None:
            raise ValueError(f"selected skill {skill_id!r} is not present in SkillBinding")
        entries.append(
            PlannedSkillInvocation(
                skill_ref=skill_ref,
                skill_invocation_id=f"skill_inv_{uuid.uuid4().hex}",
            )
        )
    return SkillInvocationPlan(
        binding_snapshot_id=binding.binding_snapshot_id,
        decision_id=decision_id,
        entries=tuple(entries),
    )


@dataclass(frozen=True)
class SkillEvent:
    schema_version: int
    event_id: str
    event_type: str
    status: str
    started_at: float
    ended_at: float | None = None
    skill_ref: SkillRef | None = None
    skill_invocation_id: str = ""
    runtime_id: str = ""
    trace_id: str = ""
    run_id: str = ""
    binding_snapshot_id: str = ""
    decision_id: str = ""
    attributes: Mapping[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error_category: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != SKILL_EVENT_SCHEMA_VERSION:
            raise ValueError(f"unsupported SkillEvent schema_version: {self.schema_version}")
        if self.event_type not in _EVENT_TYPES:
            raise ValueError(f"unknown SkillEvent event_type: {self.event_type}")
        if not self.status:
            raise ValueError("SkillEvent status is required")
        if self.event_type in _INVOCATION_EVENTS and not self.skill_invocation_id:
            raise ValueError(f"skill_invocation_id is required for {self.event_type}")

    @classmethod
    def create(
        cls,
        event_type: str,
        *,
        status: str,
        skill_ref: SkillRef | None = None,
        skill_invocation_id: str = "",
        runtime_id: str = "",
        trace_id: str = "",
        run_id: str = "",
        binding_snapshot_id: str = "",
        decision_id: str = "",
        attributes: Mapping[str, Any] | None = None,
        error_code: str = "",
        error_category: str = "",
        started_at: float | None = None,
        ended_at: float | None = None,
        event_id: str | None = None,
    ) -> "SkillEvent":
        return cls(
            schema_version=SKILL_EVENT_SCHEMA_VERSION,
            event_id=event_id or f"skill_evt_{uuid.uuid4().hex}",
            event_type=event_type,
            status=status,
            started_at=time.time() if started_at is None else started_at,
            ended_at=ended_at,
            skill_ref=skill_ref,
            skill_invocation_id=skill_invocation_id,
            runtime_id=runtime_id,
            trace_id=trace_id,
            run_id=run_id,
            binding_snapshot_id=binding_snapshot_id,
            decision_id=decision_id,
            attributes=dict(attributes or {}),
            error_code=error_code,
            error_category=error_category,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "skill_ref": _skill_ref_to_dict(self.skill_ref),
            "skill_invocation_id": self.skill_invocation_id,
            "runtime_id": self.runtime_id,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "binding_snapshot_id": self.binding_snapshot_id,
            "decision_id": self.decision_id,
            "attributes": dict(self.attributes),
            "error_code": self.error_code,
            "error_category": self.error_category,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SkillEvent":
        raw_skill_ref = payload.get("skill_ref")
        return cls(
            schema_version=int(payload.get("schema_version", 0)),
            event_id=str(payload.get("event_id") or ""),
            event_type=str(payload.get("event_type") or ""),
            status=str(payload.get("status") or ""),
            started_at=float(payload.get("started_at", 0)),
            ended_at=_optional_float(payload.get("ended_at")),
            skill_ref=_skill_ref_from_dict(raw_skill_ref),
            skill_invocation_id=str(payload.get("skill_invocation_id") or ""),
            runtime_id=str(payload.get("runtime_id") or ""),
            trace_id=str(payload.get("trace_id") or ""),
            run_id=str(payload.get("run_id") or ""),
            binding_snapshot_id=str(payload.get("binding_snapshot_id") or ""),
            decision_id=str(payload.get("decision_id") or ""),
            attributes=dict(payload.get("attributes") or {}),
            error_code=str(payload.get("error_code") or ""),
            error_category=str(payload.get("error_category") or ""),
        )


@dataclass(frozen=True)
class SandboxSkillEventEnvelope:
    event: SkillEvent

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SandboxSkillEventEnvelope":
        return cls(event=SkillEvent.from_dict(payload))

    def to_event(
        self,
        *,
        expected_skill_ref: SkillRef | None,
        expected_invocation_id: str,
    ) -> SkillEvent:
        event = self.event
        if expected_invocation_id and event.skill_invocation_id != expected_invocation_id:
            raise ValueError("sandbox envelope skill_invocation_id does not match outer invocation")
        if expected_skill_ref is not None and event.skill_ref != expected_skill_ref:
            raise ValueError("sandbox envelope SkillRef does not match outer invocation")
        return event


class SkillEventSink:
    """In-process event collector shared by Skill Runtime boundaries."""

    def __init__(self, event_file: str | Path | None = None) -> None:
        self.events: list[SkillEvent] = []
        self.event_file = Path(event_file) if event_file else None

    def emit(self, event: SkillEvent) -> SkillEvent:
        accepted = _redact_event(event)
        self.events.append(accepted)
        if self.event_file is not None:
            try:
                with self.event_file.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(accepted.to_dict(), separators=(",", ":")) + "\n")
            except OSError:
                pass
        return accepted


def apply_execution_context(event: SkillEvent, context: SkillExecutionContext) -> SkillEvent:
    """Replace untrusted request correlation with a trusted outer invocation."""

    return replace(
        event,
        trace_id=context.trace_id,
        run_id=context.run_id,
        binding_snapshot_id=context.binding.binding_snapshot_id if context.binding else "",
        decision_id=context.decision_id,
    )


def record_skill_result_consumption(
    result: Any,
    context: SkillExecutionContext,
    *,
    agent_step_id: str,
    skill_invocation_ids: tuple[str, ...] | list[str],
) -> list[SkillEvent]:
    """Create explicit consumption receipts from a durable Agent Runtime step."""

    observed = {
        event.skill_invocation_id: event
        for event in getattr(result, "skill_events", [])
        if event.event_type == "skill.execution.completed" and event.status == "completed"
    }
    receipts: list[SkillEvent] = []
    for invocation_id in skill_invocation_ids:
        event = observed.get(invocation_id)
        if event is None:
            receipts.append(
                SkillEvent.create(
                    "sandbox.envelope.rejected",
                    status="rejected",
                    error_category="invalid_consumption_receipt",
                )
            )
            continue
        receipts.append(
            apply_execution_context(
                SkillEvent.create(
                    "skill.result.consumed",
                    status="completed",
                    skill_ref=event.skill_ref,
                    skill_invocation_id=invocation_id,
                    attributes={
                        "agent_step_id": agent_step_id,
                        "selection_receipt_id": context.selection_receipt_id,
                    },
                ),
                context,
            )
        )
    return receipts


def read_sandbox_skill_events(
    path: str | Path,
    *,
    expected_invocations: Mapping[str, SkillRef] | None = None,
) -> list[SkillEvent]:
    """Read a sandbox sidecar without exposing malformed payload contents."""

    event_path = Path(path)
    if not event_path.exists():
        return []
    return parse_sandbox_skill_event_lines(
        event_path.read_text(encoding="utf-8", errors="replace"),
        expected_invocations=expected_invocations,
    )


def parse_sandbox_skill_event_lines(
    raw: str,
    *,
    expected_invocations: Mapping[str, SkillRef] | None = None,
) -> list[SkillEvent]:
    """Parse JSONL returned by a sandbox without exposing malformed payload contents."""

    events: list[SkillEvent] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = SandboxSkillEventEnvelope.from_dict(json.loads(line)).event
            if expected_invocations is not None and event.event_type in _INVOCATION_EVENTS:
                expected_ref = expected_invocations.get(event.skill_invocation_id)
                if expected_ref is None:
                    raise ValueError("sandbox envelope invocation is not in outer plan")
                event = SandboxSkillEventEnvelope(event).to_event(
                    expected_skill_ref=expected_ref,
                    expected_invocation_id=event.skill_invocation_id,
                )
            events.append(_redact_event(event))
        except (TypeError, ValueError, json.JSONDecodeError):
            events.append(
                SkillEvent.create(
                    "sandbox.envelope.rejected",
                    status="rejected",
                    error_category="invalid_envelope",
                )
            )
    return events


def _redact_event(event: SkillEvent) -> SkillEvent:
    attributes = {
        key: value for key, value in event.attributes.items() if key in _ALLOWED_ATTRIBUTE_KEYS
    }
    return SkillEvent(**{**event.__dict__, "attributes": attributes})


def _skill_ref_to_dict(skill_ref: SkillRef | None) -> dict[str, Any] | None:
    if skill_ref is None:
        return None
    return {
        "skill_id": skill_ref.skill_id,
        "version_id": skill_ref.version_id,
        "version": skill_ref.version,
        "name": skill_ref.name,
        "description": skill_ref.description,
        "status": skill_ref.status,
        "content_hash": skill_ref.content_hash.render() if skill_ref.content_hash else "",
        "aliases": list(skill_ref.aliases),
        "tags": list(skill_ref.tags),
    }


def _skill_ref_from_dict(raw: Any) -> SkillRef | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("SkillEvent skill_ref must be an object")
    return SkillRef(
        skill_id=str(raw.get("skill_id") or ""),
        version_id=str(raw.get("version_id") or ""),
        version=str(raw.get("version") or ""),
        name=str(raw.get("name") or ""),
        description=str(raw.get("description") or ""),
        status=str(raw.get("status") or ""),
        content_hash=ContentHash.parse(str(raw.get("content_hash") or "")),
        aliases=tuple(str(item) for item in raw.get("aliases") or ()),
        tags=tuple(str(item) for item in raw.get("tags") or ()),
    )


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)
