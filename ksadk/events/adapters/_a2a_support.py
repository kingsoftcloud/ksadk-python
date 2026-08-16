"""A2A adapter 的错误/上下文/状态 dataclass 与校验辅助（纯移动自 adapters.a2a，行为不变）。"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast

from a2a.types import (
    GetTaskRequest,
    Task,
    TaskState,
    TaskStatus,
)
from google.protobuf.json_format import MessageToDict
from pydantic import JsonValue

from ksadk.events.canonical import (
    OutputRef,
    RuntimeEvent,
    SourceRef,
)
from ksadk.events.content import (
    ContentSnapshot,
    ContentValue,
)
from ksadk.events.identity import (
    stable_scope_id,
)

ReconciliationReason = Literal["terminal", "reconnect", "subscription_rebuild"]

_ACTIVE_STATES = frozenset({TaskState.TASK_STATE_SUBMITTED, TaskState.TASK_STATE_WORKING})
_INTERACTION_STATES = frozenset(
    {TaskState.TASK_STATE_INPUT_REQUIRED, TaskState.TASK_STATE_AUTH_REQUIRED}
)
_TERMINAL_STATES = frozenset(
    {
        TaskState.TASK_STATE_COMPLETED,
        TaskState.TASK_STATE_FAILED,
        TaskState.TASK_STATE_CANCELED,
        TaskState.TASK_STATE_REJECTED,
    }
)


class A2AMappingError(ValueError):
    """An A2A protobuf violates the native identity or content contract."""

    def __init__(self, code: str, field_name: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.field_name = field_name
        self.source = "a2a"


def _fail(code: str, field_name: str, message: str) -> None:
    raise A2AMappingError(code, field_name, message)


@dataclass
class A2AAdapterContext:
    """Runtime invocation facts and non-durable pre-store sequence placeholders."""

    run_id: str
    context_id: str
    task_id: str | None
    initial_seq: int = 0
    _next_seq: int = field(init=False, repr=False)
    _direct_message_id: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.run_id = _required_string(self.run_id, "runtime run_id")
        self.context_id = _required_string(self.context_id, "context_id")
        if self.task_id is not None:
            self.task_id = _required_string(self.task_id, "task_id")
        if self.initial_seq < 0:
            raise ValueError("A2A initial_seq must be non-negative")
        self._next_seq = self.initial_seq

    @property
    def scope_id(self) -> str:
        if self.task_id is not None:
            return stable_scope_id("a2a", self.context_id, self.task_id)
        if self._direct_message_id is not None:
            return stable_scope_id("a2a", self.context_id, "message", self._direct_message_id)
        _fail(
            "missing_native_identity",
            "task_id/message_id",
            "A2A scope requires a task_id or direct message_id",
        )

    @property
    def native_run_id(self) -> str:
        return _required_string(self.task_id or self._direct_message_id, "task_id/message_id")

    def bind_direct_message(self, message_id: str) -> None:
        native_message_id = _required_string(message_id, "message.message_id")
        if self.task_id is not None:
            return
        if self._direct_message_id not in {None, native_message_id}:
            _fail(
                "direct_message_scope_collision",
                "message.message_id",
                "A2A direct response changed message scope",
            )
        self._direct_message_id = native_message_id

    def allocate_placeholder_seq(self) -> int:
        value = self._next_seq
        self._next_seq += 1
        return value

    def peek_placeholder_seq(self) -> int:
        """Return the next placeholder without consuming it."""

        return self._next_seq


@dataclass(frozen=True)
class A2AReconciliationResult:
    """Result of GetTask reconciliation.

    ``consistent`` means the emitted projection matches the fetched Task.
    ``terminal`` independently reports whether that Task was terminal.
    """

    events: tuple[RuntimeEvent, ...]
    consistent: bool
    terminal: bool
    attempt_id: str
    error: str | None = None


class _A2AClient(Protocol):
    async def get_task(self, request: GetTaskRequest, **kwargs: Any) -> Task: ...


@dataclass
class _ArtifactState:
    artifact_id: str
    item_id: str
    parts: dict[str, ContentValue] = field(default_factory=dict)
    part_order: list[str] = field(default_factory=list)
    closed: bool = False
    present: bool = True

    def snapshot(self) -> ContentSnapshot:
        return ContentSnapshot(parts=tuple(self.parts[part_id] for part_id in self.part_order))


@dataclass(frozen=True)
class _MessageState:
    signature: bytes


@dataclass(frozen=True)
class _Occurrence:
    native_event_id: str | None
    native_cursor: str | None
    identity: str
    provisional: bool
    duplicate: bool = False


@dataclass
class _SnapshotScope:
    """Shared plumbing for one GetTask snapshot projection pass."""

    task: Task
    context: A2AAdapterContext
    source: SourceRef
    timestamp: float
    occurrence: _Occurrence
    terminal: bool
    reason: ReconciliationReason
    attempt_id: str
    events: list[RuntimeEvent] = field(default_factory=list)
    _output_refs: list[OutputRef] = field(default_factory=list)
    _output_ref_keys: set[tuple[str, str]] = field(default_factory=set)

    def add_output_ref(self, item_id: str) -> None:
        key = (self.context.scope_id, item_id)
        if key not in self._output_ref_keys:
            self._output_ref_keys.add(key)
            self._output_refs.append(OutputRef(scope_id=self.context.scope_id, item_id=item_id))


def _proto_fingerprint(value: object) -> str:
    serialize = getattr(value, "SerializeToString", None)
    if not callable(serialize):
        _fail(
            "invalid_protobuf_payload",
            "event",
            "A2A occurrence payload must be a protobuf message",
        )
    payload = cast(bytes, serialize(deterministic=True))
    return hashlib.sha256(payload).hexdigest()


def _validate_unique_parts(parts: tuple[ContentValue, ...], field_name: str) -> None:
    seen: set[str] = set()
    for part in parts:
        if part.part_id in seen:
            _fail(
                "duplicate_part_id",
                field_name,
                f"A2A snapshot contains duplicate part_id {part.part_id!r}",
            )
        seen.add(part.part_id)


def _metadata(struct: Any) -> dict[str, JsonValue]:
    if struct is None:
        return {}
    return cast(
        dict[str, JsonValue],
        MessageToDict(struct, preserving_proto_field_name=True),
    )


def _optional_metadata_string(
    metadata: Mapping[str, JsonValue],
    *keys: str,
) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return None


def _required_metadata_string(metadata: Mapping[str, JsonValue], key: str) -> str:
    value = _optional_metadata_string(metadata, key)
    if value is None:
        _fail(
            "missing_part_metadata",
            f"part.metadata.{key}",
            f"A2A typed part requires metadata {key!r}",
        )
    return value


def _required_string(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        _fail(
            "missing_native_identity",
            field_name,
            f"A2A {field_name} must be non-empty",
        )
    return text


def _status_message_id(status: TaskStatus) -> str | None:
    if status.HasField("message") and status.message.message_id:
        return status.message.message_id
    return None


def _parts_text(parts: Any) -> str:
    return "".join(str(part.text) for part in parts if part.text)


def _timestamp(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("invalid_timestamp", "timestamp", "timestamp must be finite")
    result = float(value)
    if not math.isfinite(result):
        _fail("invalid_timestamp", "timestamp", "timestamp must be finite")
    return result
