"""收口 6：多 Agent（子 Agent 即工具）+ 事件树子 Agent 展示。"""

from __future__ import annotations

import asyncio

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from ksadk.harness.conformance import run_conformance_suite
from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
from ksadk.harness.event_tree import build_event_tree
from ksadk.harness.reasoner import HarnessReasoningTurn, HarnessToolCall
from ksadk.harness.spec import (
    CapabilityBinding,
    CapabilityBindings,
    ExecutionStrategySpec,
    HarnessSpec,
    ModelBinding,
    PromptSpec,
    SubAgentBinding,
)
from ksadk.harness.subagent import SubAgentSpec, child_spec
from ksadk.runtime import StartRequest


class _Reasoner:
    """父：先调子 Agent 工具再收尾；子：直接给结论（usage 可审计）。"""

    def __init__(self) -> None:
        self.turns = 0

    async def complete(self, **kwargs):
        self.turns += 1
        if self.turns == 1:
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(
                        call_id="tc-sub",
                        name="researcher",
                        arguments={"task": "查预算"},
                    ),
                ),
                usage={"input_tokens": 40, "output_tokens": 10},
            )
        if self.turns == 2:
            # 子 Agent 的推理（无工具，直接结论）。
            return HarnessReasoningTurn(
                final_text="预算是 42000", usage={"input_tokens": 15, "output_tokens": 8}
            )
        return HarnessReasoningTurn(
            final_text="综合结论：预算 42000", usage={"input_tokens": 25, "output_tokens": 6}
        )


def _drive() -> list:
    engine = ManagedLangGraphEngine(
        reasoner=_Reasoner(),
        checkpointer=InMemorySaver(),
        sub_agents={
            "researcher": SubAgentSpec(
                name="researcher", instructions="你是研究员，完成子任务并给出结论。"
            )
        },
    )
    spec = HarnessSpec(
        agent_revision_ref="agent-revision://proj-1@1",
        model=ModelBinding(profile_ref="model-profile://m@1.0.0"),
        prompt=PromptSpec(instructions="主 Agent"),
    )

    async def run() -> list:
        compiled = await engine.compile(spec)
        handle = await engine.start(
            StartRequest(
                input="查预算并总结",
                user_id="u",
                session_id="s",
                agent_id="main",
                runtime_type="managed-langgraph",
                metadata={"invocation_id": "run-sub"},
            ),
            compiled,
        )
        return [e async for e in engine.stream(handle)]

    return asyncio.run(run())


def test_subagent_runs_inline_and_streams_child_events():
    events = _drive()
    kinds = [e.event_type for e in events]
    assert "run.completed" in kinds and kinds.count("run.started") == 1
    # 子 Agent 区间事件存在且 agent_id 是子 Agent。
    child_agent_events = [
        e
        for e in events
        if e.event_type == "agent.started" and e.payload.get("agent_id") == "main:researcher"
    ]
    assert child_agent_events, "子 Agent 必须有自己的 agent.started"
    # 子 Agent 结论作为工具结果回流父对话。
    tool_end = [
        e
        for e in events
        if e.event_type == "tool.call.end" and e.payload.get("call_id") == "tc-sub"
    ]
    assert tool_end and "42000" in str(tool_end[0].payload.get("result"))


def test_event_tree_shows_subagent_as_separate_agent():
    events = _drive()
    tree = build_event_tree(events)
    agents = {a["agent_id"]: a for a in tree["agents"]}
    assert "main" in agents and "main:researcher" in agents
    assert agents["main:researcher"]["status"] == "completed"
    assert agents["main:researcher"]["usage"]["input_tokens"] == 15
    # 父 Agent 两次模型调用（发起点 + 收尾），不含子 Agent 的调用。
    assert agents["main"]["usage"]["input_tokens"] == 40 + 25
    # Run 级 usage = 父 + 子。
    assert tree["usage"]["input_tokens"] == 40 + 15 + 25


def test_mixed_agent_stream_still_conformant():
    """子 Agent 事件并入父流后，整体仍通过 Conformance 套件。"""
    events = _drive()
    report = run_conformance_suite(events, cancel_requested=False)
    assert report.ok, [(v.rule, v.detail) for v in report.violations]


def test_child_agent_inherits_model_fallback_chain():
    parent = HarnessSpec(
        agent_revision_ref="agent-revision://proj-1@1",
        model=ModelBinding(
            profile_ref="model-profile://primary@1.0.0",
            fallback_profile_refs=("model-profile://backup@1.0.0",),
        ),
        prompt=PromptSpec(instructions="主 Agent"),
    )
    child = child_spec(parent, SubAgentSpec(name="researcher", instructions="研究"))
    assert child.model == parent.model


def test_child_capability_inheritance_is_least_privilege():
    parent = HarnessSpec(
        agent_revision_ref="agent-revision://proj-1@1",
        model=ModelBinding(profile_ref="model-profile://primary@1.0.0"),
        prompt=PromptSpec(instructions="主 Agent"),
        capabilities=CapabilityBindings(
            skill_bindings=(CapabilityBinding(capability_ref="skill://guide@1"),),
            mcp_bindings=(CapabilityBinding(capability_ref="mcp://finance@1"),),
        ),
    )
    default_child = child_spec(parent, SubAgentSpec(name="researcher", instructions="研究"))
    assert default_child.capabilities.skill_bindings == parent.capabilities.skill_bindings
    assert default_child.capabilities.mcp_bindings == ()

    privileged_child = child_spec(
        parent,
        SubAgentSpec(name="operator", instructions="操作", inherit_mcp=True),
    )
    assert privileged_child.capabilities.mcp_bindings == parent.capabilities.mcp_bindings


class _TimeoutReasoner:
    def __init__(self) -> None:
        self.parent_calls = 0

    async def complete(self, **kwargs):
        if kwargs.get("prompt") == "慢速子任务":
            await asyncio.sleep(0.1)
            return HarnessReasoningTurn(final_text="太晚了")
        self.parent_calls += 1
        if self.parent_calls == 1:
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(
                        call_id="tc-timeout", name="slow-worker", arguments={"task": "慢任务"}
                    ),
                )
            )
        return HarnessReasoningTurn(final_text="父 Agent 已处理子任务失败")


def _drive_declared_subagent(*, failure_policy: str) -> list:
    engine = ManagedLangGraphEngine(reasoner=_TimeoutReasoner(), checkpointer=InMemorySaver())
    spec = HarnessSpec(
        agent_revision_ref="agent-revision://proj-1@1",
        model=ModelBinding(profile_ref="model-profile://m@1.0.0"),
        prompt=PromptSpec(instructions="主 Agent"),
        sub_agents=(
            SubAgentBinding(
                name="slow-worker",
                instructions="慢速子任务",
                timeout_seconds=0.01,
                failure_policy=failure_policy,
            ),
        ),
    )

    async def run() -> list:
        compiled = await engine.compile(spec)
        handle = await engine.start(
            StartRequest(
                input="委派",
                user_id="u",
                session_id="s",
                agent_id="main",
                runtime_type="managed-langgraph",
                metadata={"invocation_id": f"run-timeout-{failure_policy}"},
            ),
            compiled,
        )
        return [event async for event in engine.stream(handle)]

    return asyncio.run(run())


def test_revision_declared_subagent_timeout_propagates_as_tool_error():
    events = _drive_declared_subagent(failure_policy="propagate")
    ended = [event for event in events if event.event_type == "tool.call.end"]
    assert ended and "timed out" in str(ended[0].payload.get("error"))
    assert events[-1].event_type == "run.completed"


def test_optional_subagent_timeout_returns_structured_result():
    events = _drive_declared_subagent(failure_policy="return_error")
    ended = [event for event in events if event.event_type == "tool.call.end"]
    assert ended and "sub-agent error" in str(ended[0].payload.get("result"))
    assert "error" not in ended[0].payload


class _ParallelSubAgentReasoner:
    """父 Agent 同轮委派两个子 Agent；子 Agent 用屏障证明真实并发。"""

    def __init__(self) -> None:
        self.parent_calls = 0
        self.child_entered = 0
        self.children_ready = asyncio.Event()

    async def complete(self, **kwargs):
        prompt = str(kwargs.get("prompt") or "")
        if prompt.startswith("子任务"):
            self.child_entered += 1
            if self.child_entered == 2:
                self.children_ready.set()
            await asyncio.wait_for(self.children_ready.wait(), timeout=0.2)
            return HarnessReasoningTurn(final_text=f"{prompt}完成")
        self.parent_calls += 1
        if self.parent_calls == 1:
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(
                        call_id="call-a", name="worker-a", arguments={"task": "A"}
                    ),
                    HarnessToolCall(
                        call_id="call-b", name="worker-b", arguments={"task": "B"}
                    ),
                )
            )
        return HarnessReasoningTurn(final_text="并行委派完成")


def test_revision_subagents_run_in_parallel_and_events_keep_call_order():
    reasoner = _ParallelSubAgentReasoner()
    engine = ManagedLangGraphEngine(reasoner=reasoner, checkpointer=InMemorySaver())
    spec = HarnessSpec(
        agent_revision_ref="agent-revision://parallel@1",
        model=ModelBinding(profile_ref="model-profile://m@1.0.0"),
        prompt=PromptSpec(instructions="主 Agent"),
        sub_agents=(
            SubAgentBinding(name="worker-a", instructions="子任务 A"),
            SubAgentBinding(name="worker-b", instructions="子任务 B"),
        ),
        execution_strategy=ExecutionStrategySpec(config={"max_parallel_subagents": 2}),
    )

    async def run() -> list:
        compiled = await engine.compile(spec)
        handle = await engine.start(
            StartRequest(
                input="并行执行",
                user_id="u",
                session_id="s",
                agent_id="main",
                runtime_type="managed-langgraph",
                metadata={"invocation_id": "run-parallel-subagents"},
            ),
            compiled,
        )
        return [event async for event in engine.stream(handle)]

    events = asyncio.run(run())
    ends = [
        event
        for event in events
        if event.event_type == "tool.call.end"
        and event.payload.get("call_id") in {"call-a", "call-b"}
    ]
    assert [event.payload["call_id"] for event in ends] == ["call-a", "call-b"]
    assert all(event.payload.get("parallel") is True for event in ends)
    child_starts = [
        event.payload.get("agent_id")
        for event in events
        if event.event_type == "agent.started" and ":worker-" in str(event.payload)
    ]
    assert child_starts == ["main:worker-a", "main:worker-b"]


@pytest.mark.parametrize("value", [0, 33, "4", True])
def test_invalid_parallel_subagent_budget_is_rejected(value):
    engine = ManagedLangGraphEngine(reasoner=_Reasoner(), checkpointer=InMemorySaver())
    spec = HarnessSpec(
        agent_revision_ref="agent-revision://parallel@1",
        model=ModelBinding(profile_ref="model-profile://m@1.0.0"),
        prompt=PromptSpec(instructions="主 Agent"),
        execution_strategy=ExecutionStrategySpec(config={"max_parallel_subagents": value}),
    )

    with pytest.raises(Exception, match="max_parallel_subagents"):
        asyncio.run(engine.compile(spec))
