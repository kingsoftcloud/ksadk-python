"""Read-only RuntimeEvent v1 wire compatibility.

This module is the only owner of the legacy v1 envelope, parser, and the
canonical-v2-to-v1 projection.  It is deliberately not a persistence or
source-adapter boundary.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from ksadk.events.canonical import (
    ContextCompactionCompleted,
    ContextCompactionStarted,
    ContinuationCreated,
    ContinuationResumed,
    EventPhase,
    InteractionRequested,
    InteractionResolved,
    ItemCompleted,
    ItemFailed,
    ItemSnapshotReplaced,
    ItemStarted,
    ItemUpdated,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RunProgress,
    RunStarted,
    RuntimeEvent,
    UsageReported,
)
from ksadk.events.content import (
    ArtifactContent,
    DataContent,
    TextContent,
    ToolCallContent,
    ToolResultContent,
)
from ksadk.events.reducer import ItemProjection, RunProjection

RuntimeEventV1ProjectionMode: TypeAlias = Literal["snapshot_only", "identity_replace"]


class V1ProjectionContextRequiredError(ValueError):
    """Raised when a lossless v1 projection needs compat-local context."""


@dataclass(frozen=True)
class A2UISurfaceProjectionRef:
    surface_id: str
    catalog: str | None = None


@dataclass(frozen=True)
class A2UIInteractionProjectionRef:
    surface_id: str
    block_id: str | None = None


@dataclass(frozen=True)
class A2ATaskProjectionRef:
    task_id: str
    origin: str


@dataclass(frozen=True)
class RuntimeEventV1ProjectionContext:
    """Ephemeral values absent from the canonical event envelope.

    These values are supplied by the v1 read boundary.  Framework adapters and
    the canonical store must not manufacture or persist them for this module.
    """

    agent_id: str
    user_id: str
    session_id: str
    projection: RunProjection | None
    a2ui_surfaces: Mapping[tuple[str, str], A2UISurfaceProjectionRef] = field(default_factory=dict)
    a2ui_interactions: Mapping[tuple[str, str], A2UIInteractionProjectionRef] = field(
        default_factory=dict
    )
    a2a_tasks: Mapping[tuple[str, str], A2ATaskProjectionRef] = field(default_factory=dict)
    artifact_versions: Mapping[tuple[str, str, str], int] = field(default_factory=dict)
    compaction_phase: str = "runtime"

    @classmethod
    def from_projection(
        cls,
        projection: RunProjection | None,
        *,
        agent_id: str,
        user_id: str,
        session_id: str,
        a2ui_surfaces: Mapping[tuple[str, str], A2UISurfaceProjectionRef] | None = None,
        a2ui_interactions: Mapping[tuple[str, str], A2UIInteractionProjectionRef] | None = None,
        a2a_tasks: Mapping[tuple[str, str], A2ATaskProjectionRef] | None = None,
        artifact_versions: Mapping[tuple[str, str, str], int] | None = None,
        compaction_phase: str = "runtime",
    ) -> RuntimeEventV1ProjectionContext:
        return cls(
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            projection=projection,
            a2ui_surfaces=a2ui_surfaces or {},
            a2ui_interactions=a2ui_interactions or {},
            a2a_tasks=a2a_tasks or {},
            artifact_versions=artifact_versions or {},
            compaction_phase=compaction_phase,
        )

    def item(self, scope_id: str, item_id: str) -> ItemProjection | None:
        if self.projection is None:
            return None
        return next(
            (
                item
                for item in self.projection.items
                if item.scope_id == scope_id and item.item_id == item_id
            ),
            None,
        )

    def tool_name(self, scope_id: str, call_id: str) -> str:
        if self.projection is None:
            return ""
        for item in self.projection.items:
            if item.scope_id != scope_id:
                continue
            for part in item.parts:
                if isinstance(part, ToolCallContent) and part.call_id == call_id:
                    return part.name
        return ""

    def interaction_call_id(self, scope_id: str, interaction_id: str) -> str:
        if self.projection is None:
            return ""
        for interaction in self.projection.interactions:
            if (
                interaction.scope_id == scope_id
                and interaction.interaction_id == interaction_id
                and interaction.request.request_type == "approval"
            ):
                return interaction.request.call_id or ""
        return ""

    def artifact_version(self, scope_id: str, item_id: str, artifact_id: str) -> int:
        version = self.artifact_versions.get((scope_id, item_id, artifact_id))
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise V1ProjectionContextRequiredError(
                "artifact version must be an explicit positive integer"
            )
        return version


class EventTypeV1:
    TEXT_DELTA = "text.delta"
    TEXT_COMPLETED = "text.completed"
    REASONING_DELTA = "reasoning.delta"
    REASONING_COMPLETED = "reasoning.completed"
    TOOL_CALL_BEGIN = "tool.call.begin"
    TOOL_CALL_END = "tool.call.end"
    ARTIFACT_CREATED = "artifact.created"
    ARTIFACT_UPDATED = "artifact.updated"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    RUN_STARTED = "run.started"
    RUN_PROGRESS = "run.progress"
    RUN_INTERRUPTED = "run.interrupted"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELED = "run.canceled"
    CONTEXT_COMPACTION_STARTED = "context.compaction.started"
    CONTEXT_COMPACTION_COMPLETED = "context.compaction.completed"
    CHECKPOINT_CREATED = "checkpoint.created"
    CHECKPOINT_RESUMED = "checkpoint.resumed"
    USAGE_REPORTED = "usage.reported"
    A2UI_SURFACE_BEGIN = "a2ui.surface.begin"
    A2UI_SURFACE_UPDATE = "a2ui.surface.update"
    A2UI_SURFACE_END = "a2ui.surface.end"
    A2UI_INTERACTION = "a2ui.interaction"
    A2UI_ACTION = "a2ui.action"
    A2A_TASK_CREATED = "a2a.task.created"
    A2A_TASK_STATUS = "a2a.task.status"
    A2A_TASK_ARTIFACT = "a2a.task.artifact"


ALL_V1_EVENT_TYPES = frozenset(
    value for name, value in vars(EventTypeV1).items() if name.isupper() and isinstance(value, str)
)

V1_EVENT_PAYLOAD_REQUIRED_KEYS: dict[str, frozenset[str]] = {
    EventTypeV1.TEXT_DELTA: frozenset({"text"}),
    EventTypeV1.TEXT_COMPLETED: frozenset({"text"}),
    EventTypeV1.REASONING_DELTA: frozenset({"text"}),
    EventTypeV1.REASONING_COMPLETED: frozenset({"text"}),
    EventTypeV1.TOOL_CALL_BEGIN: frozenset({"call_id", "name"}),
    EventTypeV1.TOOL_CALL_END: frozenset({"call_id", "name"}),
    EventTypeV1.ARTIFACT_CREATED: frozenset({"name", "version"}),
    EventTypeV1.ARTIFACT_UPDATED: frozenset({"name", "version"}),
    EventTypeV1.APPROVAL_REQUESTED: frozenset({"approval_id", "call_id", "kind"}),
    EventTypeV1.APPROVAL_RESOLVED: frozenset({"approval_id", "call_id", "decision"}),
    EventTypeV1.RUN_STARTED: frozenset({"status"}),
    EventTypeV1.RUN_PROGRESS: frozenset({"status"}),
    EventTypeV1.RUN_INTERRUPTED: frozenset({"status"}),
    EventTypeV1.RUN_COMPLETED: frozenset({"status"}),
    EventTypeV1.RUN_FAILED: frozenset({"status", "error"}),
    EventTypeV1.RUN_CANCELED: frozenset({"status"}),
    EventTypeV1.CONTEXT_COMPACTION_STARTED: frozenset({"phase", "trigger"}),
    EventTypeV1.CONTEXT_COMPACTION_COMPLETED: frozenset(
        {"phase", "trigger", "compacted_until_seq_id"}
    ),
    EventTypeV1.CHECKPOINT_CREATED: frozenset({"checkpoint_id", "granularity"}),
    EventTypeV1.CHECKPOINT_RESUMED: frozenset({"checkpoint_id"}),
    EventTypeV1.USAGE_REPORTED: frozenset({"input_tokens", "output_tokens", "total_tokens"}),
    EventTypeV1.A2UI_SURFACE_BEGIN: frozenset({"surface_id"}),
    EventTypeV1.A2UI_SURFACE_UPDATE: frozenset({"surface_id"}),
    EventTypeV1.A2UI_SURFACE_END: frozenset({"surface_id"}),
    EventTypeV1.A2UI_INTERACTION: frozenset({"surface_id"}),
    EventTypeV1.A2UI_ACTION: frozenset({"surface_id"}),
    EventTypeV1.A2A_TASK_CREATED: frozenset({"task_id", "origin"}),
    EventTypeV1.A2A_TASK_STATUS: frozenset({"task_id", "origin", "status"}),
    EventTypeV1.A2A_TASK_ARTIFACT: frozenset({"task_id", "origin"}),
}

_V1_PHASE_AWARE_TYPES = frozenset(
    {
        EventTypeV1.TEXT_DELTA,
        EventTypeV1.TEXT_COMPLETED,
        EventTypeV1.REASONING_DELTA,
        EventTypeV1.REASONING_COMPLETED,
    }
)


class RuntimeEventV1(BaseModel):
    """Frozen RuntimeEvent v1 JSON envelope."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    event_id: str
    event_type: str
    timestamp: float
    agent_id: str
    user_id: str
    session_id: str
    invocation_id: str
    seq_id: int
    phase: Literal["commentary", "final_answer"] | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def create(
        cls,
        event_type: str,
        *,
        agent_id: str,
        user_id: str,
        session_id: str,
        invocation_id: str,
        seq_id: int,
        payload: dict[str, Any] | None = None,
        phase: str | None = None,
        event_id: str | None = None,
        timestamp: float | None = None,
    ) -> RuntimeEventV1:
        event = cls(
            event_id=event_id or f"evt_{uuid.uuid4().hex}",
            event_type=event_type,
            timestamp=time.time() if timestamp is None else timestamp,
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            invocation_id=invocation_id,
            seq_id=seq_id,
            phase=phase,  # type: ignore[arg-type]
            payload=payload or {},
        )
        event.validate_conformance()
        return event

    def validate_conformance(self) -> None:
        if self.event_type not in ALL_V1_EVENT_TYPES:
            raise ValueError(f"unknown event_type: {self.event_type!r} (v1 event family)")
        if self.phase is not None and self.event_type not in _V1_PHASE_AWARE_TYPES:
            raise ValueError(f"phase is only valid for v1 text/reasoning events: {self.event_type}")
        required = V1_EVENT_PAYLOAD_REQUIRED_KEYS.get(self.event_type, frozenset())
        missing = required - self.payload.keys()
        if missing:
            raise ValueError(
                f"event_type {self.event_type!r} payload missing required keys: {sorted(missing)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    def to_json(self) -> str:
        return self.model_dump_json(exclude_none=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RuntimeEventV1:
        event = cls.model_validate(data)
        event.validate_conformance()
        return event

    @classmethod
    def from_json(cls, raw: str) -> RuntimeEventV1:
        event = cls.model_validate_json(raw)
        event.validate_conformance()
        return event


_TextKey: TypeAlias = tuple[str, str] | tuple[str, str, str, str, str]
_ToolKey: TypeAlias = str | tuple[str, str, str]
_ArtifactKey: TypeAlias = str | tuple[str, str, str, str]
_TEXT_TYPES = frozenset({EventTypeV1.TEXT_DELTA, EventTypeV1.TEXT_COMPLETED})
_REASONING_TYPES = frozenset({EventTypeV1.REASONING_DELTA, EventTypeV1.REASONING_COMPLETED})
_RUN_TYPES = frozenset(
    {
        EventTypeV1.RUN_STARTED,
        EventTypeV1.RUN_PROGRESS,
        EventTypeV1.RUN_INTERRUPTED,
        EventTypeV1.RUN_COMPLETED,
        EventTypeV1.RUN_FAILED,
        EventTypeV1.RUN_CANCELED,
    }
)


class RuntimeEventV1Parser:
    """Fold v1 live/replay events with identity-aware replace semantics."""

    def __init__(self) -> None:
        self._seen_event_ids: set[str] = set()
        self._text: dict[_TextKey, dict[str, Any]] = {}
        self._reasoning: dict[_TextKey, dict[str, Any]] = {}
        self._tool_calls: dict[_ToolKey, dict[str, Any]] = {}
        self._artifacts: dict[_ArtifactKey, dict[str, Any]] = {}
        self._run_status: dict[str, str] = {}
        self._order: list[tuple[str, Any]] = []
        self._extras: list[dict[str, Any]] = []

    def feed(self, event: RuntimeEventV1) -> None:
        if event.event_id in self._seen_event_ids:
            return
        event.validate_conformance()
        event_type = event.event_type
        if event_type in _TEXT_TYPES:
            self._feed_text(
                self._text,
                "text",
                event,
                final=event_type == EventTypeV1.TEXT_COMPLETED,
            )
        elif event_type in _REASONING_TYPES:
            self._feed_text(
                self._reasoning,
                "reasoning",
                event,
                final=event_type == EventTypeV1.REASONING_COMPLETED,
            )
        elif event_type == EventTypeV1.TOOL_CALL_BEGIN:
            call_id = str(event.payload.get("call_id") or "")
            if call_id:
                tool_key = self._tool_key(event, call_id)
                if tool_key not in self._tool_calls:
                    self._order.append(("tool_call", tool_key))
                self._tool_calls[tool_key] = {
                    "call_id": call_id,
                    "name": event.payload.get("name", ""),
                    "detail": event.payload.get("detail") or {},
                    "done": False,
                    "invocation_id": event.invocation_id,
                    "scope_id": event.payload.get("scope_id"),
                    "item_id": event.payload.get("item_id"),
                    "part_id": event.payload.get("part_id"),
                }
        elif event_type == EventTypeV1.TOOL_CALL_END:
            call_id = str(event.payload.get("call_id") or "")
            if call_id:
                tool_key = self._tool_key(event, call_id)
                if tool_key not in self._tool_calls:
                    self._order.append(("tool_call", tool_key))
                    self._tool_calls[tool_key] = {
                        "call_id": call_id,
                        "name": event.payload.get("name", ""),
                        "detail": {},
                        "done": False,
                        "invocation_id": event.invocation_id,
                        "scope_id": event.payload.get("scope_id"),
                        "item_id": event.payload.get("item_id"),
                        "part_id": event.payload.get("part_id"),
                    }
                self._tool_calls[tool_key]["done"] = True
                self._tool_calls[tool_key]["result"] = event.payload.get("result")
        elif event_type in (EventTypeV1.ARTIFACT_CREATED, EventTypeV1.ARTIFACT_UPDATED):
            name = str(event.payload.get("name") or "artifact")
            artifact_key = self._artifact_key(event, name)
            previous = self._artifacts.get(artifact_key, {"version": 0})
            if artifact_key not in self._artifacts:
                self._order.append(("artifact", artifact_key))
            self._artifacts[artifact_key] = {
                "name": name,
                "version": int(event.payload.get("version") or previous["version"] + 1),
                "text": str(event.payload.get("text") or ""),
                "invocation_id": event.invocation_id,
                "scope_id": event.payload.get("scope_id"),
                "item_id": event.payload.get("item_id"),
                "part_id": event.payload.get("part_id"),
            }
        elif event_type in _RUN_TYPES:
            self._run_status[event.invocation_id] = str(event.payload.get("status") or event_type)
        else:
            self._extras.append(
                {
                    "event_type": event_type,
                    "invocation_id": event.invocation_id,
                    "payload": event.payload,
                }
            )
        self._seen_event_ids.add(event.event_id)

    @staticmethod
    def _identity_triplet(event: RuntimeEventV1) -> tuple[str, str, str] | None:
        values = tuple(event.payload.get(field) for field in ("scope_id", "item_id", "part_id"))
        has_any = any(value is not None for value in values)
        has_all = all(isinstance(value, str) and value for value in values)
        if has_any and not has_all:
            raise ValueError("identity-aware v1 events require scope_id, item_id, and part_id")
        if not has_all:
            return None
        return str(values[0]), str(values[1]), str(values[2])

    def _tool_key(self, event: RuntimeEventV1, call_id: str) -> _ToolKey:
        identity = self._identity_triplet(event)
        if identity is None:
            return call_id
        return event.invocation_id, identity[0], call_id

    def _artifact_key(self, event: RuntimeEventV1, name: str) -> _ArtifactKey:
        identity = self._identity_triplet(event)
        if identity is None:
            return name
        return event.invocation_id, identity[0], identity[1], identity[2]

    def _feed_text(
        self,
        bucket: dict[_TextKey, dict[str, Any]],
        kind: str,
        event: RuntimeEventV1,
        *,
        final: bool,
    ) -> None:
        phase = str(event.phase or "commentary")
        identity_values = tuple(
            event.payload.get(field) for field in ("scope_id", "item_id", "part_id")
        )
        has_any_identity = any(value is not None for value in identity_values)
        has_full_identity = all(isinstance(value, str) and value for value in identity_values)
        if has_any_identity and not has_full_identity:
            raise ValueError("identity-aware v1 text events require scope_id, item_id, and part_id")
        if has_full_identity:
            operation = event.payload.get("operation")
            if operation not in {"append", "replace"}:
                raise ValueError("identity-aware v1 text events require append/replace operation")
            key: _TextKey = (
                event.invocation_id,
                str(identity_values[0]),
                str(identity_values[1]),
                str(identity_values[2]),
                phase,
            )
        else:
            operation = "append"
            key = (event.invocation_id, phase)
        if key not in bucket:
            bucket[key] = {"text": "", "final": False}
            self._order.append((kind, key))
        entry = bucket[key]
        text = str(event.payload.get("text") or "")
        entry["text"] = entry["text"] + text if operation == "append" else text
        if final:
            entry["final"] = True

    def transcript(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for kind, key in self._order:
            if kind in {"text", "reasoning"}:
                bucket = self._text if kind == "text" else self._reasoning
                entry = bucket.get(key, {"text": "", "final": False})
                item = {
                    "kind": kind,
                    "invocation_id": key[0],
                    "phase": key[-1],
                    "text": entry["text"],
                    "final": entry["final"],
                }
                if len(key) == 5:
                    item.update({"scope_id": key[1], "item_id": key[2], "part_id": key[3]})
                items.append(item)
            elif kind == "tool_call":
                call = self._tool_calls.get(key, {})
                item = {
                    "kind": "tool_call",
                    "call_id": call.get("call_id", key),
                    "name": call.get("name", ""),
                    "done": call.get("done", False),
                    "result": call.get("result"),
                    "invocation_id": call.get("invocation_id"),
                }
                if isinstance(key, tuple):
                    item.update(
                        {
                            "scope_id": call.get("scope_id"),
                            "item_id": call.get("item_id"),
                            "part_id": call.get("part_id"),
                        }
                    )
                items.append(item)
            elif kind == "artifact":
                artifact = self._artifacts.get(key, {})
                item = {
                    "kind": "artifact",
                    "name": artifact.get("name", key),
                    "version": artifact.get("version", 1),
                    "text": artifact.get("text", ""),
                    "invocation_id": artifact.get("invocation_id"),
                }
                if isinstance(key, tuple):
                    item.update(
                        {
                            "scope_id": artifact.get("scope_id"),
                            "item_id": artifact.get("item_id"),
                            "part_id": artifact.get("part_id"),
                        }
                    )
                items.append(item)
        return {
            "items": items,
            "run_status": {key: self._run_status[key] for key in sorted(self._run_status)},
            "extras": self._extras,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.transcript(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )


def _phase_for_item(
    event: ItemStarted | ItemUpdated | ItemCompleted,
    context: RuntimeEventV1ProjectionContext | None,
) -> EventPhase:
    if event.item_kind == "reasoning":
        return "commentary"
    if event.item_kind != "message":
        raise ValueError(f"item kind {event.item_kind!r} has no v1 text phase")
    if isinstance(event, ItemStarted) and event.phase is not None:
        return event.phase
    item = context.item(event.scope_id, event.item_id) if context else None
    phase = item.phase if item is not None else None
    if phase is None:
        raise V1ProjectionContextRequiredError(
            "message phase requires RuntimeEventV1ProjectionContext"
        )
    return phase


def _source_event_id(event: RuntimeEvent) -> str:
    return event.source.native_event_id or event.event_id


def _identity_payload(
    event: RuntimeEvent,
    *,
    item_id: str | None = None,
    part_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "scope_id": event.scope_id,
        "source_event_id": _source_event_id(event),
    }
    if item_id is not None:
        payload["item_id"] = item_id
    if part_id is not None:
        payload["part_id"] = part_id
    return payload


def _artifact_payload(
    event: ItemStarted | ItemUpdated | ItemCompleted,
    part: ArtifactContent,
    context: RuntimeEventV1ProjectionContext | None,
) -> dict[str, Any]:
    if context is None:
        raise V1ProjectionContextRequiredError(
            "artifact version requires RuntimeEventV1ProjectionContext"
        )
    return {
        "name": part.name,
        "version": context.artifact_version(event.scope_id, event.item_id, part.artifact_id),
        "uri": part.uri,
        "mime": part.mime_type,
        "data": part.data,
        **_identity_payload(event, item_id=event.item_id, part_id=part.part_id),
    }


def _a2a_task_ref(
    event: RuntimeEvent,
    context: RuntimeEventV1ProjectionContext | None,
) -> A2ATaskProjectionRef | None:
    if event.source.framework != "a2a":
        return None
    ref = context.a2a_tasks.get((event.run_id, event.scope_id)) if context else None
    if ref is None or not ref.task_id.strip() or not ref.origin.strip():
        raise V1ProjectionContextRequiredError(
            "A2A task projection requires nonempty task_id and origin"
        )
    return ref


def _a2ui_surface_ref(
    event: ItemStarted | ItemUpdated | ItemSnapshotReplaced | ItemCompleted,
    context: RuntimeEventV1ProjectionContext | None,
) -> A2UISurfaceProjectionRef | None:
    ref = context.a2ui_surfaces.get((event.scope_id, event.item_id)) if context else None
    if ref is not None and not ref.surface_id.strip():
        raise V1ProjectionContextRequiredError(
            "A2UI surface projection requires a nonempty surface_id"
        )
    return ref


def _a2ui_interaction_ref(
    event: InteractionRequested | InteractionResolved,
    context: RuntimeEventV1ProjectionContext | None,
) -> A2UIInteractionProjectionRef | None:
    ref = context.a2ui_interactions.get((event.scope_id, event.interaction_id)) if context else None
    if ref is not None and not ref.surface_id.strip():
        raise V1ProjectionContextRequiredError(
            "A2UI interaction projection requires a nonempty surface_id"
        )
    return ref


def _project_artifact_parts(
    event: ItemStarted | ItemUpdated | ItemCompleted,
    parts: tuple[ArtifactContent, ...],
    *,
    generic_event_type: str,
    context: RuntimeEventV1ProjectionContext | None,
) -> tuple[RuntimeEventV1, ...]:
    a2a_ref = _a2a_task_ref(event, context)
    event_type = EventTypeV1.A2A_TASK_ARTIFACT if a2a_ref is not None else generic_event_type
    projected: list[RuntimeEventV1] = []
    for ordinal, part in enumerate(parts):
        artifact = _artifact_payload(event, part, context)
        payload = (
            {
                "task_id": a2a_ref.task_id,
                "origin": a2a_ref.origin,
                "artifact": artifact,
                **_identity_payload(event, item_id=event.item_id, part_id=part.part_id),
            }
            if a2a_ref is not None
            else artifact
        )
        projected.append(
            _v1_event(
                event,
                event_type,
                payload,
                context=context,
                ordinal=ordinal,
            )
        )
    return tuple(projected)


def _v1_event(
    event: RuntimeEvent,
    event_type: str,
    payload: dict[str, Any],
    *,
    context: RuntimeEventV1ProjectionContext | None,
    phase: EventPhase | None = None,
    ordinal: int = 0,
    identity_item_id: str | None = None,
    identity_part_id: str | None = None,
) -> RuntimeEventV1:
    if context is None or not all(
        value.strip() for value in (context.agent_id, context.user_id, context.session_id)
    ):
        raise V1ProjectionContextRequiredError(
            "v1 output requires a complete nonempty envelope context"
        )
    if context.projection is not None and context.projection.run_id != event.run_id:
        raise V1ProjectionContextRequiredError(
            "RuntimeEventV1ProjectionContext projection run_id must match event run_id"
        )
    item_id = identity_item_id or payload.get("item_id") or ""
    part_id = identity_part_id or payload.get("part_id") or ""
    identity = json.dumps(
        [event.event_id, ordinal, item_id, part_id, event_type],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    legacy_event_id = f"evt_v1_{hashlib.sha256(identity).hexdigest()[:32]}"
    projected = RuntimeEventV1(
        event_id=legacy_event_id,
        event_type=event_type,
        timestamp=event.timestamp,
        agent_id=context.agent_id,
        user_id=context.user_id,
        session_id=context.session_id,
        invocation_id=event.run_id,
        seq_id=event.seq,
        phase=phase,
        payload=payload,
    )
    projected.validate_conformance()
    return projected


def _project_text_item(
    event: ItemStarted | ItemUpdated | ItemCompleted,
    *,
    mode: RuntimeEventV1ProjectionMode,
    context: RuntimeEventV1ProjectionContext | None,
) -> tuple[RuntimeEventV1, ...]:
    if event.item_kind not in {"message", "reasoning"}:
        return ()
    if mode == "snapshot_only":
        return ()
    parts: tuple[TextContent, ...]
    if isinstance(event, ItemUpdated):
        if not isinstance(event.update, TextContent):
            return ()
        parts = (event.update,)
        completed = False
        operation = event.op
    elif isinstance(event, ItemCompleted):
        parts = tuple(part for part in event.snapshot.parts if isinstance(part, TextContent))
        completed = True
        operation = "replace"
    else:
        if event.initial is None:
            return ()
        parts = tuple(part for part in event.initial.parts if isinstance(part, TextContent))
        completed = False
        operation = "replace"
    if not parts:
        return ()
    phase = _phase_for_item(event, context)
    prefix = "reasoning" if event.item_kind == "reasoning" else "text"
    event_type = f"{prefix}.completed" if completed else f"{prefix}.delta"
    projected: list[RuntimeEventV1] = []
    for ordinal, part in enumerate(parts):
        payload = {
            "text": part.text,
            **_identity_payload(event, item_id=event.item_id, part_id=part.part_id),
            "operation": operation,
        }
        projected.append(
            _v1_event(
                event,
                event_type,
                payload,
                context=context,
                phase=phase,
                ordinal=ordinal,
            )
        )
    return tuple(projected)


def _project_item_started(
    event: ItemStarted,
    *,
    mode: RuntimeEventV1ProjectionMode,
    context: RuntimeEventV1ProjectionContext | None,
) -> tuple[RuntimeEventV1, ...]:
    text_projection = _project_text_item(event, mode=mode, context=context)
    if text_projection or event.item_kind in {"message", "reasoning"}:
        return text_projection
    if event.item_kind == "data":
        ref = _a2ui_surface_ref(event, context)
        if ref is None:
            return ()
        data_parts = (
            tuple(part for part in event.initial.parts if isinstance(part, DataContent))
            if event.initial is not None
            else ()
        )
        payload = {
            "surface_id": ref.surface_id,
            "catalog": ref.catalog,
            "data": [part.data for part in data_parts],
            **_identity_payload(event, item_id=event.item_id),
        }
        return (_v1_event(event, EventTypeV1.A2UI_SURFACE_BEGIN, payload, context=context),)
    if event.item_kind == "artifact" and event.initial is not None:
        artifact_parts = tuple(
            part for part in event.initial.parts if isinstance(part, ArtifactContent)
        )
        return _project_artifact_parts(
            event,
            artifact_parts,
            generic_event_type=EventTypeV1.ARTIFACT_CREATED,
            context=context,
        )
    if event.item_kind != "tool_call" or event.initial is None:
        return ()
    tool_parts = tuple(part for part in event.initial.parts if isinstance(part, ToolCallContent))
    return tuple(
        _v1_event(
            event,
            EventTypeV1.TOOL_CALL_BEGIN,
            {
                "call_id": part.call_id,
                "name": part.name,
                "args": part.arguments,
                **_identity_payload(event, item_id=event.item_id, part_id=part.part_id),
            },
            context=context,
            ordinal=ordinal,
        )
        for ordinal, part in enumerate(tool_parts)
    )


def _project_item_updated(
    event: ItemUpdated,
    *,
    mode: RuntimeEventV1ProjectionMode,
    context: RuntimeEventV1ProjectionContext | None,
) -> tuple[RuntimeEventV1, ...]:
    text_projection = _project_text_item(event, mode=mode, context=context)
    if text_projection or event.item_kind in {"message", "reasoning"}:
        return text_projection
    if event.item_kind == "data":
        ref = _a2ui_surface_ref(event, context)
        if ref is None or not isinstance(event.update, DataContent):
            return ()
        payload = {
            "surface_id": ref.surface_id,
            "catalog": ref.catalog,
            "data": event.update.data,
            **_identity_payload(event, item_id=event.item_id, part_id=event.update.part_id),
        }
        return (_v1_event(event, EventTypeV1.A2UI_SURFACE_UPDATE, payload, context=context),)
    if event.item_kind != "artifact" or not isinstance(event.update, ArtifactContent):
        return ()
    return _project_artifact_parts(
        event,
        (event.update,),
        generic_event_type=EventTypeV1.ARTIFACT_UPDATED,
        context=context,
    )


def _project_item_completed(
    event: ItemCompleted,
    *,
    mode: RuntimeEventV1ProjectionMode,
    context: RuntimeEventV1ProjectionContext | None,
) -> tuple[RuntimeEventV1, ...]:
    text_projection = _project_text_item(event, mode=mode, context=context)
    if text_projection or event.item_kind in {"message", "reasoning"}:
        return text_projection
    if event.item_kind == "data":
        ref = _a2ui_surface_ref(event, context)
        if ref is None:
            return ()
        parts = tuple(part for part in event.snapshot.parts if isinstance(part, DataContent))
        payload = {
            "surface_id": ref.surface_id,
            "catalog": ref.catalog,
            "data": [part.data for part in parts],
            **_identity_payload(event, item_id=event.item_id),
        }
        return (_v1_event(event, EventTypeV1.A2UI_SURFACE_END, payload, context=context),)
    if event.item_kind == "tool_result":
        tool_result_parts = tuple(
            part for part in event.snapshot.parts if isinstance(part, ToolResultContent)
        )
        return tuple(
            _v1_event(
                event,
                EventTypeV1.TOOL_CALL_END,
                {
                    "call_id": part.call_id,
                    "name": (context.tool_name(event.scope_id, part.call_id) if context else ""),
                    "result": part.result,
                    "error": part.result if part.is_error else None,
                    **_identity_payload(event, item_id=event.item_id, part_id=part.part_id),
                },
                context=context,
                ordinal=ordinal,
            )
            for ordinal, part in enumerate(tool_result_parts)
        )
    if event.item_kind == "artifact":
        artifact_parts = tuple(
            part for part in event.snapshot.parts if isinstance(part, ArtifactContent)
        )
        return _project_artifact_parts(
            event,
            artifact_parts,
            generic_event_type=EventTypeV1.ARTIFACT_UPDATED,
            context=context,
        )
    return ()


def _project_item_snapshot_replaced(
    event: ItemSnapshotReplaced,
    *,
    mode: RuntimeEventV1ProjectionMode,
    context: RuntimeEventV1ProjectionContext | None,
) -> tuple[RuntimeEventV1, ...]:
    """Project only snapshots with an existing lossless v1 item-level meaning."""

    if event.item_kind in {"message", "reasoning"}:
        if mode == "snapshot_only":
            return ()
        raise V1ProjectionContextRequiredError(
            "identity_replace cannot represent an item-level snapshot replacement "
            "without leaving stale or reordered v1 text parts"
        )

    if event.item_kind == "data" and event.source.protocol == "a2ui":
        ref = _a2ui_surface_ref(event, context)
        if ref is None:
            raise V1ProjectionContextRequiredError(
                "A2UI item-level snapshot requires a typed surface projection ref"
            )
        raise V1ProjectionContextRequiredError(
            "v1 A2UI updates cannot represent an item-level snapshot replacement atomically"
        )

    # A2A and artifact projection identities must still be validated before the
    # legacy boundary rejects a snapshot it cannot express atomically.
    _a2a_task_ref(event, context)
    if event.item_kind == "artifact":
        if context is None:
            raise V1ProjectionContextRequiredError(
                "artifact item-level snapshot requires typed projection context"
            )
        for part in event.snapshot.parts:
            if not isinstance(part, ArtifactContent):
                raise V1ProjectionContextRequiredError(
                    "artifact item-level snapshot contains incompatible content"
                )
            context.artifact_version(event.scope_id, event.item_id, part.artifact_id)
        raise V1ProjectionContextRequiredError(
            "v1 artifact events cannot represent an item-level snapshot replacement atomically"
        )

    if mode == "identity_replace":
        raise V1ProjectionContextRequiredError(
            f"v1 cannot represent an item-level snapshot replacement for {event.item_kind!r}"
        )
    return ()


def _snapshot_output_events(
    event: RunCompleted,
    context: RuntimeEventV1ProjectionContext | None,
) -> tuple[RuntimeEventV1, ...]:
    if context is None or context.projection is None:
        raise V1ProjectionContextRequiredError(
            "snapshot_only run completion requires reducer RunProjection"
        )
    if context.projection.run_id != event.run_id:
        raise V1ProjectionContextRequiredError(
            "RunProjection run_id must match run.completed run_id"
        )
    if context.projection.status != "completed":
        raise V1ProjectionContextRequiredError(
            "RunProjection status must be completed for snapshot_only output"
        )
    if context.projection.output_refs != event.output_refs:
        raise V1ProjectionContextRequiredError(
            "RunProjection output_refs must match run.completed output_refs"
        )
    projected: list[RuntimeEventV1] = []
    ordinal = 0
    for output_ref in event.output_refs:
        item = context.item(output_ref.scope_id, output_ref.item_id)
        if item is None:
            raise V1ProjectionContextRequiredError(
                "RunProjection is missing a run.completed output_ref item"
            )
        if item.item_kind not in {"message", "reasoning"}:
            continue
        if item.item_kind == "reasoning":
            phase: EventPhase = "commentary"
            event_type = EventTypeV1.REASONING_COMPLETED
        else:
            if item.phase is None:
                raise V1ProjectionContextRequiredError(
                    "message phase is missing from reducer RunProjection"
                )
            phase = item.phase
            event_type = EventTypeV1.TEXT_COMPLETED
        parts = tuple(part for part in item.parts if isinstance(part, TextContent))
        if output_ref.part_id is not None:
            parts = tuple(part for part in parts if part.part_id == output_ref.part_id)
            if not parts:
                raise V1ProjectionContextRequiredError(
                    "RunProjection is missing a run.completed output_ref part"
                )
        for part in parts:
            projected.append(
                _v1_event(
                    event,
                    event_type,
                    {"text": part.text},
                    context=context,
                    phase=phase,
                    ordinal=ordinal,
                    identity_item_id=item.item_id,
                    identity_part_id=part.part_id,
                )
            )
            ordinal += 1
    return tuple(projected)


def _project_run_event(
    event: RuntimeEvent,
    *,
    mode: RuntimeEventV1ProjectionMode,
    context: RuntimeEventV1ProjectionContext | None,
) -> tuple[RuntimeEventV1, ...] | None:
    event_type: str
    payload: dict[str, Any]
    a2a_ref = _a2a_task_ref(event, context)
    snapshot_events: tuple[RuntimeEventV1, ...] = ()
    if isinstance(event, RunCompleted) and mode == "snapshot_only":
        snapshot_events = _snapshot_output_events(event, context)
    if a2a_ref is not None:
        if isinstance(event, RunStarted):
            event_type = EventTypeV1.A2A_TASK_CREATED
            payload = {
                "task_id": a2a_ref.task_id,
                "origin": a2a_ref.origin,
                "status": event.status,
            }
        elif isinstance(
            event,
            (RunProgress, RunInterrupted, RunCompleted, RunFailed, RunCanceled),
        ):
            event_type = EventTypeV1.A2A_TASK_STATUS
            payload = {
                "task_id": a2a_ref.task_id,
                "origin": a2a_ref.origin,
                "status": event.status,
            }
            if isinstance(event, RunFailed):
                payload["error"] = event.error.model_dump(mode="json")
        else:
            return None
        payload.update(_identity_payload(event))
        lifecycle = _v1_event(
            event,
            event_type,
            payload,
            context=context,
            ordinal=len(snapshot_events),
        )
        return (*snapshot_events, lifecycle)
    if isinstance(event, RunStarted):
        event_type, payload = EventTypeV1.RUN_STARTED, {"status": event.status}
    elif isinstance(event, RunProgress):
        event_type = EventTypeV1.RUN_PROGRESS
        payload = {"status": event.status, "progress": event.progress, "message": event.message}
    elif isinstance(event, RunInterrupted):
        event_type = EventTypeV1.RUN_INTERRUPTED
        payload = {
            "status": event.status,
            "reason": event.reason,
            "interaction_id": event.interaction_id,
            "continuation_id": event.continuation_id,
        }
    elif isinstance(event, RunCompleted):
        event_type = EventTypeV1.RUN_COMPLETED
        payload = {
            "status": event.status,
            "output_refs": [
                ref.model_dump(mode="json", exclude_none=True) for ref in event.output_refs
            ],
        }
    elif isinstance(event, RunFailed):
        event_type = EventTypeV1.RUN_FAILED
        payload = {"status": event.status, "error": event.error.model_dump(mode="json")}
    elif isinstance(event, RunCanceled):
        event_type = EventTypeV1.RUN_CANCELED
        payload = {"status": event.status, "reason": event.reason}
    else:
        return None
    payload.update(_identity_payload(event))
    lifecycle = _v1_event(
        event,
        event_type,
        payload,
        context=context,
        ordinal=len(snapshot_events),
    )
    return (*snapshot_events, lifecycle)


def project_to_v1(
    event: RuntimeEvent,
    *,
    mode: RuntimeEventV1ProjectionMode = "snapshot_only",
    context: RuntimeEventV1ProjectionContext | None = None,
) -> tuple[RuntimeEventV1, ...]:
    """Project one canonical event to zero or more legacy v1 wire events.

    公开承诺字段（契约声明见 ``ksadk/events/projections.py``，执行形态为
    ``tests/protocol/test_cross_projection_golden.py``）：
    - RuntimeEventV1 事件类型与各类型 payload（approval_id/call_id/kind/detail、
      surface_id/block_id/data、output_refs、status/error/reason 等）；
    - 身份字段 run_id/scope_id/item_id。

    内部不保证字段：seq/run_seq 的具体数值（仅保序）、source.native_* 游标、
    source.metadata 原始键值。消费方不得依赖未列出的 payload 附加键。
    """

    if mode not in {"snapshot_only", "identity_replace"}:
        raise ValueError(f"unknown RuntimeEvent v1 projection mode: {mode!r}")
    if (
        context is not None
        and context.projection is not None
        and context.projection.run_id != event.run_id
    ):
        raise V1ProjectionContextRequiredError(
            "RuntimeEventV1ProjectionContext projection run_id must match event run_id"
        )

    run_projection = _project_run_event(event, mode=mode, context=context)
    if run_projection is not None:
        return run_projection
    if isinstance(event, ItemStarted):
        return _project_item_started(event, mode=mode, context=context)
    if isinstance(event, ItemUpdated):
        return _project_item_updated(event, mode=mode, context=context)
    if isinstance(event, ItemSnapshotReplaced):
        return _project_item_snapshot_replaced(event, mode=mode, context=context)
    if isinstance(event, ItemCompleted):
        return _project_item_completed(event, mode=mode, context=context)
    if isinstance(event, ItemFailed):
        return ()
    if isinstance(event, InteractionRequested):
        a2ui_ref = _a2ui_interaction_ref(event, context)
        if a2ui_ref is not None:
            payload = {
                "surface_id": a2ui_ref.surface_id,
                "block_id": a2ui_ref.block_id,
                "data": event.request.model_dump(mode="json", by_alias=True),
                **_identity_payload(event, item_id=event.interaction_id),
            }
            return (_v1_event(event, EventTypeV1.A2UI_INTERACTION, payload, context=context),)
        if event.interaction_kind != "approval" or event.request.request_type != "approval":
            return ()
        call_id = event.request.call_id or (
            context.interaction_call_id(event.scope_id, event.interaction_id) if context else ""
        )
        payload = {
            "approval_id": event.interaction_id,
            "call_id": call_id,
            "kind": event.request.kind,
            "detail": event.request.detail,
            **_identity_payload(event, item_id=event.interaction_id),
        }
        return (_v1_event(event, EventTypeV1.APPROVAL_REQUESTED, payload, context=context),)
    if isinstance(event, InteractionResolved):
        a2ui_ref = _a2ui_interaction_ref(event, context)
        if a2ui_ref is not None:
            payload = {
                "surface_id": a2ui_ref.surface_id,
                "block_id": a2ui_ref.block_id,
                "data": event.response.model_dump(mode="json", by_alias=True),
                **_identity_payload(event, item_id=event.interaction_id),
            }
            return (_v1_event(event, EventTypeV1.A2UI_ACTION, payload, context=context),)
        if event.interaction_kind != "approval" or event.response.response_type != "approval":
            return ()
        call_id = (
            context.interaction_call_id(event.scope_id, event.interaction_id) if context else ""
        )
        payload = {
            "approval_id": event.interaction_id,
            "call_id": call_id,
            "decision": event.response.decision,
            "data": event.response.data,
            **_identity_payload(event, item_id=event.interaction_id),
        }
        return (_v1_event(event, EventTypeV1.APPROVAL_RESOLVED, payload, context=context),)
    if isinstance(event, ContinuationCreated):
        if event.continuation_kind != "graph_checkpoint":
            return ()
        payload = {
            "checkpoint_id": event.continuation_id,
            "granularity": event.ref.get("granularity", "snapshot"),
            "resume_target": event.ref,
            "resumable": event.resumable,
            **_identity_payload(event, item_id=event.continuation_id),
        }
        return (_v1_event(event, EventTypeV1.CHECKPOINT_CREATED, payload, context=context),)
    if isinstance(event, ContinuationResumed):
        if event.continuation_kind != "graph_checkpoint":
            return ()
        payload = {
            "checkpoint_id": event.continuation_id,
            "resume_attempt_id": event.resume_attempt_id,
            **_identity_payload(event, item_id=event.continuation_id),
        }
        return (_v1_event(event, EventTypeV1.CHECKPOINT_RESUMED, payload, context=context),)
    if isinstance(event, ContextCompactionStarted):
        payload = {
            "phase": context.compaction_phase if context else "runtime",
            "trigger": event.trigger,
            **_identity_payload(event),
        }
        return (_v1_event(event, EventTypeV1.CONTEXT_COMPACTION_STARTED, payload, context=context),)
    if isinstance(event, ContextCompactionCompleted):
        payload = {
            "phase": context.compaction_phase if context else "runtime",
            "trigger": event.trigger,
            "compacted_until_seq_id": event.compacted_until_seq,
            **_identity_payload(event),
        }
        return (
            _v1_event(event, EventTypeV1.CONTEXT_COMPACTION_COMPLETED, payload, context=context),
        )
    if isinstance(event, UsageReported):
        payload = {
            "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens,
            "total_tokens": event.total_tokens,
            "cached_tokens": event.cached_tokens,
            "reasoning_tokens": event.reasoning_tokens,
            **_identity_payload(event),
        }
        return (_v1_event(event, EventTypeV1.USAGE_REPORTED, payload, context=context),)
    raise TypeError(f"unsupported canonical RuntimeEvent: {type(event).__name__}")


__all__ = [
    "ALL_V1_EVENT_TYPES",
    "A2ATaskProjectionRef",
    "A2UIInteractionProjectionRef",
    "A2UISurfaceProjectionRef",
    "EventTypeV1",
    "RuntimeEventV1",
    "RuntimeEventV1Parser",
    "RuntimeEventV1ProjectionContext",
    "RuntimeEventV1ProjectionMode",
    "V1ProjectionContextRequiredError",
    "V1_EVENT_PAYLOAD_REQUIRED_KEYS",
    "project_to_v1",
]
