"""收口 6：多 Agent（子 Agent 即工具）+ 事件树子 Agent 展示。"""

from __future__ import annotations

import asyncio

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from ksadk.harness.capability_runtime import CapabilityRuntime
from ksadk.harness.conformance import run_conformance_suite
from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
from ksadk.harness.event_tree import build_event_tree
from ksadk.harness.events import project_v2
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
from ksadk.harness.subagent import (
    SubAgentOutputValidationError,
    SubAgentResult,
    SubAgentSpec,
    child_spec,
    order_subagent_tool_calls,
    validate_subagent_output,
)
from ksadk.harness.tool_receipts import ToolReceiptStore
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
    result = tool_end[0].payload["result"]
    assert result["status"] == "completed"
    assert result["output"] == "预算是 42000"
    assert result["usage"] == {"input_tokens": 15, "output_tokens": 8, "total_tokens": 23}
    assert result["evidence"]["child_agent_id"] == "main:researcher"


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


def test_v2_projection_preserves_child_parent_tool_usage_and_terminal_links():
    projected = project_v2(_drive())
    child = [event for event in projected if event.agent_id == "main:researcher"]
    assert child
    parent = [event for event in projected if event.agent_id == "main"]
    assert all(event.run_id == "run-sub" for event in parent)
    assert all(event.run_id != "run-sub" for event in child)
    assert len({event.run_id for event in child}) == 1
    assert all(event.parent_run_id == "run-sub" for event in child)
    assert all(event.scope_id for event in projected)
    assert all(event.parent_scope_id == "agent:main" for event in child)
    call_ids = {
        event.payload["call_id"]
        for event in projected
        if event.event_type in {"tool.call.begin", "tool.call.end"}
    }
    assert call_ids == {"tc-sub"}
    assert any(event.event_type == "usage.reported" for event in child)
    assert projected[-1].event_type == "run.completed"


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
    assert ended and ended[0].payload["result"]["status"] == "failed"
    assert ended[0].payload["result"]["error_category"] == "timeout"
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


def test_subagent_token_budget_stops_child_and_returns_classified_tool_error():
    engine = ManagedLangGraphEngine(reasoner=_Reasoner(), checkpointer=InMemorySaver())
    spec = HarnessSpec(
        agent_revision_ref="agent-revision://budgeted-child@1",
        model=ModelBinding(profile_ref="model-profile://m@1.0.0"),
        prompt=PromptSpec(instructions="主 Agent"),
        sub_agents=(
            SubAgentBinding(
                name="researcher",
                instructions="研究",
                max_total_tokens=10,
            ),
        ),
    )

    async def run() -> list:
        compiled = await engine.compile(spec)
        handle = await engine.start(
            StartRequest(
                input="委派研究",
                user_id="u",
                session_id="budget-session",
                agent_id="main",
                runtime_type="managed-langgraph",
                metadata={"invocation_id": "run-budget-child"},
            ),
            compiled,
        )
        return [event async for event in engine.stream(handle)]

    events = asyncio.run(run())
    tool_end = next(
        event
        for event in events
        if event.event_type == "tool.call.end"
        and event.payload.get("call_id") == "tc-sub"
    )
    assert tool_end.payload["error_category"] == "budget_exhausted"
    assert "10" in tool_end.payload["error"]


def test_positive_token_budget_is_checked_before_child_model_call():
    child_calls = 0

    class _BudgetReasoner:
        async def complete(self, **kwargs):
            nonlocal child_calls
            if kwargs.get("prompt") == "child instructions consume context":
                child_calls += 1
                return HarnessReasoningTurn(final_text="must not run")
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(
                        call_id="budget-child", name="worker", arguments={"task": "large task"}
                    ),
                )
            )

    async def run() -> list:
        engine = ManagedLangGraphEngine(
            reasoner=_BudgetReasoner(), checkpointer=InMemorySaver()
        )
        spec = HarnessSpec(
            agent_revision_ref="agent-revision://token-preflight@1",
            model=ModelBinding(profile_ref="model-profile://m@1.0.0"),
            prompt=PromptSpec(instructions="main"),
            sub_agents=(
                SubAgentBinding(
                    name="worker",
                    instructions="child instructions consume context",
                    max_total_tokens=1,
                ),
            ),
        )
        compiled = await engine.compile(spec)
        handle = await engine.start(
            StartRequest(input="delegate", user_id="u", session_id="s", agent_id="main"),
            compiled,
        )
        return await _collect(engine, handle)

    events = asyncio.run(run())
    assert child_calls == 0
    ended = next(
        event
        for event in events
        if event.event_type == "tool.call.end"
        and event.payload.get("call_id") == "budget-child"
    )
    assert ended.payload["error_category"] == "budget_exhausted"


def test_artifact_budget_rejects_producing_tool_before_execution():
    tool_calls = 0

    class _ArtifactTool:
        artifact_budget_cost = 1

        async def __call__(self, _arguments):
            nonlocal tool_calls
            tool_calls += 1
            return "created"

        def drain_artifacts(self):
            return [{"name": "result", "version": 1, "uri": "artifact://x/result@v1"}]

    class _ArtifactReasoner:
        def __init__(self) -> None:
            self.parent_calls = 0

        async def complete(self, **kwargs):
            if kwargs.get("prompt") == "child":
                return HarnessReasoningTurn(
                    tool_calls=(
                        HarnessToolCall(call_id="make", name="make_artifact", arguments={}),
                    )
                )
            self.parent_calls += 1
            if self.parent_calls == 1:
                return HarnessReasoningTurn(
                    tool_calls=(
                        HarnessToolCall(call_id="delegate", name="worker", arguments={"task": "x"}),
                    )
                )
            return HarnessReasoningTurn(final_text="done")

    async def run() -> list:
        engine = ManagedLangGraphEngine(
            reasoner=_ArtifactReasoner(),
            checkpointer=InMemorySaver(),
            tools={"make_artifact": _ArtifactTool()},
        )
        spec = HarnessSpec(
            agent_revision_ref="agent-revision://artifact-budget@1",
            model=ModelBinding(profile_ref="model-profile://m@1.0.0"),
            prompt=PromptSpec(instructions="main"),
            sub_agents=(
                SubAgentBinding(
                    name="worker",
                    instructions="child",
                    tools=("make_artifact",),
                    max_artifacts=0,
                ),
            ),
        )
        compiled = await engine.compile(spec)
        handle = await engine.start(
            StartRequest(input="go", user_id="u", session_id="s", agent_id="main"),
            compiled,
        )
        return await _collect(engine, handle)

    events = asyncio.run(run())
    assert tool_calls == 0
    delegate_end = next(
        event
        for event in events
        if event.event_type == "tool.call.end" and event.payload.get("call_id") == "delegate"
    )
    assert delegate_end.payload["error_category"] == "budget_exhausted"


def test_subagent_tool_budget_stops_child_after_limit():
    class _ToolBudgetReasoner:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return HarnessReasoningTurn(
                    tool_calls=(
                        HarnessToolCall(
                            call_id="delegate", name="worker", arguments={"task": "work"}
                        ),
                    )
                )
            if self.calls == 2:
                return HarnessReasoningTurn(
                    tool_calls=(
                        HarnessToolCall(call_id="child-tool", name="noop", arguments={}),
                    )
                )
            return HarnessReasoningTurn(final_text="done")

    async def noop(_arguments):
        return "ok"

    async def run() -> list:
        engine = ManagedLangGraphEngine(
            reasoner=_ToolBudgetReasoner(),
            checkpointer=InMemorySaver(),
            tools={"noop": noop},
        )
        spec = HarnessSpec(
            agent_revision_ref="agent-revision://tool-budget@1",
            model=ModelBinding(profile_ref="model-profile://m@1.0.0"),
            prompt=PromptSpec(instructions="main"),
            sub_agents=(
                SubAgentBinding(
                    name="worker",
                    instructions="child",
                    tools=("noop",),
                    max_tool_calls=0,
                ),
            ),
        )
        compiled = await engine.compile(spec)
        handle = await engine.start(
            StartRequest(
                input="delegate", user_id="u", session_id="s", agent_id="main"
            ),
            compiled,
        )
        return await _collect(engine, handle)

    events = asyncio.run(run())
    ended = next(
        event
        for event in events
        if event.event_type == "tool.call.end" and event.payload.get("call_id") == "delegate"
    )
    assert ended.payload["error_category"] == "budget_exhausted"
    assert "tool-call budget 0" in ended.payload["error"]


def test_subagent_result_contract_is_additive_and_serializable():
    result = SubAgentResult(
        status="completed",
        output={"answer": 42},
        artifact_refs=("artifact://run/result@v1",),
        usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        evidence={"child_run_id": "child-1"},
    )
    payload = result.model_dump(mode="json")
    assert payload["output"] == {"answer": 42}
    assert payload["artifact_refs"] == ["artifact://run/result@v1"]


def test_declared_subagent_output_schema_accepts_json_and_rejects_invalid_output():
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    assert validate_subagent_output('{"answer": 42}', schema) == {"answer": 42}

    with pytest.raises(SubAgentOutputValidationError, match="output_schema"):
        validate_subagent_output('{"answer": "forty-two"}', schema)


def test_subagent_binding_rejects_invalid_output_schema_definition():
    with pytest.raises(Exception, match="output_schema"):
        SubAgentBinding(
            name="worker",
            instructions="work",
            output_schema={"type": "not-a-json-schema-type"},
        )


def test_subagent_dependency_graph_orders_calls_topologically():
    calls = [
        {"call_id": "review", "name": "reviewer", "arguments": {"task": "review"}},
        {"call_id": "draft", "name": "writer", "arguments": {"task": "draft"}},
    ]
    specs = {
        "writer": SubAgentSpec(name="writer", instructions="write"),
        "reviewer": SubAgentSpec(
            name="reviewer", instructions="review", depends_on=("writer",)
        ),
    }
    ordered = order_subagent_tool_calls(calls, specs)
    assert [call["name"] for call in ordered] == ["writer", "reviewer"]


def test_subagent_dependency_graph_rejects_unknown_and_cyclic_dependencies():
    common = {
        "agent_revision_ref": "agent-revision://dependencies@1",
        "model": ModelBinding(profile_ref="model-profile://m@1.0.0"),
        "prompt": PromptSpec(instructions="main"),
    }
    with pytest.raises(Exception, match="unknown"):
        HarnessSpec(
            **common,
            sub_agents=(
                SubAgentBinding(
                    name="reviewer", instructions="review", depends_on=("writer",)
                ),
            ),
        )
    with pytest.raises(Exception, match="cycle"):
        HarnessSpec(
            **common,
            sub_agents=(
                SubAgentBinding(name="a", instructions="a", depends_on=("b",)),
                SubAgentBinding(name="b", instructions="b", depends_on=("a",)),
            ),
        )


def test_parent_cancel_propagates_to_active_child_run():
    child_started = asyncio.Event()

    class _BlockingChildReasoner:
        def __init__(self) -> None:
            self.parent_calls = 0

        async def complete(self, **kwargs):
            if kwargs.get("prompt") == "blocking child":
                child_started.set()
                await asyncio.Event().wait()
            self.parent_calls += 1
            if self.parent_calls == 1:
                return HarnessReasoningTurn(
                    tool_calls=(
                        HarnessToolCall(
                            call_id="blocking-call",
                            name="worker",
                            arguments={"task": "wait"},
                        ),
                    )
                )
            return HarnessReasoningTurn(final_text="done")

    async def run() -> list:
        engine = ManagedLangGraphEngine(
            reasoner=_BlockingChildReasoner(), checkpointer=InMemorySaver()
        )
        spec = HarnessSpec(
            agent_revision_ref="agent-revision://cancel-child@1",
            model=ModelBinding(profile_ref="model-profile://m@1.0.0"),
            prompt=PromptSpec(instructions="main"),
            sub_agents=(
                SubAgentBinding(name="worker", instructions="blocking child"),
            ),
        )
        compiled = await engine.compile(spec)
        handle = await engine.start(
            StartRequest(
                input="delegate",
                user_id="u",
                session_id="cancel-child",
                agent_id="main",
                runtime_type="managed-langgraph",
            ),
            compiled,
        )
        stream_task = asyncio.create_task(_collect(engine, handle))
        await asyncio.wait_for(child_started.wait(), timeout=1)
        await engine.cancel(handle)
        events = await asyncio.wait_for(stream_task, timeout=1)
        assert not engine._active_subagent_runs
        return events

    events = asyncio.run(run())
    assert events[-1].event_type == "run.canceled"


async def _collect(engine, handle):
    return [event async for event in engine.stream(handle)]


def test_subagent_structured_result_commits_tool_receipt():
    receipts = ToolReceiptStore(":memory:")
    engine = ManagedLangGraphEngine(
        reasoner=_Reasoner(),
        checkpointer=InMemorySaver(),
        capability_runtime=CapabilityRuntime(receipts=receipts),
    )
    spec = HarnessSpec(
        agent_revision_ref="agent-revision://receipted-child@1",
        model=ModelBinding(profile_ref="model-profile://m@1.0.0"),
        prompt=PromptSpec(instructions="main"),
        sub_agents=(SubAgentBinding(name="researcher", instructions="research"),),
    )

    async def run() -> list:
        compiled = await engine.compile(spec)
        handle = await engine.start(
            StartRequest(
                input="delegate",
                user_id="u",
                session_id="s",
                agent_id="main",
                metadata={"invocation_id": "run-receipted-child"},
            ),
            compiled,
        )
        return await _collect(engine, handle)

    events = asyncio.run(run())
    receipt = receipts.get("run-receipted-child", "tc-sub")
    assert receipt is not None and receipt.status == "executed"
    assert '"status": "completed"' in receipt.result_digest
    result = next(
        event.payload["result"]
        for event in events
        if event.event_type == "tool.call.end" and event.payload.get("call_id") == "tc-sub"
    )
    assert result["evidence"]["child_run_id"]


def test_invalid_structured_child_output_never_enters_parent_context():
    engine = ManagedLangGraphEngine(reasoner=_Reasoner(), checkpointer=InMemorySaver())
    spec = HarnessSpec(
        agent_revision_ref="agent-revision://schema-child@1",
        model=ModelBinding(profile_ref="model-profile://m@1.0.0"),
        prompt=PromptSpec(instructions="main"),
        sub_agents=(
            SubAgentBinding(
                name="researcher",
                instructions="research",
                output_schema={
                    "type": "object",
                    "properties": {"answer": {"type": "integer"}},
                    "required": ["answer"],
                },
            ),
        ),
    )

    async def run() -> list:
        compiled = await engine.compile(spec)
        handle = await engine.start(
            StartRequest(input="delegate", user_id="u", session_id="s", agent_id="main"),
            compiled,
        )
        return await _collect(engine, handle)

    events = asyncio.run(run())
    ended = next(
        event
        for event in events
        if event.event_type == "tool.call.end" and event.payload.get("call_id") == "tc-sub"
    )
    assert ended.payload["error_category"] == "output_validation_failed"
    assert "result" not in ended.payload


def test_cross_process_resume_replays_completed_child_receipt(tmp_path):
    import contextlib

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from ksadk.harness.capabilities import RiskLevel
    from ksadk.harness.capability_runtime import ToolProfile
    from ksadk.runtime import ResumePayload, ResumeTarget

    checkpoint_path = str(tmp_path / "subagent-checkpoints.db")
    receipt_path = str(tmp_path / "subagent-receipts.db")
    child_executions: list[str] = []
    side_effects: list[str] = []

    class _PhaseOneReasoner:
        async def complete(self, **kwargs):
            if kwargs.get("prompt") == "child instructions":
                child_executions.append("child")
                return HarnessReasoningTurn(final_text="child result")
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(
                        call_id="child-call", name="worker", arguments={"task": "work"}
                    ),
                    HarnessToolCall(
                        call_id="approval-call", name="publish", arguments={"value": 1}
                    ),
                )
            )

    class _PhaseTwoReasoner:
        async def complete(self, **kwargs):
            return HarnessReasoningTurn(final_text="resumed")

    async def publish(_arguments):
        side_effects.append("publish")
        return "published"

    spec = HarnessSpec(
        agent_revision_ref="agent-revision://recover-children@1",
        model=ModelBinding(profile_ref="model-profile://m@1.0.0"),
        prompt=PromptSpec(instructions="main"),
        sub_agents=(
            SubAgentBinding(name="worker", instructions="child instructions"),
        ),
    )

    def runtime():
        return CapabilityRuntime(
            receipts=ToolReceiptStore(receipt_path),
            profiles={
                "publish": ToolProfile(name="publish", risk_level=RiskLevel.HIGH),
            },
        )

    async def phase_one():
        cm = AsyncSqliteSaver.from_conn_string(checkpoint_path)
        saver = await cm.__aenter__()
        try:
            engine = ManagedLangGraphEngine(
                reasoner=_PhaseOneReasoner(),
                checkpointer=saver,
                tools={"publish": publish},
                capability_runtime=runtime(),
            )
            compiled = await engine.compile(spec)
            handle = await engine.start(
                StartRequest(input="go", user_id="u", session_id="s", agent_id="main"),
                compiled,
            )
            events = await _collect(engine, handle)
            return handle, events
        finally:
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)

    async def phase_two(handle):
        cm = AsyncSqliteSaver.from_conn_string(checkpoint_path)
        saver = await cm.__aenter__()
        try:
            engine = ManagedLangGraphEngine(
                reasoner=_PhaseTwoReasoner(),
                checkpointer=saver,
                tools={"publish": publish},
                capability_runtime=runtime(),
            )
            compiled = await engine.compile(spec)
            attached = await engine.attach(handle, compiled)
            await engine.resume(
                attached,
                ResumeTarget(kind="thread_id", id=handle.native_ref["thread_id"]),
                ResumePayload(
                    kind="approval_decision", call_id="approval-call", data="approved"
                ),
            )
            return await _collect(engine, attached)
        finally:
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)

    handle, first = asyncio.run(phase_one())
    assert any(event.event_type == "run.interrupted" for event in first)
    assert child_executions == ["child"]

    second = asyncio.run(phase_two(handle))
    assert child_executions == ["child"], "completed child must replay Receipt, not rerun"
    assert side_effects == ["publish"]
    assert second[-1].event_type == "run.completed"


def test_cross_process_recovers_unfinished_child_checkpoint_without_restarting(tmp_path):
    import contextlib

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    checkpoint_path = str(tmp_path / "unfinished-child.db")
    tool_entered: asyncio.Event | None = None
    side_effects: list[str] = []
    sub = SubAgentSpec(name="worker", instructions="child instructions", tools=("effect",))
    parent_spec = HarnessSpec(
        agent_revision_ref="agent-revision://unfinished-child@1",
        model=ModelBinding(profile_ref="model-profile://m@1.0.0"),
        prompt=PromptSpec(instructions="main instructions"),
        sub_agents=(
            SubAgentBinding(
                name="worker", instructions=sub.instructions, tools=sub.tools
            ),
        ),
    )

    class _PhaseOneChildReasoner:
        async def complete(self, **kwargs):
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(call_id="effect-call", name="effect", arguments={}),
                )
            )

    async def phase_one_effect(_arguments):
        assert tool_entered is not None
        tool_entered.set()
        await asyncio.Event().wait()

    async def phase_one():
        nonlocal tool_entered
        tool_entered = asyncio.Event()
        cm = AsyncSqliteSaver.from_conn_string(checkpoint_path)
        saver = await cm.__aenter__()
        try:
            child_engine = ManagedLangGraphEngine(
                reasoner=_PhaseOneChildReasoner(),
                checkpointer=saver,
                tools={"effect": phase_one_effect},
            )
            compiled = await child_engine.compile(child_spec(parent_spec, sub))
            handle = await child_engine.start(
                StartRequest(
                    input="work",
                    user_id="u",
                    session_id="s",
                    agent_id="main:worker",
                    metadata={
                        "invocation_id": "parent-run:sub:worker:child-call",
                        "checkpoint_session_id": "s:sub:worker:child-call",
                    },
                ),
                compiled,
            )
            streaming = asyncio.create_task(_collect(child_engine, handle))
            await asyncio.wait_for(tool_entered.wait(), timeout=2)
            await child_engine.pause(handle)
            await streaming
        finally:
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)

    class _PhaseTwoReasoner:
        def __init__(self) -> None:
            self.parent_calls = 0

        async def complete(self, **kwargs):
            if kwargs.get("prompt") == "child instructions":
                return HarnessReasoningTurn(final_text="recovered child")
            self.parent_calls += 1
            if self.parent_calls == 1:
                return HarnessReasoningTurn(
                    tool_calls=(
                        HarnessToolCall(
                            call_id="child-call", name="worker", arguments={"task": "work"}
                        ),
                    )
                )
            return HarnessReasoningTurn(final_text="parent done")

    async def phase_two_effect(_arguments):
        side_effects.append("effect")
        return "ok"

    async def phase_two():
        cm = AsyncSqliteSaver.from_conn_string(checkpoint_path)
        saver = await cm.__aenter__()
        try:
            engine = ManagedLangGraphEngine(
                reasoner=_PhaseTwoReasoner(),
                checkpointer=saver,
                tools={"effect": phase_two_effect},
            )
            compiled = await engine.compile(parent_spec)
            handle = await engine.start(
                StartRequest(
                    input="delegate",
                    user_id="u",
                    session_id="s",
                    agent_id="main",
                    metadata={"invocation_id": "parent-run"},
                ),
                compiled,
            )
            return await _collect(engine, handle)
        finally:
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)

    asyncio.run(phase_one())
    events = asyncio.run(phase_two())
    assert side_effects == ["effect"], [
        (event.event_type, event.payload)
        for event in events
        if event.event_type in {"tool.call.end", "run.failed", "run.interrupted"}
    ]
    delegated = next(
        event
        for event in events
        if event.event_type == "tool.call.end"
        and event.payload.get("call_id") == "child-call"
    )
    assert delegated.payload["result"]["evidence"]["recovered_from_checkpoint"] is True
    assert events[-1].event_type == "run.completed"


def test_retry_policy_retries_retryable_child_failure_once():
    child_attempts = 0

    class _RetryReasoner:
        def __init__(self) -> None:
            self.parent_calls = 0

        async def complete(self, **kwargs):
            nonlocal child_attempts
            if kwargs.get("prompt") == "retry child":
                child_attempts += 1
                if child_attempts == 1:
                    raise RuntimeError("transient child failure")
                return HarnessReasoningTurn(final_text="recovered")
            self.parent_calls += 1
            if self.parent_calls == 1:
                return HarnessReasoningTurn(
                    tool_calls=(
                        HarnessToolCall(
                            call_id="retry-child", name="worker", arguments={"task": "work"}
                        ),
                    )
                )
            return HarnessReasoningTurn(final_text="done")

    async def run() -> list:
        engine = ManagedLangGraphEngine(
            reasoner=_RetryReasoner(), checkpointer=InMemorySaver()
        )
        spec = HarnessSpec(
            agent_revision_ref="agent-revision://retry-child@1",
            model=ModelBinding(profile_ref="model-profile://m@1.0.0"),
            prompt=PromptSpec(instructions="main"),
            sub_agents=(
                SubAgentBinding(
                    name="worker",
                    instructions="retry child",
                    failure_policy="retry",
                    max_retries=1,
                ),
            ),
        )
        compiled = await engine.compile(spec)
        handle = await engine.start(
            StartRequest(input="go", user_id="u", session_id="s", agent_id="main"),
            compiled,
        )
        return await _collect(engine, handle)

    events = asyncio.run(run())
    assert child_attempts == 2
    result = next(
        event.payload["result"]
        for event in events
        if event.event_type == "tool.call.end" and event.payload.get("call_id") == "retry-child"
    )
    assert result["status"] == "completed"


def test_fail_fast_cancels_unstarted_sibling_after_child_failure():
    started: list[str] = []

    class _FailFastReasoner:
        def __init__(self) -> None:
            self.parent_calls = 0

        async def complete(self, **kwargs):
            prompt = kwargs.get("prompt")
            if prompt in {"first child", "second child"}:
                started.append(str(prompt))
                if prompt == "first child":
                    raise RuntimeError("first failed")
                return HarnessReasoningTurn(final_text="second completed")
            self.parent_calls += 1
            if self.parent_calls == 1:
                return HarnessReasoningTurn(
                    tool_calls=(
                        HarnessToolCall(
                            call_id="first", name="first", arguments={"task": "one"}
                        ),
                        HarnessToolCall(
                            call_id="second", name="second", arguments={"task": "two"}
                        ),
                    )
                )
            return HarnessReasoningTurn(final_text="done")

    async def run() -> list:
        engine = ManagedLangGraphEngine(
            reasoner=_FailFastReasoner(), checkpointer=InMemorySaver()
        )
        spec = HarnessSpec(
            agent_revision_ref="agent-revision://fail-fast@1",
            model=ModelBinding(profile_ref="model-profile://m@1.0.0"),
            prompt=PromptSpec(instructions="main"),
            sub_agents=(
                SubAgentBinding(name="first", instructions="first child"),
                SubAgentBinding(name="second", instructions="second child"),
            ),
            execution_strategy=ExecutionStrategySpec(
                config={"subagent_failure_mode": "fail_fast"}
            ),
        )
        compiled = await engine.compile(spec)
        handle = await engine.start(
            StartRequest(input="go", user_id="u", session_id="s", agent_id="main"),
            compiled,
        )
        return await _collect(engine, handle)

    events = asyncio.run(run())
    assert started == ["first child"]
    second_end = next(
        event
        for event in events
        if event.event_type == "tool.call.end" and event.payload.get("call_id") == "second"
    )
    assert second_end.payload["error_category"] == "cancelled_by_fail_fast"
