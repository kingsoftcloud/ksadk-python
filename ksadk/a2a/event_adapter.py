"""A2AEventAdapter — A2A Message/Task/Artifact 与 RuntimeEvent 的双向转换 (goal-05)。

契约 §3.2:``A2AEventAdapter`` 负责 A2A wire 对象与 G0.2 RuntimeEvent 的双向转换;
§7.2:artifact/message ↔ RuntimeEvent artifact/text/data。

方向:
- ``task_status_to_event``:A2A TaskStatus/TaskState → RuntimeEvent(run.*)。
- ``artifact_to_event``:A2A Artifact → RuntimeEvent(artifact.*)。
- ``message_to_event``:A2A Message → RuntimeEvent(text.*)。
- ``event_to_text_part``:RuntimeEvent(text.*)→ A2A ``Part``(用于出站)。

wire 对象是 protobuf(``a2a_pb2``);文本用 ``Part(text=...)``。
"""

from __future__ import annotations

from typing import Any, Optional

from a2a.types import Part, TaskState, TaskStatus

from ksadk.events.runtime_event import EventType, RuntimeEvent

#: A2A TaskState → RuntimeEvent run.* 事件类型映射。
_TASK_STATE_TO_RUN_EVENT = {
    TaskState.TASK_STATE_SUBMITTED: EventType.RUN_STARTED,
    TaskState.TASK_STATE_WORKING: EventType.RUN_PROGRESS,
    TaskState.TASK_STATE_COMPLETED: EventType.RUN_COMPLETED,
    TaskState.TASK_STATE_FAILED: EventType.RUN_FAILED,
    TaskState.TASK_STATE_CANCELED: EventType.RUN_CANCELED,
    TaskState.TASK_STATE_INPUT_REQUIRED: EventType.RUN_INTERRUPTED,
    TaskState.TASK_STATE_REJECTED: EventType.RUN_FAILED,
}


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
        """A2A TaskStatus → RuntimeEvent(run.*)。"""
        event_type = _TASK_STATE_TO_RUN_EVENT.get(status.state, EventType.RUN_PROGRESS)
        state_name = TaskState.Name(status.state) if status.state is not None else "unknown"
        payload: dict[str, Any] = {"status": state_name}
        if event_type == EventType.RUN_CANCELED:
            payload["cancel_result"] = "interrupted_active_turn"
        elif event_type == EventType.RUN_FAILED:
            message = getattr(status, "message", None)
            payload["error"] = self._parts_text(getattr(message, "parts", None)) or state_name
        return RuntimeEvent.create(
            event_type,
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            invocation_id=invocation_id,
            seq_id=seq_id,
            payload=payload,
            event_id=event_id,
        )

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
        """A2A Artifact → RuntimeEvent(artifact.*)。"""
        name = getattr(artifact, "name", None) or "artifact"
        text = self._parts_text(getattr(artifact, "parts", None))
        return RuntimeEvent.create(
            EventType.ARTIFACT_CREATED,
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            invocation_id=invocation_id,
            seq_id=seq_id,
            payload={
                "artifact_id": str(getattr(artifact, "artifact_id", None) or ""),
                "name": name,
                "version": 1,
                "text": text,
            },
            event_id=event_id,
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
        """A2A Message 文本 → RuntimeEvent(text.*,带相位)。"""
        return RuntimeEvent.create(
            EventType.TEXT_COMPLETED if final else EventType.TEXT_DELTA,
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            invocation_id=invocation_id,
            seq_id=seq_id,
            phase="final_answer" if final else "commentary",
            payload={"text": text},
            event_id=event_id,
        )

    # ---- RuntimeEvent → A2A ----

    @staticmethod
    def event_to_text_part(event: RuntimeEvent) -> Optional[Part]:
        """RuntimeEvent(text.*)→ A2A ``Part``(用于出站 message/artifact)。"""
        event.validate_conformance()
        if event.event_type not in (EventType.TEXT_DELTA, EventType.TEXT_COMPLETED):
            return None
        text = str(event.payload.get("text") or "")
        return Part(text=text) if text else None

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
