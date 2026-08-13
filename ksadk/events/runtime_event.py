"""RuntimeEvent v1 schema (goal-02 / G0.2 冻结稿)。

事件只定义一次:Runtime 产生 → server 持久化 → gateway 透传 → UI/协议 adapter 消费。
本模块只负责**定义层**(类型 + 序列化/反序列化 + 事件族清单);不改 runtime.py 发事件
(那是后续阶段)。

设计约束(友商证伪,G0.2 冻结):

- **additive + ``SCHEMA_VERSION``**:只增字段/事件类型,不改既有字段语义。
- **相位字段** ``phase``:区分 ``commentary``(过程解说)vs ``final_answer``(最终答案),
  仅 text/reasoning 类事件使用。
- **工具审批一等事件**(``approval.*``),不是普通 text;审批回包走独立命令/恢复通道,
  事件流上的 ``approval.resolved`` 仅作回放/审计(非 duplex stream)。
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

#: additive 演进锚点。冻结为 1;只增不改。
SCHEMA_VERSION: Literal[1] = 1


class EventPhase(str, Enum):
    """相位:text/reasoning 类事件区分过程解说与最终答案。"""

    COMMENTARY = "commentary"
    FINAL_ANSWER = "final_answer"


# ---------------------------------------------------------------------------
# 事件族(event_type 常量,v1 冻结)。新增事件类型只能 additive 追加。
# ---------------------------------------------------------------------------


class EventType:
    """v1 事件族清单(冻结)。按族分组;每族注释标明 payload 关键字段。"""

    # text(相位:commentary/final_answer)。payload: text, message_id
    TEXT_DELTA = "text.delta"
    TEXT_COMPLETED = "text.completed"
    # reasoning(相位恒 commentary)。payload: text, summary
    REASONING_DELTA = "reasoning.delta"
    REASONING_COMPLETED = "reasoning.completed"
    # tool。begin: call_id, name, args;end: call_id, name, result, error, duration_ms
    TOOL_CALL_BEGIN = "tool.call.begin"
    TOOL_CALL_END = "tool.call.end"
    # artifact。payload: name, version, uri, mime
    ARTIFACT_CREATED = "artifact.created"
    ARTIFACT_UPDATED = "artifact.updated"
    # approval(一等)。requested: approval_id, call_id, kind, detail;
    # resolved: approval_id, call_id, decision(回放/审计)
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    # run 生命周期。payload: status;progress?: progress;failed: error;canceled: cancel_result
    RUN_STARTED = "run.started"
    RUN_PROGRESS = "run.progress"
    RUN_INTERRUPTED = "run.interrupted"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELED = "run.canceled"
    # context preprocessing. payload: phase, trigger; completed also carries cursor
    CONTEXT_COMPACTION_STARTED = "context.compaction.started"
    CONTEXT_COMPACTION_COMPLETED = "context.compaction.completed"
    # checkpoint。payload: checkpoint_id, granularity(delta|snapshot), resume_target?
    CHECKPOINT_CREATED = "checkpoint.created"
    CHECKPOINT_RESUMED = "checkpoint.resumed"
    # usage。payload: input_tokens, output_tokens, total_tokens, cached_tokens, reasoning_tokens
    USAGE_REPORTED = "usage.reported"
    # A2UI。payload: surface_id, block_id?, catalog?, data?
    A2UI_SURFACE_BEGIN = "a2ui.surface.begin"
    A2UI_SURFACE_UPDATE = "a2ui.surface.update"
    A2UI_SURFACE_END = "a2ui.surface.end"
    A2UI_INTERACTION = "a2ui.interaction"
    A2UI_ACTION = "a2ui.action"
    # remote A2A。payload: task_id, origin(remote agent url/space), status?, artifact?
    A2A_TASK_CREATED = "a2a.task.created"
    A2A_TASK_STATUS = "a2a.task.status"
    A2A_TASK_ARTIFACT = "a2a.task.artifact"


#: 全部 v1 事件类型(供校验/枚举)。
ALL_EVENT_TYPES: frozenset[str] = frozenset(
    {
        EventType.TEXT_DELTA,
        EventType.TEXT_COMPLETED,
        EventType.REASONING_DELTA,
        EventType.REASONING_COMPLETED,
        EventType.TOOL_CALL_BEGIN,
        EventType.TOOL_CALL_END,
        EventType.ARTIFACT_CREATED,
        EventType.ARTIFACT_UPDATED,
        EventType.APPROVAL_REQUESTED,
        EventType.APPROVAL_RESOLVED,
        EventType.RUN_STARTED,
        EventType.RUN_PROGRESS,
        EventType.RUN_INTERRUPTED,
        EventType.RUN_COMPLETED,
        EventType.RUN_FAILED,
        EventType.RUN_CANCELED,
        EventType.CONTEXT_COMPACTION_STARTED,
        EventType.CONTEXT_COMPACTION_COMPLETED,
        EventType.CHECKPOINT_CREATED,
        EventType.CHECKPOINT_RESUMED,
        EventType.USAGE_REPORTED,
        EventType.A2UI_SURFACE_BEGIN,
        EventType.A2UI_SURFACE_UPDATE,
        EventType.A2UI_SURFACE_END,
        EventType.A2UI_INTERACTION,
        EventType.A2UI_ACTION,
        EventType.A2A_TASK_CREATED,
        EventType.A2A_TASK_STATUS,
        EventType.A2A_TASK_ARTIFACT,
    }
)

#: 各 event_type 的 payload 必填键(conformance 用;additive —— 只允许增键)。
#: 信封字段是硬冻结;payload 必填键是 v1 最低契约,后续版本只能加可选键。
EVENT_PAYLOAD_REQUIRED_KEYS: dict[str, frozenset[str]] = {
    EventType.TEXT_DELTA: frozenset({"text"}),
    EventType.TEXT_COMPLETED: frozenset({"text"}),
    EventType.REASONING_DELTA: frozenset({"text"}),
    EventType.REASONING_COMPLETED: frozenset({"text"}),
    EventType.TOOL_CALL_BEGIN: frozenset({"call_id", "name"}),
    EventType.TOOL_CALL_END: frozenset({"call_id", "name"}),
    EventType.ARTIFACT_CREATED: frozenset({"name", "version"}),
    EventType.ARTIFACT_UPDATED: frozenset({"name", "version"}),
    EventType.APPROVAL_REQUESTED: frozenset({"approval_id", "call_id", "kind"}),
    EventType.APPROVAL_RESOLVED: frozenset({"approval_id", "call_id", "decision"}),
    EventType.RUN_STARTED: frozenset({"status"}),
    EventType.RUN_PROGRESS: frozenset({"status"}),
    EventType.RUN_INTERRUPTED: frozenset({"status"}),
    EventType.RUN_COMPLETED: frozenset({"status"}),
    EventType.RUN_FAILED: frozenset({"status", "error"}),
    EventType.RUN_CANCELED: frozenset({"status"}),
    EventType.CONTEXT_COMPACTION_STARTED: frozenset({"phase", "trigger"}),
    EventType.CONTEXT_COMPACTION_COMPLETED: frozenset(
        {"phase", "trigger", "compacted_until_seq_id"}
    ),
    EventType.CHECKPOINT_CREATED: frozenset({"checkpoint_id", "granularity"}),
    EventType.CHECKPOINT_RESUMED: frozenset({"checkpoint_id"}),
    EventType.USAGE_REPORTED: frozenset({"input_tokens", "output_tokens", "total_tokens"}),
    EventType.A2UI_SURFACE_BEGIN: frozenset({"surface_id"}),
    EventType.A2UI_SURFACE_UPDATE: frozenset({"surface_id"}),
    EventType.A2UI_SURFACE_END: frozenset({"surface_id"}),
    EventType.A2UI_INTERACTION: frozenset({"surface_id"}),
    EventType.A2UI_ACTION: frozenset({"surface_id"}),
    EventType.A2A_TASK_CREATED: frozenset({"task_id", "origin"}),
    EventType.A2A_TASK_STATUS: frozenset({"task_id", "origin", "status"}),
    EventType.A2A_TASK_ARTIFACT: frozenset({"task_id", "origin"}),
}

#: 仅 text/reasoning 类事件使用相位字段。
_PHASE_AWARE_TYPES: frozenset[str] = frozenset(
    {
        EventType.TEXT_DELTA,
        EventType.TEXT_COMPLETED,
        EventType.REASONING_DELTA,
        EventType.REASONING_COMPLETED,
    }
)


class RuntimeEvent(BaseModel):
    """RuntimeEvent v1 信封。

    字段全部硬冻结(additive 演进只允许新增可选字段)。``payload`` 按 event_type
    承载,最低必填键见 :data:`EVENT_PAYLOAD_REQUIRED_KEYS`。
    """

    schema_version: Literal[1] = SCHEMA_VERSION
    event_id: str
    event_type: str
    timestamp: float
    agent_id: str
    user_id: str
    session_id: str
    invocation_id: str
    seq_id: int
    phase: Optional[Literal["commentary", "final_answer"]] = None
    payload: dict[str, Any] = Field(default_factory=dict)

    # ---- 构造 ----

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
        payload: Optional[dict[str, Any]] = None,
        phase: Optional[str] = None,
        event_id: Optional[str] = None,
        timestamp: Optional[float] = None,
    ) -> "RuntimeEvent":
        """便捷构造:自动补 event_id / timestamp,并按 event_type 校验相位与 payload。"""
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

    # ---- 序列化 ----

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict(含全部信封字段 + payload)。"""
        return self.model_dump(mode="json", exclude_none=True)

    def to_json(self) -> str:
        """序列化为 JSON 字符串。"""
        return self.model_dump_json(exclude_none=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeEvent":
        """从 dict 反序列化。与 :meth:`create` 一致过 conformance:
        未知 event_type / 相位滥用 / 缺必填键抛 ``ValueError``,不得混入系统。"""
        event = cls.model_validate(data)
        event.validate_conformance()
        return event

    @classmethod
    def from_json(cls, raw: str) -> "RuntimeEvent":
        """从 JSON 字符串反序列化(同 :meth:`from_dict` 过 conformance)。"""
        event = cls.model_validate_json(raw)
        event.validate_conformance()
        return event

    # ---- conformance ----

    def validate_conformance(self) -> None:
        """按 v1 契约校验:事件类型已知、相位仅用于 text/reasoning、payload 必填键齐全。

        additive 演进:允许 payload 含额外键(不作 strict 拒绝),只校验最低必填键。
        未知 event_type / 缺必填键 / 相位滥用抛 :class:`ValueError`。
        """
        if self.event_type not in ALL_EVENT_TYPES:
            raise ValueError(f"unknown event_type: {self.event_type!r}(v1 事件族之外)")
        if self.phase is not None and self.event_type not in _PHASE_AWARE_TYPES:
            raise ValueError(
                f"phase 仅用于 text/reasoning 事件,{self.event_type!r} 不应带 phase={self.phase!r}"
            )
        required = EVENT_PAYLOAD_REQUIRED_KEYS.get(self.event_type, frozenset())
        missing = required - set(self.payload.keys())
        if missing:
            raise ValueError(f"event_type {self.event_type!r} payload 缺必填键: {sorted(missing)}")


__all__ = [
    "ALL_EVENT_TYPES",
    "EVENT_PAYLOAD_REQUIRED_KEYS",
    "EventPhase",
    "EventType",
    "RuntimeEvent",
    "SCHEMA_VERSION",
]
