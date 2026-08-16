"""A2AEventAdapter — A2A Message/Task/Artifact 与 RuntimeEvent 的双向转换 (goal-05)。

契约 §3.2:``A2AEventAdapter`` 负责 A2A wire 对象与 G0.2 RuntimeEvent 的双向转换;
§7.2:artifact/message ↔ RuntimeEvent artifact/text/data。

方向:
- ``task_status_to_event``:A2A TaskStatus/TaskState → RuntimeEvent(run.*)。
- ``artifact_to_event``:A2A Artifact → RuntimeEvent(item.*,item_kind="artifact")。
- ``message_to_event``:A2A Message → RuntimeEvent(item.*,item_kind="message")。
- ``event_to_text_part``:RuntimeEvent(item.*,item_kind="message")→ A2A ``Part``(用于出站)。

wire 对象是 protobuf(``a2a_pb2``);文本用 ``Part(text=...)``。
"""

from __future__ import annotations

import time
from typing import Any, Optional

from a2a.types import Part, TaskState, TaskStatus

from ksadk.events.canonical import (
    ContentSnapshot,
    ErrorInfo,
    ItemCompleted,
    ItemStarted,
    ItemUpdated,
    RunCanceled,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RunProgress,
    RunStarted,
    RuntimeEvent,
    SourceRef,
)
from ksadk.events.content import TextContent
from ksadk.events.identity import stable_event_id, stable_item_id, stable_scope_id


class A2AEventAdapter:
    """A2A wire 对象 ↔ RuntimeEvent 双向转换器。"""

    # ---- A2A → RuntimeEvent ----

    def task_status_to_event(
        self,
        status: TaskStatus,
        *,
        agent_id: str,
        user_id: str,
        session_id: str,
        invocation_id: str,
        seq_id: int,
        event_id: Optional[str] = None,
    ) -> RuntimeEvent:
        """A2A TaskStatus → canonical RuntimeEvent(run.*)。"""
        state = status.state
        state_name = TaskState.Name(state) if state is not None else "unknown"
        scope_id = stable_scope_id("a2a", session_id, invocation_id)
        run_id = invocation_id
        source = SourceRef(
            framework="a2a",
            native_run_id=invocation_id,
            metadata={"agent_id": agent_id, "user_id": user_id, "status": state_name},
        )
        timestamp = time.time()
        eid = event_id or stable_event_id(
            "a2a", scope_id, run_id, "run", "run", invocation_id, seq_id
        )
        common: dict[str, Any] = {
            "schema_version": 2,
            "event_id": eid,
            "seq": seq_id,
            "timestamp": timestamp,
            "run_id": run_id,
            "scope_id": scope_id,
            "source": source,
        }
        if state == TaskState.TASK_STATE_SUBMITTED:
            return RunStarted(**common, status="running")
        if state == TaskState.TASK_STATE_WORKING:
            return RunProgress(**common, status="running", message=state_name)
        if state == TaskState.TASK_STATE_COMPLETED:
            return RunCompleted(**common, status="completed", output_refs=())
        if state in {TaskState.TASK_STATE_FAILED, TaskState.TASK_STATE_REJECTED}:
            message = getattr(status, "message", None)
            error_text = self._parts_text(getattr(message, "parts", None)) or state_name
            return RunFailed(
                **common,
                status="failed",
                error=ErrorInfo(
                    code=(
                        "a2a_task_rejected"
                        if state == TaskState.TASK_STATE_REJECTED
                        else "a2a_task_failed"
                    ),
                    message=error_text,
                    source="a2a",
                    scope_id=scope_id,
                ),
            )
        if state == TaskState.TASK_STATE_CANCELED:
            return RunCanceled(**common, status="canceled", reason="interrupted_active_turn")
        if state == TaskState.TASK_STATE_INPUT_REQUIRED:
            return RunInterrupted(**common, status="interrupted", reason=state_name)
        return RunProgress(**common, status="running", message=state_name)

    def artifact_to_event(
        self,
        artifact: Any,
        *,
        agent_id: str,
        user_id: str,
        session_id: str,
        invocation_id: str,
        seq_id: int,
        event_id: Optional[str] = None,
    ) -> RuntimeEvent:
        """A2A Artifact → canonical RuntimeEvent(item.started,item_kind="artifact")。"""
        artifact_id = str(getattr(artifact, "artifact_id", None) or "")
        name = getattr(artifact, "name", None) or "artifact"
        text = self._parts_text(getattr(artifact, "parts", None))
        scope_id = stable_scope_id("a2a", session_id, invocation_id)
        item_id = stable_item_id(
            "a2a", session_id, invocation_id, "artifact", artifact_id or name
        )
        source = SourceRef(
            framework="a2a",
            native_run_id=invocation_id,
            native_item_id=artifact_id or None,
            metadata={"agent_id": agent_id, "user_id": user_id, "artifact_name": name},
        )
        eid = event_id or stable_event_id(
            "a2a", scope_id, item_id, "item.started", "artifact", invocation_id, seq_id
        )
        initial = (
            ContentSnapshot(parts=(TextContent(part_id="text", text=text),))
            if text
            else None
        )
        return ItemStarted(
            schema_version=2,
            event_id=eid,
            seq=seq_id,
            timestamp=time.time(),
            run_id=invocation_id,
            scope_id=scope_id,
            source=source,
            item_id=item_id,
            item_kind="artifact",
            phase="final_answer",
            initial=initial,
        )

    def message_to_event(
        self,
        text: str,
        *,
        final: bool,
        agent_id: str,
        user_id: str,
        session_id: str,
        invocation_id: str,
        seq_id: int,
        event_id: Optional[str] = None,
    ) -> RuntimeEvent:
        """A2A Message 文本 → canonical RuntimeEvent(item.*,item_kind="message")。"""
        scope_id = stable_scope_id("a2a", session_id, invocation_id)
        item_id = stable_item_id("a2a", session_id, invocation_id, "message", "response")
        source = SourceRef(
            framework="a2a",
            native_run_id=invocation_id,
            metadata={"agent_id": agent_id, "user_id": user_id},
        )
        timestamp = time.time()
        if final:
            eid = event_id or stable_event_id(
                "a2a", scope_id, item_id, "item.completed", "snapshot", invocation_id, seq_id
            )
            return ItemCompleted(
                schema_version=2,
                event_id=eid,
                seq=seq_id,
                timestamp=timestamp,
                run_id=invocation_id,
                scope_id=scope_id,
                source=source,
                item_id=item_id,
                item_kind="message",
                snapshot=ContentSnapshot(
                    parts=(TextContent(part_id="text-0", text=text),)
                ),
            )
        eid = event_id or stable_event_id(
            "a2a", scope_id, item_id, "item.updated", "text-0", invocation_id, seq_id
        )
        return ItemUpdated(
            schema_version=2,
            event_id=eid,
            seq=seq_id,
            timestamp=timestamp,
            run_id=invocation_id,
            scope_id=scope_id,
            source=source,
            item_id=item_id,
            item_kind="message",
            op="append",
            update=TextContent(part_id="text-0", text=text),
        )

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
