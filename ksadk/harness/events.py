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
#: v2 信封版本（长任务方案 §8）：run_id / scope_id / parent_scope_id。
V2_SCHEMA_VERSION: Literal[2] = 2


def project_v2(events: list["RuntimeEvent"]) -> list["RuntimeEvent"]:
    """整流事件流为 v2（长任务方案 §8 平台输出边界）。

    Harness 内部可保留 v1 兼容事件；向平台（server/gateway/Studio）输出前
    经本函数统一升级。子 Agent 事件以 ``agent.started`` 中的父信息标记
    parent_scope_id（事件树已有 child → parent 映射时直接透传）。
    """
    parent_by_agent: dict[str, str] = {}
    for event in events:
        explicit_parent = str(event.payload.get("parent_agent_id") or "")
        if explicit_parent:
            parent_by_agent[event.agent_id] = explicit_parent
    return [
        event.to_v2(
            parent_scope_id=(
                f"agent:{parent_by_agent[event.agent_id]}"
                if event.agent_id in parent_by_agent
                else None
            )
        )
        for event in events
    ]


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
    # --- Harness v1 增量事件（plan §13.2，additive 扩展） ---
    # run 恢复。payload: target, resume_kind
    RUN_RESUMED = "run.resumed"
    # turn 生命周期。payload: turn_id, turn_number
    TURN_STARTED = "turn.started"
    TURN_COMPLETED = "turn.completed"
    # context 计划。payload: budget_tokens, sections
    CONTEXT_PLANNED = "context.planned"
    CONTEXT_RECOVERED = "context.recovered"
    # context 构建完成（长任务方案 §8）：manifest 引用 + Section Token 构成。
    # payload: manifest_id, planned_tokens, projected_tokens, sections
    CONTEXT_BUILT = "context.built"
    # 稳定 Prompt 前缀缓存诊断（仅 Hash）。payload: stable_prompt_hash,
    # previous_stable_prompt_hash, cache_break, reason
    PROMPT_CACHE_DIAGNOSTIC = "prompt.cache.diagnostic"
    # model 调用。payload: model, attempt; completed 增补 usage
    MODEL_CALL_STARTED = "model.call.started"
    MODEL_CALL_COMPLETED = "model.call.completed"
    MODEL_CALL_FAILED = "model.call.failed"
    # policy 决策。payload: action, reason, policy_ref, risk_level
    POLICY_DECISION = "policy.decision"
    # memory。payload: scope, memory_ref; conflict 增补 conflicting_ref
    MEMORY_READ = "memory.read"
    MEMORY_WRITE = "memory.write"
    MEMORY_CONFLICT = "memory.conflict"
    # 召回注入（长任务方案 §8）：Memory ID、分数、是否进入模型输入。
    # payload: scope, query; items 增补 memory_id/score/injected
    MEMORY_RECALLED = "memory.recalled"
    # capability 声明/降级/恢复。
    # declared payload: capability_ref, kind, required, load_policy, state=unknown
    # degraded/recovered payload: capability_ref, state
    CAPABILITY_DECLARED = "capability.declared"
    CAPABILITY_DEGRADED = "capability.degraded"
    CAPABILITY_RECOVERED = "capability.recovered"
    # --- 事件树（收口 5）：Run → Agent → Turn → Node → Model/Tool/Usage ---
    # agent 生命周期。payload: agent_id; completed 增补 status
    AGENT_STARTED = "agent.started"
    AGENT_COMPLETED = "agent.completed"
    # 图节点执行区间。payload: node; completed 增补 duration_ms?
    NODE_STARTED = "node.started"
    NODE_COMPLETED = "node.completed"
    # Skill 渐进披露。payload: skill_ref, level, content_hash, size_bytes
    SKILL_DISCLOSED = "skill.disclosed"
    # MCP 渐进披露。payload: server_id, level, content_hash, size_bytes,
    # tool_name（L2/L3）；L1 = tools/list，L2 = 读 Schema，L3 = 调用。
    MCP_DISCLOSED = "mcp.disclosed"


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
        EventType.RUN_RESUMED,
        EventType.TURN_STARTED,
        EventType.TURN_COMPLETED,
        EventType.CONTEXT_PLANNED,
        EventType.CONTEXT_RECOVERED,
        EventType.CONTEXT_BUILT,
        EventType.PROMPT_CACHE_DIAGNOSTIC,
        EventType.MODEL_CALL_STARTED,
        EventType.MODEL_CALL_COMPLETED,
        EventType.MODEL_CALL_FAILED,
        EventType.POLICY_DECISION,
        EventType.MEMORY_READ,
        EventType.MEMORY_WRITE,
        EventType.MEMORY_CONFLICT,
        EventType.MEMORY_RECALLED,
        EventType.CAPABILITY_DECLARED,
        EventType.CAPABILITY_DEGRADED,
        EventType.CAPABILITY_RECOVERED,
        EventType.AGENT_STARTED,
        EventType.AGENT_COMPLETED,
        EventType.NODE_STARTED,
        EventType.NODE_COMPLETED,
        EventType.SKILL_DISCLOSED,
        EventType.MCP_DISCLOSED,
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
    # --- Harness v1 增量事件 payload 必填键（plan §13.2） ---
    EventType.RUN_RESUMED: frozenset({"target", "resume_kind"}),
    EventType.TURN_STARTED: frozenset({"turn_id", "turn_number"}),
    EventType.TURN_COMPLETED: frozenset({"turn_id", "turn_number"}),
    EventType.CONTEXT_PLANNED: frozenset({"budget_tokens"}),
    EventType.CONTEXT_RECOVERED: frozenset({"reason"}),
    EventType.CONTEXT_BUILT: frozenset(
        {"manifest_id", "planned_tokens", "projected_tokens", "sections"}
    ),
    EventType.PROMPT_CACHE_DIAGNOSTIC: frozenset(
        {"stable_prompt_hash", "cache_break", "reason"}
    ),
    EventType.MODEL_CALL_STARTED: frozenset({"model"}),
    EventType.MODEL_CALL_COMPLETED: frozenset({"model"}),
    EventType.MODEL_CALL_FAILED: frozenset({"model", "error"}),
    EventType.POLICY_DECISION: frozenset({"action", "reason"}),
    EventType.MEMORY_READ: frozenset({"scope"}),
    EventType.MEMORY_WRITE: frozenset({"scope"}),
    EventType.MEMORY_CONFLICT: frozenset({"scope", "conflicting_ref"}),
    EventType.MEMORY_RECALLED: frozenset({"scope", "query"}),
    EventType.CAPABILITY_DECLARED: frozenset({"capability_ref", "kind", "state"}),
    EventType.CAPABILITY_DEGRADED: frozenset({"capability_ref"}),
    EventType.CAPABILITY_RECOVERED: frozenset({"capability_ref"}),
    # --- 事件树（收口 5）---
    EventType.AGENT_STARTED: frozenset({"agent_id"}),
    EventType.AGENT_COMPLETED: frozenset({"agent_id", "status"}),
    EventType.NODE_STARTED: frozenset({"node"}),
    EventType.NODE_COMPLETED: frozenset({"node"}),
    EventType.SKILL_DISCLOSED: frozenset({"skill_ref", "level", "content_hash", "size_bytes"}),
    EventType.MCP_DISCLOSED: frozenset({"server_id", "level", "content_hash", "size_bytes"}),
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
    """RuntimeEvent 事件信封（v1 / v2）。

    - **v1**（``schema_version=1``）：字段硬冻结（additive 只允许新增可选字段）；
    - **v2**（``schema_version=2``，长任务方案 §8）：信封新增
      ``run_id`` / ``scope_id`` / ``parent_scope_id``，使 Studio 能展示单 Agent
      与多 Agent 的 Context、Memory 和 Token 层级。v2 必须 ``run_id`` +
      ``scope_id`` 齐全（conformance 强制）。

    v1 事件可经 :meth:`to_v2` 无损升级（``run_id`` 取 ``invocation_id``，
    ``scope_id`` 取 ``agent_id``）；反序列化同时接受 v1/v2。
    ``payload`` 按 event_type 承载,最低必填键见
    :data:`EVENT_PAYLOAD_REQUIRED_KEYS`。
    """

    schema_version: Literal[1, 2] = SCHEMA_VERSION
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
    # ---- v2 信封增量（长任务方案 §8）----
    #: 本事件所属 Run（v1 中由 invocation_id 承载；v2 显式命名）。
    run_id: Optional[str] = None
    #: 作用域（单 Agent：agent:<id>；子 Agent 带 parent_scope_id）。
    scope_id: Optional[str] = None
    #: 父作用域（子 Agent 事件的父 Agent scope；顶层为空）。
    parent_scope_id: Optional[str] = None
    #: 父 Run（多 Agent 子 Run 使用；顶层为空）。
    parent_run_id: Optional[str] = None

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
        # v2 信封（长任务方案 §8）：传入 run_id/scope_id 即构造 v2 事件。
        run_id: Optional[str] = None,
        scope_id: Optional[str] = None,
        parent_scope_id: Optional[str] = None,
        parent_run_id: Optional[str] = None,
    ) -> "RuntimeEvent":
        """便捷构造:自动补 event_id / timestamp,并按 event_type 校验相位与 payload。

        传入 ``run_id``/``scope_id`` 构造 v2 信封（schema_version=2）。
        """
        schema_version: int = 1
        if (
            run_id is not None
            or scope_id is not None
            or parent_scope_id is not None
            or parent_run_id is not None
        ):
            schema_version = 2
        event = cls(
            schema_version=schema_version,  # type: ignore[arg-type]
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
            run_id=run_id,
            scope_id=scope_id,
            parent_scope_id=parent_scope_id,
            parent_run_id=parent_run_id,
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

    # ---- v2 升级 ----

    def to_v2(self, *, parent_scope_id: Optional[str] = None) -> "RuntimeEvent":
        """无损升级为 v2 信封（长任务方案 §8）。

        ``run_id`` 取 ``invocation_id``，``scope_id`` 取 ``agent:<agent_id>``。
        已是 v2 且未传 ``parent_scope_id`` 时原样返回。
        """
        if self.schema_version == 2 and parent_scope_id is None:
            return self
        return self.model_copy(
            update={
                "schema_version": 2,
                "run_id": self.run_id or self.invocation_id,
                "scope_id": self.scope_id or f"agent:{self.agent_id}",
                "parent_scope_id": self.parent_scope_id or parent_scope_id,
            }
        )

    # ---- conformance ----

    def validate_conformance(self) -> None:
        """按契约校验:事件类型已知、相位仅用于 text/reasoning、payload 必填键齐全。

        additive 演进:允许 payload 含额外键(不作 strict 拒绝),只校验最低必填键。
        v2 信封额外要求 ``run_id`` 与 ``scope_id`` 齐全（长任务方案 §8）。
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
        if self.schema_version == 2 and (not self.run_id or not self.scope_id):
            raise ValueError("v2 信封必须携带 run_id 与 scope_id（长任务方案 §8）")


__all__ = [
    "ALL_EVENT_TYPES",
    "EVENT_PAYLOAD_REQUIRED_KEYS",
    "EventPhase",
    "EventType",
    "RuntimeEvent",
    "SCHEMA_VERSION",
    "V2_SCHEMA_VERSION",
    "project_v2",
]
