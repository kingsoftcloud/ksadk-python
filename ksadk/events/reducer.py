"""Single canonical reducer for live delivery and durable event replay."""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from ksadk.events.canonical import (
    ContextCompactionCompleted,
    ContextCompactionStarted,
    ContinuationCreated,
    ContinuationKind,
    ContinuationResumed,
    ErrorInfo,
    EventPhase,
    InteractionKind,
    InteractionRequest,
    InteractionRequested,
    InteractionResolved,
    InteractionResponse,
    ItemCompleted,
    ItemFailed,
    ItemKind,
    ItemSnapshotReplaced,
    ItemStarted,
    ItemUpdated,
    OutputRef,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RunProgress,
    RunStarted,
    RuntimeEvent,
    UsageReported,
    dump_runtime_event,
)
from ksadk.events.content import ContentValue, DataContent, JsonContent, TextContent

RunStatus = Literal["running", "interrupted", "completed", "failed", "canceled"]
ItemStatus = Literal["open", "completed", "failed"]
_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "canceled"})
_RUN_STATUS_TRANSITIONS: dict[RunStatus | None, frozenset[str]] = {
    None: frozenset(
        {
            "run.started",
            "run.progress",
            "run.interrupted",
            "run.completed",
            "run.failed",
            "run.canceled",
        }
    ),
    "running": frozenset(
        {
            "run.progress",
            "run.interrupted",
            "run.completed",
            "run.failed",
            "run.canceled",
        }
    ),
    "interrupted": frozenset({"run.progress", "run.completed", "run.failed", "run.canceled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "canceled": frozenset(),
}
_RUN_LIFECYCLE_EVENT_TYPES = _RUN_STATUS_TRANSITIONS[None]


class _ProjectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ItemProjection(_ProjectionModel):
    scope_id: str
    item_id: str
    item_kind: ItemKind
    phase: EventPhase | None = None
    status: ItemStatus = "open"
    parts: tuple[ContentValue, ...] = ()
    error: ErrorInfo | None = None


class InteractionProjection(_ProjectionModel):
    scope_id: str
    interaction_id: str
    interaction_kind: InteractionKind
    status: Literal["requested", "resolved"]
    request: InteractionRequest
    response: InteractionResponse | None = None


class ContinuationProjection(_ProjectionModel):
    scope_id: str
    continuation_id: str
    continuation_kind: ContinuationKind
    resumable: bool
    ref: dict[str, JsonValue]
    resume_attempt_ids: tuple[str, ...] = ()


class ContextCompactionProjection(_ProjectionModel):
    scope_id: str
    trigger: str
    status: Literal["started", "completed"]
    compacted_until_seq: int | None = None


class UsageProjection(_ProjectionModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0


class ProjectionPatch(_ProjectionModel):
    event_id: str
    seq: int
    event_type: str
    applied: bool = True
    mutation: RuntimeEvent | None
    reconciled: bool = False

    @model_validator(mode="after")
    def _mutation_matches_envelope(self) -> "ProjectionPatch":
        if self.applied != (self.mutation is not None):
            raise ValueError("applied patches require exactly one typed mutation")
        if self.mutation is not None and (
            self.event_id != self.mutation.event_id
            or self.seq != self.mutation.seq
            or self.event_type != self.mutation.event_type
        ):
            raise ValueError("patch envelope must match its typed mutation")
        return self


class RunProjection(_ProjectionModel):
    run_id: str | None = None
    status: RunStatus | None = None
    last_seq: int | None = None
    items: tuple[ItemProjection, ...] = ()
    output_refs: tuple[OutputRef, ...] = ()
    interactions: tuple[InteractionProjection, ...] = ()
    continuations: tuple[ContinuationProjection, ...] = ()
    context_compactions: tuple[ContextCompactionProjection, ...] = ()
    usage: UsageProjection = Field(default_factory=UsageProjection)


class StreamConformanceError(ValueError):
    """Structured rejection of an invalid canonical stream transition."""

    code: str
    source: str
    scope_id: str
    item_id: str | None

    def __init__(
        self,
        code: str,
        message: str,
        *,
        source: str,
        scope_id: str,
        item_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.source = source
        self.scope_id = scope_id
        self.item_id = item_id


@dataclass(frozen=True)
class _RecentEvent:
    event_id: str
    fingerprint: str


class StreamReducer:
    """Reduce canonical mutations into one identity-aware run projection."""

    RECENT_EVENT_LIMIT = 1024

    def __init__(self) -> None:
        self._run_id: str | None = None
        self._status: RunStatus | None = None
        self._last_seq: int | None = None
        self._items: OrderedDict[tuple[str, str], ItemProjection] = OrderedDict()
        self._interactions: OrderedDict[tuple[str, str], InteractionProjection] = OrderedDict()
        self._continuations: OrderedDict[tuple[str, str], ContinuationProjection] = OrderedDict()
        self._context_compactions: list[ContextCompactionProjection] = []
        self._usage = UsageProjection()
        self._output_refs: tuple[OutputRef, ...] = ()
        self._recent_events: OrderedDict[int, _RecentEvent] = OrderedDict()
        self._recent_event_ids: dict[str, int] = {}

    @property
    def recent_event_count(self) -> int:
        """Number of retained event fingerprints (diagnostic state only)."""

        return len(self._recent_events)

    def apply(self, event: RuntimeEvent) -> ProjectionPatch:
        """Apply one event or return an idempotent no-op for a recent replay."""

        fingerprint = self._fingerprint(event)
        duplicate = self._check_recent(event, fingerprint)
        if duplicate:
            return self._noop_patch(event)
        if self._last_seq is not None and event.seq <= self._last_seq:
            if self._status in _TERMINAL_RUN_STATUSES:
                self._validate_run(event)
            return self._noop_patch(event)

        self._validate_run(event)
        patch = self._apply_event(event)
        if self._run_id is None:
            self._run_id = event.run_id
        self._last_seq = event.seq
        self._record_recent(event, fingerprint)
        return patch

    def snapshot(self) -> RunProjection:
        """Return a detached projection suitable for live output or replay."""

        return RunProjection(
            run_id=self._run_id,
            status=self._status,
            last_seq=self._last_seq,
            items=tuple(item.model_copy(deep=True) for item in self._items.values()),
            output_refs=self._output_refs,
            interactions=tuple(
                interaction.model_copy(deep=True) for interaction in self._interactions.values()
            ),
            continuations=tuple(
                continuation.model_copy(deep=True) for continuation in self._continuations.values()
            ),
            context_compactions=tuple(
                compaction.model_copy(deep=True) for compaction in self._context_compactions
            ),
            usage=self._usage.model_copy(deep=True),
        )

    def _apply_event(self, event: RuntimeEvent) -> ProjectionPatch:
        if isinstance(event, (RunStarted, RunProgress)):
            self._status = "running"
            return self._patch(event)
        if isinstance(event, RunInterrupted):
            self._status = "interrupted"
            return self._patch(event)
        if isinstance(event, RunCompleted):
            self._ensure_no_open_items(event)
            self._validate_output_refs(event)
            self._status = "completed"
            self._output_refs = event.output_refs
            return self._patch(event)
        if isinstance(event, RunFailed):
            self._status = "failed"
            return self._patch(event)
        if isinstance(event, RunCanceled):
            self._status = "canceled"
            return self._patch(event)
        if isinstance(event, ItemStarted):
            return self._start_item(event)
        if isinstance(event, ItemUpdated):
            return self._update_item(event)
        if isinstance(event, ItemSnapshotReplaced):
            return self._replace_item_snapshot(event)
        if isinstance(event, ItemCompleted):
            return self._complete_item(event)
        if isinstance(event, ItemFailed):
            return self._fail_item(event)
        if isinstance(event, InteractionRequested):
            return self._request_interaction(event)
        if isinstance(event, InteractionResolved):
            return self._resolve_interaction(event)
        if isinstance(event, ContinuationCreated):
            return self._create_continuation(event)
        if isinstance(event, ContinuationResumed):
            return self._resume_continuation(event)
        if isinstance(event, ContextCompactionStarted):
            projection = ContextCompactionProjection(
                scope_id=event.scope_id,
                trigger=event.trigger,
                status="started",
            )
            self._context_compactions.append(projection)
            return self._patch(event)
        if isinstance(event, ContextCompactionCompleted):
            projection = ContextCompactionProjection(
                scope_id=event.scope_id,
                trigger=event.trigger,
                status="completed",
                compacted_until_seq=event.compacted_until_seq,
            )
            self._context_compactions.append(projection)
            return self._patch(event)
        if isinstance(event, UsageReported):
            self._usage = UsageProjection(
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
                total_tokens=event.total_tokens,
                cached_tokens=event.cached_tokens,
                reasoning_tokens=event.reasoning_tokens,
            )
            return self._patch(event)
        raise TypeError(f"unsupported runtime event: {type(event).__name__}")

    def _start_item(self, event: ItemStarted) -> ProjectionPatch:
        key = (event.scope_id, event.item_id)
        if key in self._items:
            raise self._error(
                event,
                "item_already_started",
                f"item {event.item_id!r} was already started",
            )
        parts = event.initial.parts if event.initial is not None else ()
        self._validate_unique_parts(event, parts)
        item = ItemProjection(
            scope_id=event.scope_id,
            item_id=event.item_id,
            item_kind=event.item_kind,
            phase=event.phase,
            parts=parts,
        )
        self._items[key] = item
        return self._patch(event)

    def _update_item(self, event: ItemUpdated) -> ProjectionPatch:
        key, item = self._open_item(event)
        parts = list(item.parts)
        matching_index = next(
            (index for index, part in enumerate(parts) if part.part_id == event.update.part_id),
            None,
        )
        if event.op == "replace" or matching_index is None:
            if matching_index is None:
                parts.append(event.update)
            else:
                parts[matching_index] = event.update
        else:
            current = parts[matching_index]
            parts[matching_index] = self._append_part(event, current, event.update)
        updated = item.model_copy(update={"parts": tuple(parts)})
        self._items[key] = updated
        return self._patch(event)

    def _replace_item_snapshot(self, event: ItemSnapshotReplaced) -> ProjectionPatch:
        key, item = self._open_item(event)
        self._validate_unique_parts(event, event.snapshot.parts)
        reconciled = item.parts != event.snapshot.parts
        self._items[key] = item.model_copy(update={"parts": event.snapshot.parts})
        return self._patch(event, reconciled=reconciled)

    def _complete_item(self, event: ItemCompleted) -> ProjectionPatch:
        key, item = self._open_item(event)
        self._validate_unique_parts(event, event.snapshot.parts)
        reconciled = item.parts != event.snapshot.parts
        completed = item.model_copy(update={"parts": event.snapshot.parts, "status": "completed"})
        self._items[key] = completed
        return self._patch(event, reconciled=reconciled)

    def _fail_item(self, event: ItemFailed) -> ProjectionPatch:
        key, item = self._open_item(event)
        failed = item.model_copy(update={"status": "failed", "error": event.error})
        self._items[key] = failed
        return self._patch(event)

    def _open_item(
        self, event: ItemUpdated | ItemSnapshotReplaced | ItemCompleted | ItemFailed
    ) -> tuple[tuple[str, str], ItemProjection]:
        key = (event.scope_id, event.item_id)
        item = self._items.get(key)
        if item is None:
            raise self._error(
                event,
                "item_not_started",
                f"item {event.item_id!r} was not started",
            )
        if item.item_kind != event.item_kind:
            raise self._error(
                event,
                "incompatible_item_kind",
                f"item {event.item_id!r} changed kind from "
                f"{item.item_kind!r} to {event.item_kind!r}",
            )
        if item.status != "open":
            raise self._error(
                event,
                "item_already_closed",
                f"item {event.item_id!r} is already closed",
            )
        return key, item

    def _append_part(
        self,
        event: ItemUpdated,
        current: ContentValue,
        update: ContentValue,
    ) -> ContentValue:
        if current.content_type != update.content_type:
            raise self._error(
                event,
                "incompatible_part_kind",
                f"part {update.part_id!r} changed content type",
            )
        if isinstance(current, TextContent) and isinstance(update, TextContent):
            return current.model_copy(update={"text": current.text + update.text})
        if isinstance(current, JsonContent) and isinstance(update, JsonContent):
            if isinstance(current.value, list) and isinstance(update.value, list):
                return current.model_copy(update={"value": current.value + update.value})
        if isinstance(current, DataContent) and isinstance(update, DataContent):
            if isinstance(current.data, list) and isinstance(update.data, list):
                return current.model_copy(update={"data": current.data + update.data})
        raise self._error(
            event,
            "unsupported_part_append",
            f"part {update.part_id!r} does not support append",
        )

    def _request_interaction(self, event: InteractionRequested) -> ProjectionPatch:
        key = (event.scope_id, event.interaction_id)
        if key in self._interactions:
            raise self._error(
                event,
                "interaction_already_requested",
                f"interaction {event.interaction_id!r} was already requested",
            )
        interaction = InteractionProjection(
            scope_id=event.scope_id,
            interaction_id=event.interaction_id,
            interaction_kind=event.interaction_kind,
            status="requested",
            request=event.request,
        )
        self._interactions[key] = interaction
        return self._patch(event)

    def _resolve_interaction(self, event: InteractionResolved) -> ProjectionPatch:
        key = (event.scope_id, event.interaction_id)
        interaction = self._interactions.get(key)
        if interaction is None:
            raise self._error(
                event,
                "interaction_not_requested",
                f"interaction {event.interaction_id!r} was not requested",
            )
        if interaction.status == "resolved":
            raise self._error(
                event,
                "interaction_already_resolved",
                f"interaction {event.interaction_id!r} was already resolved",
            )
        if interaction.interaction_kind != event.interaction_kind:
            raise self._error(
                event,
                "incompatible_interaction_kind",
                f"interaction {event.interaction_id!r} changed kind",
            )
        resolved = interaction.model_copy(update={"status": "resolved", "response": event.response})
        self._interactions[key] = resolved
        return self._patch(event)

    def _create_continuation(self, event: ContinuationCreated) -> ProjectionPatch:
        key = (event.scope_id, event.continuation_id)
        if key in self._continuations:
            raise self._error(
                event,
                "continuation_already_created",
                f"continuation {event.continuation_id!r} was already created",
            )
        continuation = ContinuationProjection(
            scope_id=event.scope_id,
            continuation_id=event.continuation_id,
            continuation_kind=event.continuation_kind,
            resumable=event.resumable,
            ref=event.ref,
        )
        self._continuations[key] = continuation
        return self._patch(event)

    def _resume_continuation(self, event: ContinuationResumed) -> ProjectionPatch:
        key = (event.scope_id, event.continuation_id)
        continuation = self._continuations.get(key)
        if continuation is None:
            raise self._error(
                event,
                "continuation_not_created",
                f"continuation {event.continuation_id!r} was not created",
            )
        if continuation.continuation_kind != event.continuation_kind:
            raise self._error(
                event,
                "incompatible_continuation_kind",
                f"continuation {event.continuation_id!r} changed kind",
            )
        resumed = continuation.model_copy(
            update={
                "resume_attempt_ids": continuation.resume_attempt_ids + (event.resume_attempt_id,)
            }
        )
        self._continuations[key] = resumed
        return self._patch(event)

    def _ensure_no_open_items(self, event: RunCompleted) -> None:
        open_item = next((item for item in self._items.values() if item.status == "open"), None)
        if open_item is not None:
            raise self._error(
                event,
                "run_completed_with_open_items",
                "run cannot complete while items remain open",
                item_id=open_item.item_id,
            )

    def _validate_output_refs(self, event: RunCompleted) -> None:
        for ref in event.output_refs:
            item = self._items.get((ref.scope_id, ref.item_id))
            if item is None or item.status != "completed":
                raise self._error(
                    event,
                    "invalid_output_ref",
                    f"output item {ref.item_id!r} is not completed",
                    item_id=ref.item_id,
                )
            if ref.part_id is not None and all(part.part_id != ref.part_id for part in item.parts):
                raise self._error(
                    event,
                    "invalid_output_ref",
                    f"output part {ref.part_id!r} does not exist",
                    item_id=ref.item_id,
                )

    def _validate_unique_parts(
        self,
        event: ItemStarted | ItemSnapshotReplaced | ItemCompleted,
        parts: tuple[ContentValue, ...],
    ) -> None:
        part_ids = [part.part_id for part in parts]
        if len(set(part_ids)) != len(part_ids):
            raise self._error(
                event,
                "duplicate_part_id",
                f"item {event.item_id!r} contains duplicate part ids",
            )

    def _validate_run(self, event: RuntimeEvent) -> None:
        if self._run_id is not None and self._run_id != event.run_id:
            raise self._error(
                event,
                "incompatible_run_id",
                f"reducer belongs to run {self._run_id!r}, not {event.run_id!r}",
            )
        if self._status in _TERMINAL_RUN_STATUSES:
            raise self._error(
                event,
                "run_already_terminal",
                f"run is already terminal with status {self._status!r}",
            )
        if (
            event.event_type in _RUN_LIFECYCLE_EVENT_TYPES
            and event.event_type not in _RUN_STATUS_TRANSITIONS[self._status]
        ):
            raise self._error(
                event,
                "invalid_run_transition",
                f"event {event.event_type!r} is invalid after status {self._status!r}",
            )

    def _check_recent(self, event: RuntimeEvent, fingerprint: str) -> bool:
        same_seq = self._recent_events.get(event.seq)
        if same_seq is not None:
            if same_seq.event_id == event.event_id and same_seq.fingerprint == fingerprint:
                return True
            raise self._error(
                event,
                "conflicting_seq",
                f"seq {event.seq} was reused with different content",
            )
        existing_seq = self._recent_event_ids.get(event.event_id)
        if existing_seq is not None:
            existing = self._recent_events[existing_seq]
            if existing.fingerprint == fingerprint:
                return True
            raise self._error(
                event,
                "conflicting_event_id",
                f"event_id {event.event_id!r} was reused with different content",
            )
        return False

    def _record_recent(self, event: RuntimeEvent, fingerprint: str) -> None:
        self._recent_events[event.seq] = _RecentEvent(event.event_id, fingerprint)
        self._recent_event_ids[event.event_id] = event.seq
        while len(self._recent_events) > self.RECENT_EVENT_LIMIT:
            old_seq, old = self._recent_events.popitem(last=False)
            if self._recent_event_ids.get(old.event_id) == old_seq:
                del self._recent_event_ids[old.event_id]

    @staticmethod
    def _fingerprint(event: RuntimeEvent) -> str:
        return json.dumps(
            dump_runtime_event(event),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _patch(event: RuntimeEvent, *, reconciled: bool = False) -> ProjectionPatch:
        return ProjectionPatch(
            event_id=event.event_id,
            seq=event.seq,
            event_type=event.event_type,
            mutation=event,
            reconciled=reconciled,
        )

    @staticmethod
    def _noop_patch(event: RuntimeEvent) -> ProjectionPatch:
        return ProjectionPatch(
            event_id=event.event_id,
            seq=event.seq,
            event_type=event.event_type,
            applied=False,
            mutation=None,
        )

    @staticmethod
    def _error(
        event: RuntimeEvent,
        code: str,
        message: str,
        *,
        item_id: str | None = None,
    ) -> StreamConformanceError:
        return StreamConformanceError(
            code,
            message,
            source=event.source.framework,
            scope_id=event.scope_id,
            item_id=item_id if item_id is not None else getattr(event, "item_id", None),
        )


__all__ = [
    "ContextCompactionProjection",
    "ContinuationProjection",
    "InteractionProjection",
    "ItemProjection",
    "ProjectionPatch",
    "RunProjection",
    "StreamConformanceError",
    "StreamReducer",
    "UsageProjection",
]
