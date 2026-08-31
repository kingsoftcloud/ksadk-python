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

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ksadk.harness.events import RuntimeEvent
from ksadk.harness.spec import CapabilityBindings, HarnessSpec, PromptSpec, SubAgentBinding
from ksadk.runtime import StartRequest


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
    failure_policy: str = "propagate"
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


def child_spec(parent_spec: HarnessSpec, sub: SubAgentSpec) -> HarnessSpec:
    """由父 Spec 派生子 Spec（同模型绑定，独立指令）。"""
    # Skill 是只读知识能力，默认继承；MCP 可能触达外部系统，只有 Revision
    # 明确授权时才继承，防止委派扩大权限面。
    return HarnessSpec(
        agent_revision_ref=parent_spec.agent_revision_ref,
        model=parent_spec.model,
        prompt=PromptSpec(instructions=sub.instructions),
        capabilities=CapabilityBindings(
            skill_bindings=(
                parent_spec.capabilities.skill_bindings if sub.inherit_skills else ()
            ),
            mcp_bindings=(parent_spec.capabilities.mcp_bindings if sub.inherit_mcp else ()),
        ),
        context_policy=parent_spec.context_policy,
        memory_policy=parent_spec.memory_policy,
        approval_policy=parent_spec.approval_policy,
        sandbox_policy=parent_spec.sandbox_policy,
        observability_policy=parent_spec.observability_policy,
    )


async def run_subagent(
    *,
    engine: Any,
    parent_run: Any,
    sub: SubAgentSpec,
    task: str,
    call_id: str = "",
) -> tuple[str, list[RuntimeEvent]]:
    """运行子 Agent 到完成，返回 (最终文本, 以父 seq 重排的子事件)。

    子事件信封：invocation_id 同父 Run（单一审计流），agent_id 为
    ``{父agent_id}:{子名}``（事件树按 agent 分组展示子 Agent）。
    """
    from ksadk.harness.engine.langgraph import ManagedLangGraphEngine

    parent_spec = getattr(engine, "_current_spec", None)
    if parent_spec is None:
        raise RuntimeError("子 Agent 需要父引擎先 compile（无当前 Spec）")
    child_engine = ManagedLangGraphEngine(
        reasoner=engine._reasoner,
        # 复用持久化后端、使用独立 thread_id；因此可独立恢复但不会污染父图。
        checkpointer=engine._checkpointer,
        tools={name: engine._tools[name] for name in sub.tools if name in engine._tools},
        skill_runtime=getattr(engine, "_skill_runtime", None),
        mcp_runtime=(getattr(engine, "_mcp_runtime", None) if sub.inherit_mcp else None),
        artifact_store=getattr(engine._mcp_disclosure, "_artifact_store", None),
        max_reasoning_turns=sub.max_turns,
    )
    compiled = await child_engine.compile(child_spec(parent_spec, sub))
    child_agent_id = f"{parent_run.state.agent_id}:{sub.name}"
    handle = await child_engine.start(
        StartRequest(
            input=task,
            user_id=parent_run.state.user_id,
            # 事件信封沿用父 session，保持单一 invocation 的 Conformance；
            # Checkpoint 另用私有 namespace，避免同名子 Agent 并发覆盖。
            session_id=parent_run.state.session_id,
            agent_id=child_agent_id,
            runtime_type="managed-langgraph",
            metadata={
                "invocation_id": parent_run.handle.run_id,
                "checkpoint_session_id": (
                    f"{parent_run.state.session_id}:sub:{sub.name}:{call_id or 'delegation'}"
                ),
            },
        ),
        compiled,
    )
    final_text = ""
    collected: list[RuntimeEvent] = []
    failure = ""
    completed = False
    try:
        async with asyncio.timeout(sub.timeout_seconds):
            async for event in child_engine.stream(handle):
                if event.event_type == "run.failed":
                    failure = str(event.payload.get("error") or "child run failed")
                # 子 Run 生命周期不并入父流；agent 区间表达子任务边界。
                if event.event_type.startswith("run."):
                    continue
                collected.append(event)
                if event.event_type == "agent.completed":
                    completed = str(event.payload.get("status") or "") == "completed"
                if event.event_type == "text.completed" and event.phase == "final_answer":
                    final_text = str(event.payload.get("text") or "")
    except TimeoutError:
        failure = f"sub-agent {sub.name!r} timed out after {sub.timeout_seconds:g}s"
    # Engine 会把 CancelledError 归一化为取消状态；因此 asyncio.timeout 的
    # 取消可能被子引擎消费。缺失 agent.completed 仍必须按超时失败处理。
    if not completed and not failure:
        failure = f"sub-agent {sub.name!r} timed out after {sub.timeout_seconds:g}s"
    if failure:
        if sub.failure_policy == "return_error":
            return f"[sub-agent error] {failure}", collected
        raise RuntimeError(failure)
    return final_text or "（子 Agent 未产出结果）", collected


def resequence_child_events(parent_run: Any, events: list[RuntimeEvent]) -> None:
    """把缓冲的子事件按父 seq 重排后并入父流（tool.call 事件之后统一落盘）。"""
    for ev in events:
        parent_run.seq += 1
        parent_run.events.append(
            RuntimeEvent.create(
                ev.event_type,
                agent_id=ev.agent_id,
                user_id=ev.user_id,
                session_id=ev.session_id,
                invocation_id=ev.invocation_id,
                seq_id=parent_run.seq,
                payload=dict(ev.payload),
                phase=ev.phase,
            )
        )


__all__ = ["SubAgentSpec", "child_spec", "resequence_child_events", "run_subagent"]
