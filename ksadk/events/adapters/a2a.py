"""A2A SDK 1.1.0 protobuf events to RuntimeEvent schema version 2."""

from __future__ import annotations

import base64
import copy
import hashlib
import math
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast

from a2a.types import (
    Artifact,
    GetTaskRequest,
    Message,
    Part,
    Role,
    StreamResponse,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from google.protobuf.json_format import MessageToDict
from pydantic import JsonValue

from ksadk.events.canonical import (
    ContinuationCreated,
    ContinuationResumed,
    ErrorInfo,
    InteractionRequested,
    InteractionResolved,
    ItemCompleted,
    ItemFailed,
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
    SourceRef,
    StructuredInputRequest,
    StructuredInputResponse,
)
from ksadk.events.content import (
    ArtifactContent,
    ContentSnapshot,
    ContentValue,
    DataContent,
    TextContent,
    ToolCallContent,
    ToolResultContent,
)
from ksadk.events.identity import (
    stable_event_id,
    stable_item_id,
    stable_part_id,
    stable_scope_id,
)

ReconciliationReason = Literal["terminal", "reconnect", "subscription_rebuild"]


class A2AMappingError(ValueError):
    """An A2A protobuf violates the native identity or content contract."""

    def __init__(self, code: str, field_name: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.field_name = field_name
        self.source = "a2a"


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
        raise A2AMappingError(
            "missing_native_identity",
            "task_id/message_id",
            "A2A scope requires a task_id or direct message_id",
        )

    @property
    def native_run_id(self) -> str:
        value = self.task_id or self._direct_message_id
        return _required_string(value, "task_id/message_id")

    def bind_direct_message(self, message_id: str) -> None:
        native_message_id = _required_string(message_id, "message.message_id")
        if self.task_id is not None:
            return
        if self._direct_message_id not in {None, native_message_id}:
            raise A2AMappingError(
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


class A2AEventAdapter:
    """Map A2A 1.1.0 typed protobuf delivery into canonical events.

    A delivery without a producer occurrence id is deliberately provisional.
    In particular, its ``last_chunk`` records source closure but does not emit
    the irreversible canonical ``item.completed``; GetTask closes it with one
    authoritative snapshot. A trusted last chunk may close immediately, and a
    later GetTask snapshot must then be byte-for-byte equivalent or fail closed.
    """

    OCCURRENCE_CACHE_LIMIT = 1024

    def __init__(self) -> None:
        self._artifacts: dict[str, _ArtifactState] = {}
        self._messages: dict[str, _MessageState] = {}
        self._seen_occurrences: OrderedDict[str, str] = OrderedDict()
        self._provisional_ordinals: dict[str, int] = {}
        self._interaction_payloads: dict[tuple[int, str], str] = {}
        self._run_started = False
        self._run_interrupted = False
        self._active_interaction: tuple[str, str] | None = None
        self._terminal_snapshot_fingerprint: str | None = None

    def map_event(
        self,
        native_event: object,
        context: A2AAdapterContext,
        *,
        timestamp: float,
    ) -> tuple[RuntimeEvent, ...]:
        """Map one real A2A protobuf object in source delivery order."""

        timestamp = _timestamp(timestamp)
        shadow = copy.deepcopy(self)
        shadow_context = copy.deepcopy(context)
        events = shadow._map_event(native_event, shadow_context, timestamp=timestamp)
        self._commit_shadow(shadow, context, shadow_context)
        return events

    def _map_event(
        self,
        native_event: object,
        context: A2AAdapterContext,
        *,
        timestamp: float,
    ) -> tuple[RuntimeEvent, ...]:
        if isinstance(native_event, StreamResponse):
            payload_name = native_event.WhichOneof("payload")
            if payload_name is None:
                raise A2AMappingError(
                    "empty_stream_response",
                    "StreamResponse.payload",
                    "A2A StreamResponse has no payload",
                )
            return self._map_event(
                getattr(native_event, payload_name), context, timestamp=timestamp
            )
        if isinstance(native_event, TaskArtifactUpdateEvent):
            return self._map_artifact_update(native_event, context, timestamp)
        if isinstance(native_event, TaskStatusUpdateEvent):
            return self._map_status_update(native_event, context, timestamp)
        if isinstance(native_event, Message):
            self._validate_message_identity(native_event, context)
            context.bind_direct_message(native_event.message_id)
            self._require_agent_message(native_event, field_name="Message.role")
            return self._map_message(
                native_event,
                context,
                timestamp,
                consistent=True,
                direct_response=True,
            )
        if isinstance(native_event, Task):
            self._validate_identity(native_event.context_id, native_event.id, context)
            self._require_task_status(native_event)
            return self._map_status(
                native_event.status,
                _metadata(native_event.metadata),
                context,
                timestamp,
                native_item_id=native_event.id,
                occurrence_payload=native_event,
            )
        raise A2AMappingError(
            "unsupported_event",
            "event",
            f"unsupported A2A event: {type(native_event).__name__}",
        )

    async def reconcile(
        self,
        client: _A2AClient,
        context: A2AAdapterContext,
        *,
        reason: ReconciliationReason,
        attempt_id: str | None = None,
        timestamp: float,
    ) -> A2AReconciliationResult:
        """Call GetTask and project its authoritative state.

        The method intentionally accepts a client, not a caller-supplied Task,
        so terminal, reconnect, and subscription rebuild cannot accidentally
        claim consistency from the notification that triggered reconciliation.
        """

        timestamp = _timestamp(timestamp)
        resolved_attempt_id = _required_string(
            attempt_id or f"{reason}:{context.task_id}",
            "reconciliation attempt_id",
        )
        task_id = _required_string(context.task_id, "task_id")
        try:
            task = await client.get_task(GetTaskRequest(id=task_id))
            self._validate_identity(task.context_id, task.id, context)
            self._require_task_status(task)
            task_fingerprint = _proto_fingerprint(task)
            if self._terminal_snapshot_fingerprint is not None:
                if task_fingerprint != self._terminal_snapshot_fingerprint:
                    raise A2AMappingError(
                        "terminal_snapshot_collision",
                        "Task",
                        "A2A terminal GetTask snapshot changed after completion",
                    )
                return A2AReconciliationResult(
                    events=(),
                    consistent=True,
                    terminal=True,
                    attempt_id=resolved_attempt_id,
                )
            shadow = copy.deepcopy(self)
            shadow_context = copy.deepcopy(context)
            events = shadow._map_task_snapshot(
                task,
                shadow_context,
                reason,
                resolved_attempt_id,
                timestamp,
            )
        except Exception as exc:  # the result must remain usable after a transport/mapping failure
            error = exc.code if isinstance(exc, A2AMappingError) else "get_task_failed"
            diagnostic = self._reconciliation_diagnostic(
                context,
                reason=reason,
                timestamp=timestamp,
                error=error,
                exception_type=type(exc).__name__,
                attempt_id=resolved_attempt_id,
            )
            return A2AReconciliationResult(
                events=(diagnostic,),
                consistent=False,
                terminal=False,
                attempt_id=resolved_attempt_id,
                error=error,
            )
        self._commit_shadow(shadow, context, shadow_context)
        return A2AReconciliationResult(
            events=events,
            consistent=True,
            terminal=task.status.state in _TERMINAL_STATES,
            attempt_id=resolved_attempt_id,
        )

    def _commit_shadow(
        self,
        shadow: A2AEventAdapter,
        context: A2AAdapterContext,
        shadow_context: A2AAdapterContext,
    ) -> None:
        self.__dict__.clear()
        self.__dict__.update(shadow.__dict__)
        context._next_seq = shadow_context._next_seq
        context._direct_message_id = shadow_context._direct_message_id

    def _map_artifact_update(
        self,
        update: TaskArtifactUpdateEvent,
        context: A2AAdapterContext,
        timestamp: float,
    ) -> tuple[RuntimeEvent, ...]:
        self._validate_identity(update.context_id, update.task_id, context)
        if not update.HasField("artifact"):
            raise A2AMappingError(
                "missing_artifact",
                "artifact",
                "A2A artifact update requires artifact",
            )
        artifact = update.artifact
        artifact_id = _required_string(artifact.artifact_id, "artifact.artifact_id")
        occurrence = self._occurrence(
            _metadata(update.metadata),
            provisional_key=f"artifact:{artifact_id}",
            payload=update,
        )
        if occurrence.duplicate:
            return ()

        state = self._artifacts.get(artifact_id)
        if update.append and (state is None or not state.present):
            raise A2AMappingError(
                "artifact_missing",
                "append",
                "A2A artifact_missing: append=True requires an authoritative base",
            )
        if state is not None and state.closed:
            raise A2AMappingError(
                "artifact_already_closed",
                "artifact.artifact_id",
                f"A2A artifact {artifact_id!r} is already closed",
            )
        item_id = stable_item_id(
            "a2a",
            context.context_id,
            context.task_id,
            "artifact",
            artifact_id,
        )
        source_metadata = self._source_metadata(
            provisional=occurrence.provisional,
            consistent=False,
            artifact_closed=bool(update.last_chunk),
            artifact=artifact,
        )
        source = self._source(
            context,
            occurrence,
            native_item_id=artifact_id,
            metadata=source_metadata,
        )
        events: list[RuntimeEvent] = []
        if state is None:
            state = _ArtifactState(artifact_id=artifact_id, item_id=item_id)
            self._artifacts[artifact_id] = state
            events.append(
                ItemStarted(
                    **self._envelope(
                        context,
                        source,
                        timestamp,
                        item_id=item_id,
                        event_type="item.started",
                        part_id="artifact",
                        occurrence=occurrence,
                        ordinal=0,
                    ),
                    item_id=item_id,
                    item_kind="artifact",
                    phase="final_answer",
                )
            )

        absolute_start = len(state.part_order) if update.append else 0
        converted = self._convert_parts(artifact, item_id, start_index=absolute_start)
        if not converted:
            raise A2AMappingError(
                "empty_artifact",
                "artifact.parts",
                "A2A artifact requires at least one supported part",
            )
        _validate_unique_parts(converted, "artifact.parts")
        previous_parts = dict(state.parts)
        for part in converted:
            previous = previous_parts.get(part.part_id)
            if previous is not None and previous.content_type != part.content_type:
                raise A2AMappingError(
                    "part_identity_collision",
                    "artifact.parts",
                    f"A2A part {part.part_id!r} changed content type",
                )
        if not update.append:
            state.present = True
            state.parts = {part.part_id: part for part in converted}
            state.part_order = [part.part_id for part in converted]
            events.append(
                ItemSnapshotReplaced(
                    **self._envelope(
                        context,
                        source,
                        timestamp,
                        item_id=item_id,
                        event_type="item.snapshot_replaced",
                        part_id="snapshot",
                        occurrence=occurrence,
                        ordinal=1,
                    ),
                    item_id=item_id,
                    item_kind="artifact",
                    snapshot=state.snapshot(),
                )
            )
        else:
            for index, part in enumerate(converted):
                if part.part_id in state.parts:
                    raise A2AMappingError(
                        "part_identity_collision",
                        "artifact.parts",
                        f"A2A append reused existing part {part.part_id!r}",
                    )
                state.parts[part.part_id] = part
                state.part_order.append(part.part_id)
                events.append(
                    ItemUpdated(
                        **self._envelope(
                            context,
                            source,
                            timestamp,
                            item_id=item_id,
                            event_type="item.updated",
                            part_id=part.part_id,
                            occurrence=occurrence,
                            ordinal=index + 1,
                        ),
                        item_id=item_id,
                        item_kind="artifact",
                        op="append",
                        update=part,
                    )
                )
        return tuple(events)

    def _map_status_update(
        self,
        update: TaskStatusUpdateEvent,
        context: A2AAdapterContext,
        timestamp: float,
    ) -> tuple[RuntimeEvent, ...]:
        self._validate_identity(update.context_id, update.task_id, context)
        if not update.HasField("status"):
            raise A2AMappingError(
                "missing_status",
                "status",
                "A2A status update requires status",
            )
        return self._map_status(
            update.status,
            _metadata(update.metadata),
            context,
            timestamp,
            native_item_id=_status_message_id(update.status),
            occurrence_payload=update,
        )

    def _map_status(
        self,
        status: TaskStatus,
        metadata: Mapping[str, JsonValue],
        context: A2AAdapterContext,
        timestamp: float,
        *,
        native_item_id: str | None,
        occurrence_payload: object,
    ) -> tuple[RuntimeEvent, ...]:
        occurrence = self._occurrence(
            metadata,
            provisional_key="status",
            payload=occurrence_payload,
        )
        if occurrence.duplicate:
            return ()
        source = self._source(
            context,
            occurrence,
            native_item_id=native_item_id,
            metadata={"provisional": occurrence.provisional, "consistent": False},
        )
        state = status.state
        events: list[RuntimeEvent] = []
        if state in {TaskState.TASK_STATE_SUBMITTED, TaskState.TASK_STATE_WORKING}:
            if self._active_interaction is not None:
                interaction_id, continuation_id = self._active_interaction
                events.append(
                    InteractionResolved(
                        **self._envelope(
                            context,
                            source,
                            timestamp,
                            item_id=interaction_id,
                            event_type="interaction.resolved",
                            part_id="interaction",
                            occurrence=occurrence,
                            ordinal=len(events),
                        ),
                        interaction_id=interaction_id,
                        interaction_kind="structured_input",
                        response=StructuredInputResponse(data={"state": TaskState.Name(state)}),
                    )
                )
                events.append(
                    ContinuationResumed(
                        **self._envelope(
                            context,
                            source,
                            timestamp,
                            item_id=continuation_id,
                            event_type="continuation.resumed",
                            part_id="continuation",
                            occurrence=occurrence,
                            ordinal=len(events),
                        ),
                        continuation_id=continuation_id,
                        continuation_kind="task_resume",
                        resume_attempt_id=stable_item_id(
                            "a2a",
                            context.scope_id,
                            continuation_id,
                            occurrence.identity,
                        ),
                    )
                )
                self._active_interaction = None
            if not self._run_started:
                events.append(
                    self._run_started_event(context, source, timestamp, occurrence, len(events))
                )
                self._run_started = True
            else:
                events.append(
                    self._run_progress_event(
                        context,
                        source,
                        timestamp,
                        occurrence,
                        len(events),
                        message=TaskState.Name(state),
                    )
                )
            self._run_interrupted = False
            return tuple(events)

        if state in {TaskState.TASK_STATE_INPUT_REQUIRED, TaskState.TASK_STATE_AUTH_REQUIRED}:
            if not status.HasField("message"):
                raise A2AMappingError(
                    "missing_interaction_message",
                    "status.message",
                    "A2A input/auth required status requires a message identity",
                )
            message = self._normalize_nested_message(status.message, context)
            message_id = _required_string(message.message_id, "status.message.message_id")
            lifecycle_key = (state, message_id)
            payload_fingerprint = _proto_fingerprint(message)
            previous_fingerprint = self._interaction_payloads.get(lifecycle_key)
            if previous_fingerprint is not None:
                if previous_fingerprint == payload_fingerprint:
                    return ()
                raise A2AMappingError(
                    "interaction_payload_collision",
                    "status.message",
                    f"A2A interaction payload changed for message {message_id!r}",
                )
            self._interaction_payloads[lifecycle_key] = payload_fingerprint
            if self._active_interaction is not None:
                previous_interaction_id, _ = self._active_interaction
                events.append(
                    InteractionResolved(
                        **self._envelope(
                            context,
                            source,
                            timestamp,
                            item_id=previous_interaction_id,
                            event_type="interaction.resolved",
                            part_id="interaction",
                            occurrence=occurrence,
                            ordinal=len(events),
                        ),
                        interaction_id=previous_interaction_id,
                        interaction_kind="structured_input",
                        response=StructuredInputResponse(
                            data={"state": "SUPERSEDED_BY_NEW_A2A_INTERACTION"}
                        ),
                    )
                )
                self._active_interaction = None
            if not self._run_started:
                events.append(
                    self._run_started_event(context, source, timestamp, occurrence, len(events))
                )
                self._run_started = True
            interaction_id = stable_item_id(
                "a2a", context.scope_id, "interaction", TaskState.Name(state), message_id
            )
            continuation_id = stable_item_id(
                "a2a", context.scope_id, "continuation", context.task_id, message_id
            )
            prompt = _parts_text(message.parts) or None
            message_metadata = _metadata(message.metadata)
            schema = message_metadata.get("input_schema")
            if not isinstance(schema, dict):
                schema = {
                    "type": "object" if state == TaskState.TASK_STATE_AUTH_REQUIRED else "string"
                }
            events.append(
                InteractionRequested(
                    **self._envelope(
                        context,
                        source,
                        timestamp,
                        item_id=interaction_id,
                        event_type="interaction.requested",
                        part_id="interaction",
                        occurrence=occurrence,
                        ordinal=len(events),
                    ),
                    interaction_id=interaction_id,
                    interaction_kind="structured_input",
                    request=StructuredInputRequest(prompt=prompt, schema=schema),
                )
            )
            events.append(
                ContinuationCreated(
                    **self._envelope(
                        context,
                        source,
                        timestamp,
                        item_id=continuation_id,
                        event_type="continuation.created",
                        part_id="continuation",
                        occurrence=occurrence,
                        ordinal=len(events),
                    ),
                    continuation_id=continuation_id,
                    continuation_kind="task_resume",
                    resumable=True,
                    ref={"context_id": context.context_id, "task_id": context.task_id},
                )
            )
            if not self._run_interrupted:
                events.append(
                    RunInterrupted(
                        **self._envelope(
                            context,
                            source,
                            timestamp,
                            item_id=context.run_id,
                            event_type="run.interrupted",
                            part_id="run",
                            occurrence=occurrence,
                            ordinal=len(events),
                        ),
                        status="interrupted",
                        reason=TaskState.Name(state),
                        interaction_id=interaction_id,
                        continuation_id=continuation_id,
                    )
                )
                self._run_interrupted = True
            self._active_interaction = (interaction_id, continuation_id)
            return tuple(events)

        if state in _TERMINAL_STATES:
            if not self._run_started:
                events.append(
                    self._run_started_event(context, source, timestamp, occurrence, len(events))
                )
                self._run_started = True
            events.append(
                self._run_progress_event(
                    context,
                    source,
                    timestamp,
                    occurrence,
                    len(events),
                    message="awaiting authoritative A2A GetTask snapshot",
                )
            )
            self._run_interrupted = False
            return tuple(events)

        raise A2AMappingError(
            "unknown_task_state",
            "status.state",
            f"unsupported A2A TaskState {state}",
        )

    def _map_task_snapshot(
        self,
        task: Task,
        context: A2AAdapterContext,
        reason: ReconciliationReason,
        attempt_id: str,
        timestamp: float,
    ) -> tuple[RuntimeEvent, ...]:
        state = task.status.state
        terminal = state in _TERMINAL_STATES
        occurrence = _Occurrence(
            native_event_id=None,
            native_cursor=None,
            identity=(
                f"get-task:{reason}:{attempt_id}:{TaskState.Name(state)}:{_proto_fingerprint(task)}"
            ),
            provisional=False,
        )
        metadata: dict[str, JsonValue] = {
            "provisional": False,
            "consistent": True,
            "reconciliation_reason": reason,
            "reconciliation_attempt_id": attempt_id,
            "terminal": terminal,
        }
        source = self._source(
            context,
            occurrence,
            native_item_id=task.id,
            metadata=metadata,
        )
        events: list[RuntimeEvent] = []
        if not self._run_started:
            events.append(
                self._run_started_event(context, source, timestamp, occurrence, len(events))
            )
            self._run_started = True
        snapshot_artifact_ids: set[str] = set()
        output_refs: list[OutputRef] = []
        output_ref_keys: set[tuple[str, str]] = set()

        def add_output_ref(item_id: str) -> None:
            key = (context.scope_id, item_id)
            if key in output_ref_keys:
                return
            output_ref_keys.add(key)
            output_refs.append(OutputRef(scope_id=context.scope_id, item_id=item_id))

        for artifact in task.artifacts:
            artifact_id = _required_string(artifact.artifact_id, "task.artifacts.artifact_id")
            snapshot_artifact_ids.add(artifact_id)
            item_id = stable_item_id(
                "a2a",
                context.context_id,
                context.task_id,
                "artifact",
                artifact_id,
            )
            parts = self._convert_parts(artifact, item_id, start_index=0)
            if not parts:
                raise A2AMappingError(
                    "empty_artifact_snapshot",
                    "task.artifacts.parts",
                    "A2A GetTask artifact snapshot requires supported parts",
                )
            _validate_unique_parts(parts, "task.artifacts.parts")
            snapshot = ContentSnapshot(parts=parts)
            artifact_state = self._artifacts.get(artifact_id)
            artifact_source = source.model_copy(update={"native_item_id": artifact_id})
            if artifact_state is None:
                artifact_state = _ArtifactState(artifact_id=artifact_id, item_id=item_id)
                self._artifacts[artifact_id] = artifact_state
                events.append(
                    ItemStarted(
                        **self._envelope(
                            context,
                            artifact_source,
                            timestamp,
                            item_id=item_id,
                            event_type="item.started",
                            part_id="artifact",
                            occurrence=occurrence,
                            ordinal=len(events),
                        ),
                        item_id=item_id,
                        item_kind="artifact",
                        phase="final_answer",
                    )
                )
            elif artifact_state.closed:
                if artifact_state.snapshot() != snapshot:
                    raise A2AMappingError(
                        "trusted_snapshot_collision",
                        "task.artifacts",
                        f"GetTask changed already completed artifact {artifact_id!r}",
                    )
                if terminal:
                    add_output_ref(item_id)
                continue
            artifact_state.parts = {part.part_id: part for part in parts}
            artifact_state.part_order = [part.part_id for part in parts]
            artifact_state.present = True
            if terminal:
                artifact_state.closed = True
                events.append(
                    ItemCompleted(
                        **self._envelope(
                            context,
                            artifact_source,
                            timestamp,
                            item_id=item_id,
                            event_type="item.completed",
                            part_id="snapshot",
                            occurrence=occurrence,
                            ordinal=len(events),
                        ),
                        item_id=item_id,
                        item_kind="artifact",
                        snapshot=snapshot,
                    )
                )
                add_output_ref(item_id)
            else:
                events.append(
                    ItemSnapshotReplaced(
                        **self._envelope(
                            context,
                            artifact_source,
                            timestamp,
                            item_id=item_id,
                            event_type="item.snapshot_replaced",
                            part_id="snapshot",
                            occurrence=occurrence,
                            ordinal=len(events),
                        ),
                        item_id=item_id,
                        item_kind="artifact",
                        snapshot=snapshot,
                    )
                )

        for artifact_id, artifact_state in self._artifacts.items():
            if artifact_id in snapshot_artifact_ids or artifact_state.closed:
                continue
            removed_source = source.model_copy(update={"native_item_id": artifact_id})
            artifact_state.present = False
            artifact_state.parts = {}
            artifact_state.part_order = []
            if terminal:
                artifact_state.closed = True
                events.append(
                    ItemFailed(
                        **self._envelope(
                            context,
                            removed_source,
                            timestamp,
                            item_id=artifact_state.item_id,
                            event_type="item.failed",
                            part_id="artifact",
                            occurrence=occurrence,
                            ordinal=len(events),
                        ),
                        item_id=artifact_state.item_id,
                        item_kind="artifact",
                        error=ErrorInfo(
                            code="a2a_artifact_removed_by_snapshot",
                            message=(
                                "provisional artifact absent from authoritative GetTask snapshot"
                            ),
                            source="a2a",
                            scope_id=context.scope_id,
                            item_id=artifact_state.item_id,
                            source_ref=removed_source,
                        ),
                    )
                )
            else:
                events.append(
                    ItemSnapshotReplaced(
                        **self._envelope(
                            context,
                            removed_source,
                            timestamp,
                            item_id=artifact_state.item_id,
                            event_type="item.snapshot_replaced",
                            part_id="snapshot",
                            occurrence=occurrence,
                            ordinal=len(events),
                        ),
                        item_id=artifact_state.item_id,
                        item_kind="artifact",
                        snapshot=ContentSnapshot(parts=()),
                    )
                )

        interaction_state = state in {
            TaskState.TASK_STATE_INPUT_REQUIRED,
            TaskState.TASK_STATE_AUTH_REQUIRED,
        }
        status_message_id = (
            task.status.message.message_id if task.status.HasField("message") else ""
        )
        snapshot_messages = [
            message
            for message in task.history
            if not (interaction_state and message.message_id == status_message_id)
        ]
        if (
            not interaction_state
            and task.status.HasField("message")
            and all(
                message.message_id != task.status.message.message_id
                for message in snapshot_messages
            )
        ):
            snapshot_messages.append(task.status.message)
        for nested_message in snapshot_messages:
            message = self._normalize_nested_message(nested_message, context)
            if message.role != Role.ROLE_AGENT:
                continue
            message_events = self._map_message(
                message,
                context,
                timestamp,
                consistent=True,
                occurrence_identity=(
                    f"get-task:{reason}:{attempt_id}:message:{message.message_id}"
                ),
            )
            events.extend(message_events)
            message_id = self._message_item_id(
                context,
                _required_string(message.message_id, "message.message_id"),
            )
            if terminal:
                add_output_ref(message_id)

        if interaction_state:
            interaction_events = self._map_status(
                task.status,
                {"event_id": occurrence.identity},
                context,
                timestamp,
                native_item_id=status_message_id or None,
                occurrence_payload=task,
            )
            for event in interaction_events:
                events.append(event.model_copy(update={"source": source}))
            return tuple(events)

        terminal_source = source.model_copy(update={"native_item_id": task.id})
        if terminal and self._active_interaction is not None:
            interaction_id, _ = self._active_interaction
            events.append(
                InteractionResolved(
                    **self._envelope(
                        context,
                        terminal_source,
                        timestamp,
                        item_id=interaction_id,
                        event_type="interaction.resolved",
                        part_id="interaction",
                        occurrence=occurrence,
                        ordinal=len(events),
                    ),
                    interaction_id=interaction_id,
                    interaction_kind="structured_input",
                    response=StructuredInputResponse(data={"state": TaskState.Name(state)}),
                )
            )
            self._active_interaction = None
        if state == TaskState.TASK_STATE_COMPLETED:
            events.append(
                RunCompleted(
                    **self._envelope(
                        context,
                        terminal_source,
                        timestamp,
                        item_id=context.run_id,
                        event_type="run.completed",
                        part_id="run",
                        occurrence=occurrence,
                        ordinal=len(events),
                    ),
                    status="completed",
                    output_refs=tuple(output_refs),
                )
            )
        elif state in {TaskState.TASK_STATE_FAILED, TaskState.TASK_STATE_REJECTED}:
            status_message = (
                self._normalize_nested_message(task.status.message, context)
                if task.status.HasField("message")
                else None
            )
            failure_message = (
                _parts_text(status_message.parts) if status_message is not None else None
            )
            events.append(
                RunFailed(
                    **self._envelope(
                        context,
                        terminal_source,
                        timestamp,
                        item_id=context.run_id,
                        event_type="run.failed",
                        part_id="run",
                        occurrence=occurrence,
                        ordinal=len(events),
                    ),
                    status="failed",
                    error=ErrorInfo(
                        code="a2a_task_rejected"
                        if state == TaskState.TASK_STATE_REJECTED
                        else "a2a_task_failed",
                        message=failure_message,
                        source="a2a",
                        scope_id=context.scope_id,
                        source_ref=terminal_source,
                    ),
                )
            )
        elif state == TaskState.TASK_STATE_CANCELED:
            status_message = (
                self._normalize_nested_message(task.status.message, context)
                if task.status.HasField("message")
                else None
            )
            events.append(
                RunCanceled(
                    **self._envelope(
                        context,
                        terminal_source,
                        timestamp,
                        item_id=context.run_id,
                        event_type="run.canceled",
                        part_id="run",
                        occurrence=occurrence,
                        ordinal=len(events),
                    ),
                    status="canceled",
                    reason=_parts_text(status_message.parts)
                    if status_message is not None
                    else None,
                )
            )
        else:
            events.append(
                self._run_progress_event(
                    context,
                    terminal_source,
                    timestamp,
                    occurrence,
                    len(events),
                    message=f"authoritative {TaskState.Name(state)} snapshot",
                )
            )
        if terminal:
            self._run_interrupted = False
            self._terminal_snapshot_fingerprint = _proto_fingerprint(task)
        return tuple(events)

    def _map_message(
        self,
        message: Message,
        context: A2AAdapterContext,
        timestamp: float,
        *,
        consistent: bool,
        occurrence_identity: str | None = None,
        direct_response: bool = False,
    ) -> tuple[RuntimeEvent, ...]:
        message_id = _required_string(message.message_id, "message.message_id")
        message_metadata = _metadata(message.metadata)
        producer_event_id = _optional_metadata_string(
            message_metadata, "event_id", "ksadk_event_id"
        )
        if producer_event_id is not None:
            occurrence = self._occurrence(
                message_metadata,
                provisional_key=f"message:{message_id}",
                payload=message,
            )
            if occurrence.duplicate:
                return ()
        else:
            cursor = _optional_metadata_string(message_metadata, "seq", "ksadk_seq")
            occurrence = _Occurrence(
                native_event_id=message_id,
                native_cursor=cursor,
                identity=occurrence_identity or message_id,
                provisional=False,
            )
        signature = message.SerializeToString(deterministic=True)
        existing = self._messages.get(message_id)
        if existing is not None:
            if existing.signature != signature:
                raise A2AMappingError(
                    "message_identity_collision",
                    "message.message_id",
                    f"A2A message {message_id!r} changed after completion",
                )
            return ()
        if producer_event_id is not None and occurrence_identity is not None:
            occurrence = _Occurrence(
                native_event_id=occurrence.native_event_id,
                native_cursor=occurrence.native_cursor,
                identity=occurrence_identity,
                provisional=False,
            )
        item_id = self._message_item_id(context, message_id)
        parts = self._convert_parts(message, item_id)
        if not parts:
            raise A2AMappingError(
                "empty_message",
                "message.parts",
                "A2A message requires at least one supported part",
            )
        _validate_unique_parts(parts, "message.parts")
        source = self._source(
            context,
            occurrence,
            native_item_id=message_id,
            metadata={
                "provisional": False,
                "consistent": consistent,
                "role": Role.Name(message.role),
            },
        )
        events: list[RuntimeEvent] = []
        if direct_response and not self._run_started:
            events.append(
                self._run_started_event(
                    context,
                    source,
                    timestamp,
                    occurrence,
                    len(events),
                )
            )
            self._run_started = True
        events.append(
            ItemStarted(
                **self._envelope(
                    context,
                    source,
                    timestamp,
                    item_id=item_id,
                    event_type="item.started",
                    part_id="message",
                    occurrence=occurrence,
                    ordinal=0,
                ),
                item_id=item_id,
                item_kind="message",
                phase="final_answer",
            )
        )
        for index, part in enumerate(parts, start=1):
            events.append(
                ItemUpdated(
                    **self._envelope(
                        context,
                        source,
                        timestamp,
                        item_id=item_id,
                        event_type="item.updated",
                        part_id=part.part_id,
                        occurrence=occurrence,
                        ordinal=index,
                    ),
                    item_id=item_id,
                    item_kind="message",
                    op="replace",
                    update=part,
                )
            )
        events.append(
            ItemCompleted(
                **self._envelope(
                    context,
                    source,
                    timestamp,
                    item_id=item_id,
                    event_type="item.completed",
                    part_id="snapshot",
                    occurrence=occurrence,
                    ordinal=len(parts) + 1,
                ),
                item_id=item_id,
                item_kind="message",
                snapshot=ContentSnapshot(parts=parts),
            )
        )
        self._messages[message_id] = _MessageState(signature=signature)
        if direct_response:
            events.append(
                RunCompleted(
                    **self._envelope(
                        context,
                        source,
                        timestamp,
                        item_id=context.run_id,
                        event_type="run.completed",
                        part_id="run",
                        occurrence=occurrence,
                        ordinal=len(parts) + 2,
                    ),
                    status="completed",
                    output_refs=(OutputRef(scope_id=context.scope_id, item_id=item_id),),
                )
            )
        return tuple(events)

    def _convert_parts(
        self,
        owner: Artifact | Message,
        item_id: str,
        *,
        start_index: int = 0,
    ) -> tuple[ContentValue, ...]:
        converted: list[ContentValue] = []
        for index, part in enumerate(owner.parts, start=start_index):
            converted.append(self._convert_part(owner, part, item_id, index))
        return tuple(converted)

    def _convert_part(
        self,
        owner: Artifact | Message,
        part: Part,
        item_id: str,
        index: int,
    ) -> ContentValue:
        metadata = _metadata(part.metadata)
        content_kind = part.WhichOneof("content")
        kind = _optional_metadata_string(metadata, "kind", "ksadk_kind")
        if content_kind == "text":
            native_part = _optional_metadata_string(metadata, "part_id") or f"text:{index}"
            return TextContent(
                part_id=stable_part_id("a2a", item_id, native_part),
                text=part.text,
            )
        if content_kind == "data":
            native_kind = kind or "data"
            native_part = _optional_metadata_string(metadata, "part_id") or f"{native_kind}:{index}"
            part_id = stable_part_id("a2a", item_id, native_part)
            value = cast(JsonValue, MessageToDict(part.data))
            if native_kind == "tool_call":
                call_id = _required_metadata_string(metadata, "call_id")
                name = _required_metadata_string(metadata, "name")
                return ToolCallContent(
                    part_id=part_id,
                    call_id=call_id,
                    name=name,
                    arguments=value,
                )
            if native_kind == "tool_result":
                return ToolResultContent(
                    part_id=part_id,
                    call_id=_required_metadata_string(metadata, "call_id"),
                    result=value,
                    is_error=bool(metadata.get("is_error", False)),
                )
            if native_kind != "data":
                raise A2AMappingError(
                    "unknown_part_kind",
                    "part.metadata.kind",
                    f"unsupported A2A data part kind {native_kind!r}",
                )
            return DataContent(part_id=part_id, data=value)
        if content_kind in {"url", "raw"}:
            native_part = _optional_metadata_string(metadata, "part_id") or f"file:{index}"
            artifact_id = (
                owner.artifact_id
                if isinstance(owner, Artifact)
                else f"message:{owner.message_id}:part:{index}"
            )
            name = part.filename or (owner.name if isinstance(owner, Artifact) else "attachment")
            data: JsonValue = None
            if content_kind == "raw":
                data = {"base64": base64.b64encode(part.raw).decode("ascii")}
            return ArtifactContent(
                part_id=stable_part_id("a2a", item_id, native_part),
                artifact_id=artifact_id,
                name=name,
                mime_type=part.media_type or None,
                uri=part.url if content_kind == "url" else None,
                data=data,
            )
        raise A2AMappingError(
            "empty_part",
            "parts",
            f"A2A part at index {index} has no supported payload",
        )

    def _occurrence(
        self,
        metadata: Mapping[str, JsonValue],
        *,
        provisional_key: str,
        payload: object,
    ) -> _Occurrence:
        native_event_id = _optional_metadata_string(metadata, "event_id", "ksadk_event_id")
        native_cursor = _optional_metadata_string(metadata, "seq", "ksadk_seq")
        if native_event_id is not None:
            fingerprint = _proto_fingerprint(payload)
            previous = self._seen_occurrences.get(native_event_id)
            if previous is not None:
                if previous != fingerprint:
                    raise A2AMappingError(
                        "producer_event_id_collision",
                        "metadata.event_id",
                        f"A2A producer event_id {native_event_id!r} changed payload",
                    )
                self._seen_occurrences.move_to_end(native_event_id)
                return _Occurrence(
                    native_event_id=native_event_id,
                    native_cursor=native_cursor,
                    identity=native_event_id,
                    provisional=False,
                    duplicate=True,
                )
            self._seen_occurrences[native_event_id] = fingerprint
            while len(self._seen_occurrences) > self.OCCURRENCE_CACHE_LIMIT:
                self._seen_occurrences.popitem(last=False)
            return _Occurrence(
                native_event_id=native_event_id,
                native_cursor=native_cursor,
                identity=native_event_id,
                provisional=False,
            )
        ordinal = self._provisional_ordinals.get(provisional_key, 0)
        self._provisional_ordinals[provisional_key] = ordinal + 1
        return _Occurrence(
            native_event_id=None,
            native_cursor=native_cursor,
            identity=f"provisional:{provisional_key}:{ordinal}",
            provisional=True,
        )

    @staticmethod
    def _source_metadata(
        *,
        provisional: bool,
        consistent: bool,
        artifact_closed: bool,
        artifact: Artifact,
    ) -> dict[str, JsonValue]:
        return {
            "provisional": provisional,
            "consistent": consistent,
            "artifact_closed": artifact_closed,
            "artifact_name": artifact.name,
            "artifact_description": artifact.description,
            "artifact_extensions": list(artifact.extensions),
        }

    @staticmethod
    def _source(
        context: A2AAdapterContext,
        occurrence: _Occurrence,
        *,
        native_item_id: str | None,
        metadata: Mapping[str, JsonValue],
    ) -> SourceRef:
        return SourceRef(
            framework="a2a",
            native_event_id=occurrence.native_event_id,
            native_cursor=occurrence.native_cursor,
            native_run_id=context.native_run_id,
            native_item_id=native_item_id,
            metadata=dict(metadata),
        )

    @staticmethod
    def _validate_identity(
        context_id: str,
        task_id: str,
        context: A2AAdapterContext,
    ) -> None:
        native_context = _required_string(context_id, "context_id")
        native_task = _required_string(task_id, "task_id")
        expected_task = _required_string(context.task_id, "task_id")
        if native_context != context.context_id or native_task != expected_task:
            raise A2AMappingError(
                "scope_identity_mismatch",
                "context_id/task_id",
                "A2A event identity does not match adapter context",
            )

    @staticmethod
    def _validate_message_identity(
        message: Message,
        context: A2AAdapterContext,
    ) -> None:
        native_context = _required_string(message.context_id, "message.context_id")
        if native_context != context.context_id:
            raise A2AMappingError(
                "scope_identity_mismatch",
                "message.context_id",
                "A2A Message context_id does not match adapter context",
            )
        if context.task_id is None:
            if message.task_id:
                raise A2AMappingError(
                    "scope_identity_mismatch",
                    "message.task_id",
                    "taskless A2A direct Message must not introduce a task_id",
                )
        elif message.task_id and message.task_id != context.task_id:
            raise A2AMappingError(
                "scope_identity_mismatch",
                "message.task_id",
                "A2A Message task_id does not match adapter context",
            )

    @staticmethod
    def _message_item_id(context: A2AAdapterContext, message_id: str) -> str:
        if context.task_id is not None:
            return stable_item_id(
                "a2a",
                context.context_id,
                context.task_id,
                "message",
                message_id,
            )
        return stable_item_id(
            "a2a",
            context.context_id,
            "message",
            message_id,
        )

    @staticmethod
    def _require_task_status(task: Task) -> None:
        if not task.HasField("status"):
            raise A2AMappingError(
                "missing_task_status",
                "Task.status",
                "A2A Task.status is required",
            )

    @staticmethod
    def _require_agent_message(message: Message, *, field_name: str) -> None:
        if message.role != Role.ROLE_AGENT:
            raise A2AMappingError(
                "unexpected_message_role",
                field_name,
                "A2A output Message.role must be ROLE_AGENT",
            )

    def _normalize_nested_message(
        self,
        message: Message,
        context: A2AAdapterContext,
    ) -> Message:
        if message.context_id and message.context_id != context.context_id:
            raise A2AMappingError(
                "nested_message_identity_mismatch",
                "Task Message.context_id",
                "nested A2A Message context_id does not match outer Task",
            )
        if message.task_id and message.task_id != context.task_id:
            raise A2AMappingError(
                "nested_message_identity_mismatch",
                "Task Message.task_id",
                "nested A2A Message task_id does not match outer Task",
            )
        normalized = Message()
        normalized.CopyFrom(message)
        normalized.context_id = context.context_id
        normalized.task_id = _required_string(context.task_id, "task_id")
        return normalized

    def _envelope(
        self,
        context: A2AAdapterContext,
        source: SourceRef,
        timestamp: float,
        *,
        item_id: str,
        event_type: str,
        part_id: str,
        occurrence: _Occurrence,
        ordinal: int,
    ) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "event_id": stable_event_id(
                "a2a",
                context.scope_id,
                item_id,
                event_type,
                part_id,
                occurrence.identity,
                ordinal,
            ),
            "seq": context.allocate_placeholder_seq(),
            "timestamp": timestamp,
            "run_id": context.run_id,
            "scope_id": context.scope_id,
            "source": source,
        }

    def _run_started_event(
        self,
        context: A2AAdapterContext,
        source: SourceRef,
        timestamp: float,
        occurrence: _Occurrence,
        ordinal: int,
    ) -> RunStarted:
        return RunStarted(
            **self._envelope(
                context,
                source,
                timestamp,
                item_id=context.run_id,
                event_type="run.started",
                part_id="run",
                occurrence=occurrence,
                ordinal=ordinal,
            ),
            status="running",
        )

    def _run_progress_event(
        self,
        context: A2AAdapterContext,
        source: SourceRef,
        timestamp: float,
        occurrence: _Occurrence,
        ordinal: int,
        *,
        message: str,
    ) -> RunProgress:
        return RunProgress(
            **self._envelope(
                context,
                source,
                timestamp,
                item_id=context.run_id,
                event_type="run.progress",
                part_id="run",
                occurrence=occurrence,
                ordinal=ordinal,
            ),
            status="running",
            message=message,
        )

    def _reconciliation_diagnostic(
        self,
        context: A2AAdapterContext,
        *,
        reason: ReconciliationReason,
        timestamp: float,
        error: str,
        exception_type: str,
        attempt_id: str,
    ) -> RunProgress:
        occurrence = _Occurrence(
            native_event_id=None,
            native_cursor=None,
            identity=(f"get-task:{reason}:{attempt_id}:failure:{error}:{exception_type}"),
            provisional=True,
        )
        source = self._source(
            context,
            occurrence,
            native_item_id=context.task_id,
            metadata={
                "provisional": True,
                "consistent": False,
                "reconciliation_reason": reason,
                "reconciliation_attempt_id": attempt_id,
                "reconciliation_error": exception_type,
                "mapping_error": error,
            },
        )
        return RunProgress(
            schema_version=2,
            event_id=stable_event_id(
                "a2a",
                context.scope_id,
                context.run_id,
                "run.progress",
                "run",
                occurrence.identity,
                0,
            ),
            seq=context.peek_placeholder_seq(),
            timestamp=timestamp,
            run_id=context.run_id,
            scope_id=context.scope_id,
            source=source,
            status="running",
            message="A2A GetTask reconciliation failed",
        )


_TERMINAL_STATES = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_REJECTED,
}


def _proto_fingerprint(value: object) -> str:
    serialize = getattr(value, "SerializeToString", None)
    if not callable(serialize):
        raise A2AMappingError(
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
            raise A2AMappingError(
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
        raise A2AMappingError(
            "missing_part_metadata",
            f"part.metadata.{key}",
            f"A2A typed part requires metadata {key!r}",
        )
    return value


def _required_string(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise A2AMappingError(
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
        raise A2AMappingError("invalid_timestamp", "timestamp", "timestamp must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise A2AMappingError("invalid_timestamp", "timestamp", "timestamp must be finite")
    return result


__all__ = [
    "A2AAdapterContext",
    "A2AEventAdapter",
    "A2AMappingError",
    "A2AReconciliationResult",
]
