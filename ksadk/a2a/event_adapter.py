"""Deprecated stateless facade over the sole canonical A2A mapper.

Managed invocations use events.adapters.a2a directly, retaining one adapter
per local handle. This facade only preserves older scalar helper signatures.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from a2a.types import Message, Part, Role, Task, TaskArtifactUpdateEvent, TaskState

from ksadk.events.adapters.a2a import A2AAdapterContext
from ksadk.events.adapters.a2a import A2AEventAdapter as CanonicalA2AEventAdapter
from ksadk.events.canonical import ItemCompleted, ItemUpdated, RunStarted, RuntimeEvent
from ksadk.events.content import TextContent


class A2AEventAdapter:
    def __init__(self) -> None:
        self._canonical = CanonicalA2AEventAdapter()

    @staticmethod
    def _context(kwargs):
        return A2AAdapterContext(
            run_id=kwargs["invocation_id"],
            context_id=kwargs["session_id"],
            task_id=kwargs["invocation_id"],
            initial_seq=kwargs["seq_id"],
        )

    @staticmethod
    def _decorate(event, kwargs):
        updates = {"seq": kwargs["seq_id"]}
        if kwargs.get("event_id"):
            updates["event_id"] = kwargs["event_id"]
        return event.model_copy(update=updates)

    def task_status_to_events(self, status, **kwargs):
        # A terminal task may be the only frame delivered by a legacy peer.
        # That path historically represents a completed item and must not
        # manufacture a lifecycle start after its terminal status message.
        # Streaming peers still receive RunStarted from their first
        # non-terminal snapshot, which keeps the canonical lifecycle intact.
        was_started = self._canonical._run_started
        context = self._context(kwargs)
        # The legacy scalar callback has no outer Task context. Project its
        # interaction message onto the local platform task identity; the
        # canonical mapper needs that message to identify input/approval waits.
        # Other status messages are projected separately by
        # _SpaceClientEventMixin.
        scalar_status = type(status)()
        scalar_status.CopyFrom(status)
        if scalar_status.HasField("message"):
            if status.state in {
                TaskState.TASK_STATE_INPUT_REQUIRED,
                TaskState.TASK_STATE_AUTH_REQUIRED,
            }:
                scalar_status.message.context_id = context.context_id
                scalar_status.message.task_id = context.task_id
            else:
                scalar_status.ClearField("message")
        task = Task(id=context.task_id, context_id=context.context_id, status=scalar_status)
        events = self._canonical._map_task_snapshot(
            task, context, "subscription_rebuild", "legacy", time.time()
        )
        terminal_states = {
            TaskState.TASK_STATE_COMPLETED,
            TaskState.TASK_STATE_CANCELED,
            TaskState.TASK_STATE_FAILED,
            TaskState.TASK_STATE_REJECTED,
        }
        if status.state in terminal_states and not was_started:
            return [event for event in events if not isinstance(event, RunStarted)]
        return events

    def task_status_to_event(self, status, **kwargs):
        return self._decorate(self.task_status_to_events(status, **kwargs)[-1], kwargs)

    def artifact_to_events(self, artifact, **kwargs):
        context = self._context(kwargs)
        native = TaskArtifactUpdateEvent(
            task_id=context.task_id,
            context_id=context.context_id,
            artifact=artifact,
            append=False,
            last_chunk=False,
        )
        return self._canonical.map_event(native, context, timestamp=time.time())

    def artifact_to_event(self, artifact, **kwargs):
        events = self.artifact_to_events(artifact, **kwargs)
        return self._decorate(events[-1], kwargs)

    def message_to_event(self, text, *, final, **kwargs):
        adapter, context = CanonicalA2AEventAdapter(), self._context(kwargs)
        native = Message(
            message_id=kwargs.get("event_id") or "legacy-response",
            task_id=context.task_id,
            context_id=context.context_id,
            role=Role.ROLE_AGENT,
            parts=[Part(text=text)],
        )
        events = adapter.map_event(native, context, timestamp=time.time())
        message_events = [
            event for event in events if getattr(event, "item_kind", None) == "message"
        ]
        return self._decorate(message_events[-1], kwargs)

    # ---- RuntimeEvent → A2A ----

    @staticmethod
    def event_to_text_part(event: RuntimeEvent) -> Optional[Part]:
        """RuntimeEvent(item.*,item_kind="message")→ A2A ``Part``(用于出站 message/artifact)。"""
        if isinstance(event, ItemUpdated) and event.item_kind == "message":
            if isinstance(event.update, TextContent):
                text = event.update.text
                return Part(text=text) if text else None
            return None
        if isinstance(event, ItemCompleted) and event.item_kind == "message":
            if event.snapshot.parts and isinstance(event.snapshot.parts[0], TextContent):
                text = event.snapshot.parts[0].text
                return Part(text=text) if text else None
            return None
        return None

    @staticmethod
    def _parts_text(parts: Any) -> str:
        if not parts:
            return ""
        texts = []
        for part in parts:
            value = getattr(part, "text", None)
            if value:
                texts.append(str(value))
        return "".join(texts)


__all__ = ["A2AEventAdapter"]
