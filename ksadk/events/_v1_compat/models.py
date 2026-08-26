"""v1 wire models: envelope, event-type registry, and projection context."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from ksadk.events.content import ToolCallContent
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


__all__ = [
    "ALL_V1_EVENT_TYPES",
    "A2ATaskProjectionRef",
    "A2UIInteractionProjectionRef",
    "A2UISurfaceProjectionRef",
    "EventTypeV1",
    "RuntimeEventV1",
    "RuntimeEventV1ProjectionContext",
    "RuntimeEventV1ProjectionMode",
    "V1ProjectionContextRequiredError",
    "V1_EVENT_PAYLOAD_REQUIRED_KEYS",
]
