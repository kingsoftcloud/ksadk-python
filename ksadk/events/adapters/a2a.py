"""A2A SDK 1.1.0 protobuf events to RuntimeEvent schema version 2."""

from __future__ import annotations

import copy
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any, Literal

from a2a.types import (
    Artifact,
    GetTaskRequest,
    Message,
    Role,
    StreamResponse,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from pydantic import JsonValue

from ksadk.events.adapters._a2a_snapshot import _A2ATaskSnapshotMixin
from ksadk.events.adapters._a2a_support import (
    A2AAdapterContext,
    A2AMappingError,
    A2AReconciliationResult,
    _A2AClient,
    _ArtifactState,
    _fail,
    _MessageState,
    _metadata,
    _Occurrence,
    _parts_text,
    _proto_fingerprint,
    _required_string,
    _status_message_id,
    _timestamp,
    _validate_unique_parts,
)
from ksadk.events.canonical import (
    ContinuationCreated,
    ContinuationResumed,
    InteractionRequested,
    InteractionResolved,
    ItemSnapshotReplaced,
    ItemStarted,
    ItemUpdated,
    RunInterrupted,
    RunProgress,
    RunStarted,
    RuntimeEvent,
    SourceRef,
    StructuredInputRequest,
    StructuredInputResponse,
)
from ksadk.events.identity import (
    stable_event_id,
    stable_item_id,
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


class A2AEventAdapter(_A2ATaskSnapshotMixin):
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
                _fail(
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
                native_event, context, timestamp, consistent=True, direct_response=True
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
        _fail(
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
                    _fail(
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
                task, shadow_context, reason, resolved_attempt_id, timestamp
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

    def _ensure_run_started(
        self,
        events: list[RuntimeEvent],
        context: A2AAdapterContext,
        source: SourceRef,
        timestamp: float,
        occurrence: _Occurrence,
    ) -> None:
        if not self._run_started:
            events.append(
                self._run_started_event(context, source, timestamp, occurrence, len(events))
            )
            self._run_started = True

    def _env_builder(
        self,
        context: A2AAdapterContext,
        source: SourceRef,
        timestamp: float,
        occurrence: _Occurrence,
    ) -> Any:
        """Return a closure building envelope kwargs for one event burst."""

        def env(
            item_id: str,
            event_type: str,
            part_id: str,
            ordinal: int,
            src: SourceRef = source,
        ) -> dict[str, Any]:
            return self._envelope(
                context,
                src,
                timestamp,
                item_id=item_id,
                event_type=event_type,
                part_id=part_id,
                occurrence=occurrence,
                ordinal=ordinal,
            )

        return env

    def _map_artifact_update(
        self,
        update: TaskArtifactUpdateEvent,
        context: A2AAdapterContext,
        timestamp: float,
    ) -> tuple[RuntimeEvent, ...]:
        self._validate_identity(update.context_id, update.task_id, context)
        if not update.HasField("artifact"):
            _fail("missing_artifact", "artifact", "A2A artifact update requires artifact")
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
            _fail(
                "artifact_missing",
                "append",
                "A2A artifact_missing: append=True requires an authoritative base",
            )
        if state is not None and state.closed:
            _fail(
                "artifact_already_closed",
                "artifact.artifact_id",
                f"A2A artifact {artifact_id!r} is already closed",
            )
        item_id = stable_item_id(
            "a2a", context.context_id, context.task_id, "artifact", artifact_id
        )
        source = self._source(
            context,
            occurrence,
            native_item_id=artifact_id,
            metadata=self._source_metadata(
                provisional=occurrence.provisional,
                consistent=False,
                artifact_closed=bool(update.last_chunk),
                artifact=artifact,
            ),
        )

        env = self._env_builder(context, source, timestamp, occurrence)

        events: list[RuntimeEvent] = []
        if state is None:
            state = _ArtifactState(artifact_id=artifact_id, item_id=item_id)
            self._artifacts[artifact_id] = state
            events.append(
                ItemStarted(
                    **env(item_id, "item.started", "artifact", 0),
                    item_id=item_id,
                    item_kind="artifact",
                    phase="final_answer",
                )
            )

        absolute_start = len(state.part_order) if update.append else 0
        converted = self._convert_parts(artifact, item_id, start_index=absolute_start)
        if not converted:
            _fail(
                "empty_artifact",
                "artifact.parts",
                "A2A artifact requires at least one supported part",
            )
        _validate_unique_parts(converted, "artifact.parts")
        previous_parts = dict(state.parts)
        for part in converted:
            previous = previous_parts.get(part.part_id)
            if previous is not None and previous.content_type != part.content_type:
                _fail(
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
                    **env(item_id, "item.snapshot_replaced", "snapshot", 1),
                    item_id=item_id,
                    item_kind="artifact",
                    snapshot=state.snapshot(),
                )
            )
        else:
            for index, part in enumerate(converted):
                if part.part_id in state.parts:
                    _fail(
                        "part_identity_collision",
                        "artifact.parts",
                        f"A2A append reused existing part {part.part_id!r}",
                    )
                state.parts[part.part_id] = part
                state.part_order.append(part.part_id)
                events.append(
                    ItemUpdated(
                        **env(item_id, "item.updated", part.part_id, index + 1),
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
            _fail("missing_status", "status", "A2A status update requires status")
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
            metadata, provisional_key="status", payload=occurrence_payload
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
        if state in _ACTIVE_STATES:
            return self._map_active_status(state, context, source, timestamp, occurrence)
        if state in _INTERACTION_STATES:
            return self._map_interaction_status(
                status, state, context, source, timestamp, occurrence
            )
        if state in _TERMINAL_STATES:
            return self._map_awaiting_terminal_status(context, source, timestamp, occurrence)
        _fail("unknown_task_state", "status.state", f"unsupported A2A TaskState {state}")

    def _map_active_status(
        self,
        state: TaskState,
        context: A2AAdapterContext,
        source: SourceRef,
        timestamp: float,
        occurrence: _Occurrence,
    ) -> tuple[RuntimeEvent, ...]:
        env = self._env_builder(context, source, timestamp, occurrence)
        events: list[RuntimeEvent] = []
        if self._active_interaction is not None:
            interaction_id, continuation_id = self._active_interaction
            events.append(
                InteractionResolved(
                    **env(interaction_id, "interaction.resolved", "interaction", len(events)),
                    interaction_id=interaction_id,
                    interaction_kind="structured_input",
                    response=StructuredInputResponse(data={"state": TaskState.Name(state)}),
                )
            )
            events.append(
                ContinuationResumed(
                    **env(continuation_id, "continuation.resumed", "continuation", len(events)),
                    continuation_id=continuation_id,
                    continuation_kind="task_resume",
                    resume_attempt_id=stable_item_id(
                        "a2a", context.scope_id, continuation_id, occurrence.identity
                    ),
                )
            )
            self._active_interaction = None
        if not self._run_started:
            self._ensure_run_started(events, context, source, timestamp, occurrence)
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

    def _map_interaction_status(
        self,
        status: TaskStatus,
        state: TaskState,
        context: A2AAdapterContext,
        source: SourceRef,
        timestamp: float,
        occurrence: _Occurrence,
    ) -> tuple[RuntimeEvent, ...]:
        if not status.HasField("message"):
            _fail(
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
            _fail(
                "interaction_payload_collision",
                "status.message",
                f"A2A interaction payload changed for message {message_id!r}",
            )
        self._interaction_payloads[lifecycle_key] = payload_fingerprint
        env = self._env_builder(context, source, timestamp, occurrence)
        events: list[RuntimeEvent] = []
        if self._active_interaction is not None:
            previous_interaction_id, _ = self._active_interaction
            events.append(
                InteractionResolved(
                    **env(
                        previous_interaction_id, "interaction.resolved", "interaction", len(events)
                    ),
                    interaction_id=previous_interaction_id,
                    interaction_kind="structured_input",
                    response=StructuredInputResponse(
                        data={"state": "SUPERSEDED_BY_NEW_A2A_INTERACTION"}
                    ),
                )
            )
            self._active_interaction = None
        self._ensure_run_started(events, context, source, timestamp, occurrence)
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
            schema = {"type": "object" if state == TaskState.TASK_STATE_AUTH_REQUIRED else "string"}
        events.append(
            InteractionRequested(
                **env(interaction_id, "interaction.requested", "interaction", len(events)),
                interaction_id=interaction_id,
                interaction_kind="structured_input",
                request=StructuredInputRequest(prompt=prompt, schema=schema),
            )
        )
        events.append(
            ContinuationCreated(
                **env(continuation_id, "continuation.created", "continuation", len(events)),
                continuation_id=continuation_id,
                continuation_kind="task_resume",
                resumable=True,
                ref={"context_id": context.context_id, "task_id": context.task_id},
            )
        )
        if not self._run_interrupted:
            events.append(
                RunInterrupted(
                    **env(context.run_id, "run.interrupted", "run", len(events)),
                    status="interrupted",
                    reason=TaskState.Name(state),
                    interaction_id=interaction_id,
                    continuation_id=continuation_id,
                )
            )
            self._run_interrupted = True
        self._active_interaction = (interaction_id, continuation_id)
        return tuple(events)

    def _map_awaiting_terminal_status(
        self,
        context: A2AAdapterContext,
        source: SourceRef,
        timestamp: float,
        occurrence: _Occurrence,
    ) -> tuple[RuntimeEvent, ...]:
        events: list[RuntimeEvent] = []
        self._ensure_run_started(events, context, source, timestamp, occurrence)
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
            _fail(
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
            _fail(
                "scope_identity_mismatch",
                "message.context_id",
                "A2A Message context_id does not match adapter context",
            )
        if context.task_id is None:
            if message.task_id:
                _fail(
                    "scope_identity_mismatch",
                    "message.task_id",
                    "taskless A2A direct Message must not introduce a task_id",
                )
        elif message.task_id and message.task_id != context.task_id:
            _fail(
                "scope_identity_mismatch",
                "message.task_id",
                "A2A Message task_id does not match adapter context",
            )

    @staticmethod
    def _message_item_id(context: A2AAdapterContext, message_id: str) -> str:
        if context.task_id is not None:
            return stable_item_id("a2a", context.context_id, context.task_id, "message", message_id)
        return stable_item_id("a2a", context.context_id, "message", message_id)

    @staticmethod
    def _require_task_status(task: Task) -> None:
        if not task.HasField("status"):
            _fail("missing_task_status", "Task.status", "A2A Task.status is required")

    @staticmethod
    def _require_agent_message(message: Message, *, field_name: str) -> None:
        if message.role != Role.ROLE_AGENT:
            _fail(
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
            _fail(
                "nested_message_identity_mismatch",
                "Task Message.context_id",
                "nested A2A Message context_id does not match outer Task",
            )
        if message.task_id and message.task_id != context.task_id:
            _fail(
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
        env = self._env_builder(context, source, timestamp, occurrence)
        return RunStarted(**env(context.run_id, "run.started", "run", ordinal), status="running")

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
        env = self._env_builder(context, source, timestamp, occurrence)
        return RunProgress(
            **env(context.run_id, "run.progress", "run", ordinal),
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


__all__ = [
    "A2AAdapterContext",
    "A2AEventAdapter",
    "A2AMappingError",
    "A2AReconciliationResult",
]
