"""子 Agent 执行（收口 6 / plan §14）：多 Agent 作为可选能力。

Engine 支持 ``sub_agents``：工具名 → 子 Agent 配置（instructions + 可选
tools）。父 Agent 的模型像调用普通工具一样发起子任务；引擎在 tool_calls
节点内联运行一个子 ManagedLangGraphEngine 到完成，子事件（含
agent.started/completed、自己的 turn/node/model/usage）以**子 agent_id**
写入父事件流——事件树（event_tree）据此把子 Agent 展示为独立树节点。

约束（一期）：
- 子 Agent 不再嵌套子 Agent（单层，避免失控递归）；
- 子 Agent 共享父 Reasoner 与工具注册表（数据面由宿主装配）；
- 子 Agent 结果以其最终文本作为工具结果回流父对话。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ksadk.harness.events import RuntimeEvent
from ksadk.harness.spec import CapabilityBindings, HarnessSpec, ModelBinding, PromptSpec
from ksadk.runtime import StartRequest


@dataclass
class SubAgentSpec:
    """子 Agent 配置（宿主声明，引擎内联执行）。"""

    name: str
    instructions: str
    #: 允许子 Agent 使用的工具名（空 = 不用工具，只做文本推理）。
    tools: tuple[str, ...] = ()
    description: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

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
    # Skill 是知识型能力，随父 Spec 的 skill_bindings 一起继承给子 Agent；
    # SkillRuntime 由父引擎透传（skill_composition 装配一次，全家共用）。
    return HarnessSpec(
        agent_revision_ref=parent_spec.agent_revision_ref,
        model=ModelBinding(profile_ref=parent_spec.model.profile_ref),
        prompt=PromptSpec(instructions=sub.instructions),
        capabilities=CapabilityBindings(
            skill_bindings=parent_spec.capabilities.skill_bindings
        ),
    )


async def run_subagent(
    *,
    engine: Any,
    parent_run: Any,
    sub: SubAgentSpec,
    task: str,
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
        checkpointer=None,
        tools={name: engine._tools[name] for name in sub.tools if name in engine._tools},
        skill_runtime=getattr(engine, "_skill_runtime", None),
    )
    compiled = await child_engine.compile(child_spec(parent_spec, sub))
    child_agent_id = f"{parent_run.state.agent_id}:{sub.name}"
    handle = await child_engine.start(
        StartRequest(
            input=task,
            user_id=parent_run.state.user_id,
            session_id=parent_run.state.session_id,
            agent_id=child_agent_id,
            runtime_type="managed-langgraph",
            metadata={"invocation_id": parent_run.handle.run_id},
        ),
        compiled,
    )
    final_text = ""
    collected: list[RuntimeEvent] = []
    async for event in child_engine.stream(handle):
        # 子 Run 的生命周期事件（run.*）不并入父流——合并流是单一 invocation，
        # Conformance 要求恰好一个 run 生命周期；子 Agent 的边界由
        # agent.started/completed 区间表达。seq 重排由引擎在 tool_calls 节点
        # 统一完成。
        if event.event_type.startswith("run."):
            continue
        collected.append(event)
        if event.event_type == "text.completed" and event.phase == "final_answer":
            final_text = str(event.payload.get("text") or "")
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
