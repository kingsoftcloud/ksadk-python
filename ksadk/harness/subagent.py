"""子 Agent 执行（收口 6 / plan §14）：多 Agent 作为可选能力。

Engine 支持 ``sub_agents``：工具名 → 子 Agent 配置（instructions + 可选
tools）。父 Agent 的模型像调用普通工具一样发起子任务；引擎在 tool_calls
节点内联运行一个子 ManagedLangGraphEngine 到完成，子事件（含
agent.started/completed、自己的 turn/node/model/usage）以**子 agent_id**
写入父事件流——事件树（event_tree）据此把子 Agent 展示为独立树节点。

约束：
- 子 Agent 不再嵌套子 Agent（单层，避免失控递归）；
- 子 Agent 共享父 Reasoner 与工具注册表（数据面由宿主装配）；
- 每次委派使用独立 Checkpoint namespace、轮数预算和墙钟超时；
- Skill 默认继承，MCP 必须显式授权后才继承；
- 子 Agent 结果以其最终文本作为工具结果回流父对话。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from jsonschema import ValidationError, validate
from pydantic import BaseModel, ConfigDict, Field

from ksadk.harness.events import RuntimeEvent
from ksadk.harness.spec import (
    CapabilityBindings,
    ExecutionStrategySpec,
    HarnessSpec,
    PromptSpec,
    SubAgentBinding,
)


@dataclass
class SubAgentSpec:
    """子 Agent 配置（宿主声明，引擎内联执行）。"""

    name: str
    instructions: str
    #: 允许子 Agent 使用的工具名（空 = 不用工具，只做文本推理）。
    tools: tuple[str, ...] = ()
    description: str = ""
    timeout_seconds: float = 120.0
    max_turns: int = 4
    max_total_tokens: int | None = None
    max_artifacts: int | None = None
    max_tool_calls: int | None = None
    output_schema: dict[str, Any] | None = None
    depends_on: tuple[str, ...] = ()
    failure_policy: str = "propagate"
    max_retries: int = 0
    inherit_skills: bool = True
    inherit_mcp: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_binding(cls, binding: SubAgentBinding) -> "SubAgentSpec":
        """把不可变 Revision 声明投影为本次 Run 的执行配置。"""
        return cls(**binding.model_dump())

    @property
    def openai_schema(self) -> dict[str, Any]:
        """模型可见的工具 schema（子 Agent 即工具，任务描述入参）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description or f"委托子 Agent {self.name} 完成任务并返回其结论",
                "parameters": {
                    "type": "object",
                    "properties": {"task": {"type": "string", "description": "要完成的子任务描述"}},
                    "required": ["task"],
                },
            },
        }


class SubAgentResult(BaseModel):
    """父 Agent 可消费、平台可审计的稳定子任务结果。"""

    model_config = ConfigDict(extra="allow")

    status: str
    output: Any = None
    artifact_refs: tuple[str, ...] = ()
    usage: dict[str, int] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    error_category: str | None = None
    error: str | None = None


class SubAgentOutputValidationError(ValueError):
    """子任务最终输出不满足 Revision 声明的 Schema。"""


def validate_subagent_output(text: str, schema: dict[str, Any] | None) -> Any:
    """按声明解析并校验最终输出；无 Schema 时保持原文本。"""
    text = text.strip()
    if not text:
        raise SubAgentOutputValidationError("sub-agent did not produce a final result")
    if re.search(r"DSML", text.replace("｜", "|"), re.IGNORECASE):
        raise SubAgentOutputValidationError(
            "sub-agent returned an unexecuted tool call instead of a result"
        )
    if schema is None:
        return text
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SubAgentOutputValidationError(
            f"sub-agent output_schema requires JSON output: {exc.msg}"
        ) from exc
    try:
        validate(instance=value, schema=schema)
    except ValidationError as exc:
        raise SubAgentOutputValidationError(
            f"sub-agent output_schema validation failed: {exc.message}"
        ) from exc
    return value


def order_subagent_tool_calls(
    calls: list[dict[str, Any]], specs: dict[str, "SubAgentSpec"]
) -> list[dict[str, Any]]:
    """对同批子任务稳定拓扑排序；普通工具与无依赖子任务保持原顺序。"""
    positions = {str(call.get("name") or ""): index for index, call in enumerate(calls)}
    call_names = set(positions)
    dependencies = {
        name: {dependency for dependency in spec.depends_on if dependency in call_names}
        for name, spec in specs.items()
        if name in call_names
    }

    def rank(name: str, trail: frozenset[str] = frozenset()) -> int:
        if name in trail:
            raise SubAgentExecutionError("dependency_cycle", f"sub-agent dependency cycle: {name}")
        parents = dependencies.get(name, set())
        if not parents:
            return 0
        return 1 + max(rank(parent, trail | {name}) for parent in parents)

    return sorted(
        calls,
        key=lambda call: (
            rank(str(call.get("name") or "")),
            positions[str(call.get("name") or "")],
        ),
    )


def child_spec(parent_spec: HarnessSpec, sub: SubAgentSpec) -> HarnessSpec:
    """由父 Spec 派生子 Spec（同模型绑定，独立指令）。"""
    # Skill 是只读知识能力，默认继承；MCP 可能触达外部系统，只有 Revision
    # 明确授权时才继承，防止委派扩大权限面。
    return HarnessSpec(
        agent_revision_ref=parent_spec.agent_revision_ref,
        model=parent_spec.model,
        prompt=PromptSpec(instructions=sub.instructions),
        capabilities=CapabilityBindings(
            skill_bindings=(parent_spec.capabilities.skill_bindings if sub.inherit_skills else ()),
            mcp_bindings=(parent_spec.capabilities.mcp_bindings if sub.inherit_mcp else ()),
        ),
        context_policy=parent_spec.context_policy,
        memory_policy=parent_spec.memory_policy,
        approval_policy=parent_spec.approval_policy,
        sandbox_policy=parent_spec.sandbox_policy,
        observability_policy=parent_spec.observability_policy,
        execution_strategy=ExecutionStrategySpec(
            config={
                "max_tool_calls": sub.max_tool_calls,
                "max_artifacts": sub.max_artifacts,
                "max_total_tokens": sub.max_total_tokens,
            }
        ),
    )


async def run_subagent(
    *,
    engine: Any,
    parent_run: Any,
    sub: SubAgentSpec,
    task: str,
    call_id: str = "",
) -> tuple[dict[str, Any], list[RuntimeEvent]]:
    """运行子 Agent 到完成，返回 (最终文本, 以父 seq 重排的子事件)。

    子事件信封：invocation_id 同父 Run（单一审计流），agent_id 为
    ``{父agent_id}:{子名}``（事件树按 agent 分组展示子 Agent）。
    """
    from ksadk.harness.engine.subagents import execute_subagent

    return await execute_subagent(
        engine=engine, parent_run=parent_run, sub=sub, task=task, call_id=call_id
    )


class SubAgentExecutionError(RuntimeError):
    """Classified child failure safe to project into Tool events."""

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


def resequence_child_events(
    parent_run: Any, events: list[RuntimeEvent], *, observe: bool = False
) -> None:
    """把缓冲的子事件按父 seq 重排后并入父流（tool.call 事件之后统一落盘）。"""
    for ev in events:
        parent_run.seq += 1
        payload = dict(ev.payload)
        if ev.event_type in {"agent.started", "agent.completed"}:
            payload.setdefault("parent_agent_id", parent_run.state.agent_id)
        parent_run.events.append(
            RuntimeEvent.create(
                ev.event_type,
                agent_id=ev.agent_id,
                user_id=ev.user_id,
                session_id=ev.session_id,
                # v1 invocation_id 保持父审计流兼容；v2 run_id 保留独立子 Run。
                invocation_id=parent_run.handle.run_id,
                seq_id=parent_run.seq,
                payload=payload,
                phase=ev.phase,
                run_id=ev.run_id or ev.invocation_id,
                scope_id=ev.scope_id or f"agent:{ev.agent_id}",
                parent_scope_id=ev.parent_scope_id or f"agent:{parent_run.state.agent_id}",
                parent_run_id=ev.parent_run_id or parent_run.handle.run_id,
            )
        )

        if observe:
            projected = parent_run.events[-1]
            if parent_run.controller is not None:
                parent_run.control_events.append(projected)
                parent_run.controller.observe(projected)
            parent_run.observed_event_ids.add(projected.event_id)


__all__ = [
    "SubAgentOutputValidationError",
    "SubAgentResult",
    "SubAgentSpec",
    "child_spec",
    "order_subagent_tool_calls",
    "resequence_child_events",
    "run_subagent",
    "validate_subagent_output",
]
