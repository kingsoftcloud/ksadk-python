"""A2ASpaceClient 的事件投影与持久化实现（纯移动自 ``ksadk.a2a.space_client``，行为不变）。

以 mixin 形式被 :class:`A2ASpaceClient` 继承，依赖宿主提供 ``_event_adapter`` /
``_event_dispatcher`` / ``_event_sink`` / ``_persisted_wire_events`` / ``_space_id`` /
``_seq`` / ``_backend`` 及校验辅助方法。
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from a2a.types import TaskState
from google.protobuf.json_format import MessageToDict

from ksadk.a2a.control_plane import DiscoveredAgent
from ksadk.a2a.event_adapter import A2AEventAdapter
from ksadk.events.runtime_event import RuntimeEvent

if TYPE_CHECKING:
    pass


def _utc_now() -> str:
    from ksadk.a2a.space_client import _utc_now as _impl

    return _impl()


def _canonical_proto(value: Any) -> dict[str, Any]:
    from ksadk.a2a.space_client import _canonical_proto as _impl

    return _impl(value)


def _present_message_field(value: Any, field_name: str) -> Any | None:
    from ksadk.a2a.space_client import _present_message_field as _impl

    return _impl(value, field_name)


class _SpaceClientEventMixin:
    async def _project_stream_item(
        self,
        platform_task_id: str,
        item: Any,
        agent: DiscoveredAgent,
        *,
        wire_position: int,
        operation_instance_id: str,
    ) -> list[RuntimeEvent]:
        runtime_events = self._stream_item_to_events(
            item,
            agent,
            wire_position=wire_position,
            invocation_id=platform_task_id,
        )
        platform_events = self._platform_events(
            item,
            platform_task_id,
            operation_instance_id=operation_instance_id,
            wire_position=wire_position,
        )
        if platform_events:
            await self._event_dispatcher.enqueue(
                platform_task_id=platform_task_id,
                events=platform_events,
            )
        return await self._persist_events(runtime_events)

    def _platform_events(
        self,
        item: Any,
        platform_task_id: str,
        *,
        operation_instance_id: str,
        wire_position: int,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []

        def append_event(
            kind: str,
            payload: dict[str, Any],
            *,
            status: str | None = None,
            occurred_at: str | None = None,
        ) -> None:
            events.append(
                self._platform_event(
                    kind,
                    payload,
                    platform_task_id,
                    operation_instance_id=operation_instance_id,
                    wire_position=wire_position,
                    event_ordinal=len(events),
                    status=status,
                    occurred_at=occurred_at,
                )
            )

        task = _present_message_field(item, "task")
        if task is None and hasattr(item, "status") and hasattr(item, "id"):
            task = item
        status_update = _present_message_field(item, "status_update")
        artifact_update = _present_message_field(item, "artifact_update")
        message = _present_message_field(item, "message")
        if task is not None and getattr(task, "status", None) is not None:
            payload = _canonical_proto(task.status)
            state_name = TaskState.Name(task.status.state)
            append_event(
                "status",
                payload,
                status=state_name.removeprefix("TASK_STATE_").lower(),
                occurred_at=str(payload.get("timestamp") or _utc_now()),
            )
            for artifact in getattr(task, "artifacts", None) or []:
                append_event(
                    "artifact",
                    {
                        "Artifact": _canonical_proto(artifact),
                        "Append": False,
                        "LastChunk": True,
                    },
                )
        if status_update is not None and getattr(status_update, "status", None) is not None:
            payload = _canonical_proto(status_update.status)
            state_name = TaskState.Name(status_update.status.state)
            append_event(
                "status",
                payload,
                status=state_name.removeprefix("TASK_STATE_").lower(),
                occurred_at=str(payload.get("timestamp") or _utc_now()),
            )
        if artifact_update is not None and getattr(artifact_update, "artifact", None) is not None:
            append_event(
                "artifact",
                {
                    "Artifact": _canonical_proto(artifact_update.artifact),
                    "Append": bool(getattr(artifact_update, "append", False)),
                    "LastChunk": bool(getattr(artifact_update, "last_chunk", False)),
                },
            )
        if message is not None:
            payload = _canonical_proto(message)
            append_event("message", payload)
            append_event(
                "status",
                {"state": "TASK_STATE_COMPLETED", "message": payload},
                status="completed",
            )
        return events

    @staticmethod
    def _platform_event(
        kind: str,
        payload: dict[str, Any],
        platform_task_id: str,
        *,
        operation_instance_id: str,
        wire_position: int,
        event_ordinal: int,
        status: str | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        source_id = hashlib.sha256(
            (
                f"{platform_task_id}:{operation_instance_id}:{wire_position}:{event_ordinal}:{kind}"
            ).encode("utf-8")
        ).hexdigest()
        event: dict[str, Any] = {
            "SourceEventId": source_id,
            "EventKind": kind,
            "Payload": payload,
            "OccurredAt": occurred_at or _utc_now(),
        }
        if status:
            event["Status"] = status
        return event

    async def flush_pending_events(self) -> int:
        """Deliver all currently queued platform event batches or raise on failure."""

        return await self._event_dispatcher.drain(raise_on_error=True)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _event_ctx(
        self,
        agent: DiscoveredAgent,
        invocation_id: str,
        *,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "agent_id": agent.agent_id,
            "user_id": "a2a_space",
            "session_id": self._space_id,
            "invocation_id": invocation_id,
            "seq_id": self._next_seq(),
            "event_id": event_id,
        }

    def task_to_event(self, task: Any, agent: DiscoveredAgent) -> RuntimeEvent:
        return self._event_adapter.task_status_to_event(
            task.status, **self._event_ctx(agent, invocation_id=str(task.id))
        )

    def _stream_item_to_events(
        self,
        item: Any,
        agent: DiscoveredAgent,
        *,
        wire_position: int = 0,
        invocation_id: str | None = None,
    ) -> list[RuntimeEvent]:
        events: list[RuntimeEvent] = []
        task = _present_message_field(item, "task")
        if task is None and hasattr(item, "status") and hasattr(item, "id"):
            task = item
        status_update = _present_message_field(item, "status_update")
        artifact_update = _present_message_field(item, "artifact_update")
        message = _present_message_field(item, "message")
        resolved_invocation_id = invocation_id or str(
            getattr(item, "task_id", None)
            or getattr(task, "id", "")
            or getattr(status_update, "task_id", "")
            or getattr(artifact_update, "task_id", "")
            or getattr(message, "task_id", "")
            or ""
        )

        def ctx(kind: str, value: Any) -> dict[str, Any]:
            metadata = getattr(value, "metadata", None)
            native_event_id = ""
            if metadata is not None:
                if isinstance(metadata, Mapping):
                    metadata_dict = dict(metadata)
                else:
                    try:
                        metadata_dict = MessageToDict(metadata, preserving_proto_field_name=True)
                    except (AttributeError, TypeError, ValueError):
                        metadata_dict = {}
                native_event_id = str(
                    metadata_dict.get("event_id") or metadata_dict.get("ksadk_event_id") or ""
                )
            message_id = str(getattr(value, "message_id", "") or "")
            artifact = getattr(value, "artifact", None)
            artifact_id = str(
                getattr(value, "artifact_id", "") or getattr(artifact, "artifact_id", "") or ""
            )
            source_id = native_event_id or message_id or artifact_id
            event_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"ksadk:a2a:{resolved_invocation_id}:{wire_position}:{kind}:{source_id}",
            ).hex
            return self._event_ctx(agent, invocation_id=resolved_invocation_id, event_id=event_id)

        if task is not None and getattr(task, "status", None) is not None:
            task_status_message = _present_message_field(task.status, "message")
            task_status_text = A2AEventAdapter._parts_text(
                getattr(task_status_message, "parts", None)
            )
            task_is_terminal = task.status.state in {
                TaskState.TASK_STATE_COMPLETED,
                TaskState.TASK_STATE_FAILED,
                TaskState.TASK_STATE_CANCELED,
                TaskState.TASK_STATE_REJECTED,
            }
            if not task_is_terminal:
                events.append(
                    self._event_adapter.task_status_to_event(
                        task.status,
                        **ctx("task", task),
                    )
                )
            if task_status_text:
                events.append(
                    self._event_adapter.message_to_event(
                        task_status_text,
                        final=task_is_terminal,
                        **ctx("task-status-message", task_status_message),
                    )
                )
            if task_is_terminal:
                events.append(
                    self._event_adapter.task_status_to_event(
                        task.status,
                        **ctx("task", task),
                    )
                )
        if status_update is not None and getattr(status_update, "status", None) is not None:
            status_message = _present_message_field(status_update.status, "message")
            text = A2AEventAdapter._parts_text(getattr(status_message, "parts", None))
            terminal_states = {
                TaskState.TASK_STATE_COMPLETED,
                TaskState.TASK_STATE_FAILED,
                TaskState.TASK_STATE_CANCELED,
                TaskState.TASK_STATE_REJECTED,
            }
            is_terminal = status_update.status.state in terminal_states
            if not is_terminal:
                events.append(
                    self._event_adapter.task_status_to_event(
                        status_update.status, **ctx("status", status_update)
                    )
                )
            if text:
                events.append(
                    self._event_adapter.message_to_event(
                        text,
                        final=is_terminal,
                        **ctx("status-message", status_message),
                    )
                )
            if is_terminal:
                events.append(
                    self._event_adapter.task_status_to_event(
                        status_update.status, **ctx("status", status_update)
                    )
                )
        if artifact_update is not None and getattr(artifact_update, "artifact", None) is not None:
            artifact = artifact_update.artifact
            events.append(
                self._event_adapter.artifact_to_event(artifact, **ctx("artifact", artifact_update))
            )
            artifact_text = A2AEventAdapter._parts_text(getattr(artifact, "parts", None))
            if artifact_text and str(getattr(artifact, "name", "") or "") == "response":
                events.append(
                    self._event_adapter.message_to_event(
                        artifact_text,
                        final=bool(getattr(artifact_update, "last_chunk", False)),
                        **ctx("artifact-text", artifact_update),
                    )
                )
        if message is not None:
            text = A2AEventAdapter._parts_text(getattr(message, "parts", None))
            if text:
                events.append(
                    self._event_adapter.message_to_event(
                        text, final=True, **ctx("message", message)
                    )
                )
        return events

    async def _persist_events(self, events: list[RuntimeEvent]) -> list[RuntimeEvent]:
        existing_ids = set(self._persisted_wire_events)
        if self._event_sink is not None:
            list_events = getattr(self._event_sink, "list", None)
            if callable(list_events) and events:
                session_id = str(events[0].source.metadata.get("session_id") or self._space_id)
                persisted_before = await list_events(session_id)
                existing_ids.update(event.event_id for event in persisted_before)
        fresh = [event for event in events if event.event_id not in existing_ids]
        if not fresh:
            return []
        if self._event_sink is not None:
            append = getattr(self._event_sink, "append", None)
            if append is None:
                raise TypeError("event_sink must provide async append(events)")
            # RuntimeEventStore.append(session_id, events) requires session_id;
            # fall back to single-arg call for non-canonical sinks.
            session_id_for_persist = (
                str(events[0].source.metadata.get("session_id") or self._space_id)
                if events
                else self._space_id
            )
            try:
                persisted = await append(session_id_for_persist, fresh)
            except TypeError:
                persisted = await append(fresh)
            if persisted is not None:
                fresh = list(persisted)
        self._persisted_wire_events.update(event.event_id for event in fresh)
        return fresh


__all__ = ["_SpaceClientEventMixin"]
