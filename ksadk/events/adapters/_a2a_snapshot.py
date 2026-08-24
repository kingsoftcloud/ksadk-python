"""A2AEventAdapter 的 task 快照映射方法（纯移动自 adapters.a2a，行为不变）。

以 mixin 形式被 :class:`A2AEventAdapter` 继承。
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Literal, cast

from a2a.types import (
    Artifact,
    Message,
    Part,
    Role,
    Task,
    TaskState,
)
from google.protobuf.json_format import MessageToDict
from pydantic import JsonValue

from ksadk.events.adapters._a2a_support import (
    A2AAdapterContext,
    _ArtifactState,
    _fail,
    _MessageState,
    _metadata,
    _Occurrence,
    _optional_metadata_string,
    _parts_text,
    _proto_fingerprint,
    _required_metadata_string,
    _required_string,
    _SnapshotScope,
    _validate_unique_parts,
)
from ksadk.events.canonical import (
    ErrorInfo,
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
    RuntimeEvent,
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
    stable_item_id,
    stable_part_id,
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


class _A2ATaskSnapshotMixin:
    def _map_task_snapshot(
        self,
        task: Task,
        context: A2AAdapterContext,
        reason: ReconciliationReason,
        attempt_id: str,
        timestamp: float,
    ) -> tuple[RuntimeEvent, ...]:
        state = task.status.state
        occurrence = _Occurrence(
            native_event_id=None,
            native_cursor=None,
            identity=(
                f"get-task:{reason}:{attempt_id}:"
                f"{TaskState.Name(state)}:{_proto_fingerprint(task)}"
            ),
            provisional=False,
        )
        scope = _SnapshotScope(
            task=task,
            context=context,
            source=self._source(
                context,
                occurrence,
                native_item_id=task.id,
                metadata={
                    "provisional": False,
                    "consistent": True,
                    "reconciliation_reason": reason,
                    "reconciliation_attempt_id": attempt_id,
                    "terminal": state in _TERMINAL_STATES,
                },
            ),
            timestamp=timestamp,
            occurrence=occurrence,
            terminal=state in _TERMINAL_STATES,
            reason=reason,
            attempt_id=attempt_id,
        )
        self._ensure_run_started(scope.events, context, scope.source, timestamp, occurrence)
        self._snapshot_artifacts(scope)
        interaction_state = state in _INTERACTION_STATES
        status_message_id = (
            task.status.message.message_id if task.status.HasField("message") else ""
        )
        self._snapshot_messages(scope, interaction_state, status_message_id)
        if interaction_state:
            interaction_events = self._map_status(
                task.status,
                {"event_id": occurrence.identity},
                context,
                timestamp,
                native_item_id=status_message_id or None,
                occurrence_payload=task,
            )
            scope.events.extend(
                event.model_copy(update={"source": scope.source}) for event in interaction_events
            )
            return tuple(scope.events)
        self._snapshot_terminal_run(scope, state)
        if scope.terminal:
            self._run_interrupted = False
            self._terminal_snapshot_fingerprint = _proto_fingerprint(task)
        return tuple(scope.events)

    def _snapshot_artifacts(self, scope: _SnapshotScope) -> None:
        task, context, source, occurrence = (
            scope.task,
            scope.context,
            scope.source,
            scope.occurrence,
        )
        env = self._env_builder(context, source, scope.timestamp, occurrence)
        snapshot_artifact_ids: set[str] = set()

        for artifact in task.artifacts:
            artifact_id = _required_string(artifact.artifact_id, "task.artifacts.artifact_id")
            snapshot_artifact_ids.add(artifact_id)
            item_id = stable_item_id(
                "a2a", context.context_id, context.task_id, "artifact", artifact_id
            )
            parts = self._convert_parts(artifact, item_id, start_index=0)
            if not parts:
                _fail(
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
                scope.events.append(
                    ItemStarted(
                        **env(
                            item_id, "item.started", "artifact", len(scope.events), artifact_source
                        ),
                        item_id=item_id,
                        item_kind="artifact",
                        phase="final_answer",
                    )
                )
            elif artifact_state.closed:
                if artifact_state.snapshot() != snapshot:
                    _fail(
                        "trusted_snapshot_collision",
                        "task.artifacts",
                        f"GetTask changed already completed artifact {artifact_id!r}",
                    )
                if scope.terminal:
                    scope.add_output_ref(item_id)
                continue
            artifact_state.parts = {part.part_id: part for part in parts}
            artifact_state.part_order = [part.part_id for part in parts]
            artifact_state.present = True
            if scope.terminal:
                artifact_state.closed = True
                scope.events.append(
                    ItemCompleted(
                        **env(
                            item_id,
                            "item.completed",
                            "snapshot",
                            len(scope.events),
                            artifact_source,
                        ),
                        item_id=item_id,
                        item_kind="artifact",
                        snapshot=snapshot,
                    )
                )
                scope.add_output_ref(item_id)
            else:
                scope.events.append(
                    ItemSnapshotReplaced(
                        **env(
                            item_id,
                            "item.snapshot_replaced",
                            "snapshot",
                            len(scope.events),
                            artifact_source,
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
            if scope.terminal:
                artifact_state.closed = True
                scope.events.append(
                    ItemFailed(
                        **env(
                            artifact_state.item_id,
                            "item.failed",
                            "artifact",
                            len(scope.events),
                            removed_source,
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
                scope.events.append(
                    ItemSnapshotReplaced(
                        **env(
                            artifact_state.item_id,
                            "item.snapshot_replaced",
                            "snapshot",
                            len(scope.events),
                            removed_source,
                        ),
                        item_id=artifact_state.item_id,
                        item_kind="artifact",
                        snapshot=ContentSnapshot(parts=()),
                    )
                )

    def _snapshot_messages(
        self,
        scope: _SnapshotScope,
        interaction_state: bool,
        status_message_id: str,
    ) -> None:
        task = scope.task
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
            message = self._normalize_nested_message(nested_message, scope.context)
            if message.role != Role.ROLE_AGENT:
                continue
            scope.events.extend(
                self._map_message(
                    message,
                    scope.context,
                    scope.timestamp,
                    consistent=True,
                    occurrence_identity=(
                        f"get-task:{scope.reason}:{scope.attempt_id}:message:{message.message_id}"
                    ),
                )
            )
            message_id = self._message_item_id(
                scope.context, _required_string(message.message_id, "message.message_id")
            )
            if scope.terminal:
                scope.add_output_ref(message_id)

    def _snapshot_terminal_run(self, scope: _SnapshotScope, state: TaskState) -> None:
        context, events = scope.context, scope.events
        terminal_source = scope.source.model_copy(update={"native_item_id": scope.task.id})
        env = self._env_builder(context, terminal_source, scope.timestamp, scope.occurrence)

        if state in _TERMINAL_STATES and self._active_interaction is not None:
            interaction_id, _ = self._active_interaction
            events.append(
                InteractionResolved(
                    **env(interaction_id, "interaction.resolved", "interaction", len(events)),
                    interaction_id=interaction_id,
                    interaction_kind="structured_input",
                    response=StructuredInputResponse(data={"state": TaskState.Name(state)}),
                )
            )
            self._active_interaction = None

        def status_text() -> str | None:
            if not scope.task.status.HasField("message"):
                return None
            return _parts_text(
                self._normalize_nested_message(scope.task.status.message, context).parts
            )

        if state == TaskState.TASK_STATE_COMPLETED:
            events.append(
                RunCompleted(
                    **env(context.run_id, "run.completed", "run", len(events)),
                    status="completed",
                    output_refs=tuple(scope._output_refs),
                )
            )
        elif state in {TaskState.TASK_STATE_FAILED, TaskState.TASK_STATE_REJECTED}:
            events.append(
                RunFailed(
                    **env(context.run_id, "run.failed", "run", len(events)),
                    status="failed",
                    error=ErrorInfo(
                        code=(
                            "a2a_task_rejected"
                            if state == TaskState.TASK_STATE_REJECTED
                            else "a2a_task_failed"
                        ),
                        message=status_text(),
                        source="a2a",
                        scope_id=context.scope_id,
                        source_ref=terminal_source,
                    ),
                )
            )
        elif state == TaskState.TASK_STATE_CANCELED:
            events.append(
                RunCanceled(
                    **env(context.run_id, "run.canceled", "run", len(events)),
                    status="canceled",
                    reason=status_text(),
                )
            )
        else:
            events.append(
                self._run_progress_event(
                    context,
                    terminal_source,
                    scope.timestamp,
                    scope.occurrence,
                    len(events),
                    message=f"authoritative {TaskState.Name(state)} snapshot",
                )
            )

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
                message_metadata, provisional_key=f"message:{message_id}", payload=message
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
                _fail(
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
            _fail(
                "empty_message", "message.parts", "A2A message requires at least one supported part"
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

        env = self._env_builder(context, source, timestamp, occurrence)
        events: list[RuntimeEvent] = []
        if direct_response:
            self._ensure_run_started(events, context, source, timestamp, occurrence)
        events.append(
            ItemStarted(
                **env(item_id, "item.started", "message", 0),
                item_id=item_id,
                item_kind="message",
                phase="final_answer",
            )
        )
        for index, part in enumerate(parts, start=1):
            events.append(
                ItemUpdated(
                    **env(item_id, "item.updated", part.part_id, index),
                    item_id=item_id,
                    item_kind="message",
                    op="replace",
                    update=part,
                )
            )
        events.append(
            ItemCompleted(
                **env(item_id, "item.completed", "snapshot", len(parts) + 1),
                item_id=item_id,
                item_kind="message",
                snapshot=ContentSnapshot(parts=parts),
            )
        )
        self._messages[message_id] = _MessageState(signature=signature)
        if direct_response:
            events.append(
                RunCompleted(
                    **env(context.run_id, "run.completed", "run", len(parts) + 2),
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
                return ToolCallContent(
                    part_id=part_id,
                    call_id=_required_metadata_string(metadata, "call_id"),
                    name=_required_metadata_string(metadata, "name"),
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
                _fail(
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
        _fail(
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
                    _fail(
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
