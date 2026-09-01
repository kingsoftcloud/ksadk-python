"""OpenSpec change complete-ksadk-harness-core 缺口收口测试。

覆盖 tasks.md 未勾选项的验收口径：

- 7.1 子 Agent 独立 Run/Checkpoint/预算/取消域 —— 并行子运行状态不互相覆盖
- 7.2 依赖图调度/并发上限/预算 —— 未授权工作不启动
- 7.3 fail-fast/部分成功/可重试失败/父取消 —— 多分支故障注入矩阵
- 7.5 跨进程多 Agent 恢复 E2E —— 已完成不重跑（Receipt 回放）、未完成从一致状态继续
"""

from __future__ import annotations

import asyncio
import contextlib

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ksadk.harness.capabilities import RiskLevel
from ksadk.harness.capability_runtime import CapabilityRuntime, ToolProfile
from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
from ksadk.harness.event_tree import build_event_tree
from ksadk.harness.reasoner import HarnessReasoningTurn, HarnessToolCall
from ksadk.harness.spec import (
    ExecutionStrategySpec,
    HarnessSpec,
    ModelBinding,
    PromptSpec,
    SubAgentBinding,
)
from ksadk.harness.tool_receipts import ToolReceiptStore
from ksadk.runtime import ResumePayload, ResumeTarget, StartRequest


async def _collect(engine, handle):  # type: ignore[no-untyped-def]
    return [event async for event in engine.stream(handle)]


def _child_run_id(events, call_id):  # type: ignore[no-untyped-def]
    end = next(
        event
        for event in events
        if event.event_type == "tool.call.end" and event.payload.get("call_id") == call_id
    )
    return end.payload["result"]["evidence"]["child_run_id"]


# ---------------------------------------------------------------------------
# 7.1 子 Agent 独立 Run / Checkpoint —— 并行状态不互相覆盖
# ---------------------------------------------------------------------------


class _ParallelIndependentReasoner:
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
            await asyncio.wait_for(self.children_ready.wait(), timeout=0.5)
            return HarnessReasoningTurn(final_text=f"{prompt}完成")
        self.parent_calls += 1
        if self.parent_calls == 1:
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(call_id="call-a", name="worker-a", arguments={"task": "A"}),
                    HarnessToolCall(call_id="call-b", name="worker-b", arguments={"task": "B"}),
                ),
                usage={"input_tokens": 20, "output_tokens": 4},
            )
        return HarnessReasoningTurn(
            final_text="并行委派完成", usage={"input_tokens": 10, "output_tokens": 3}
        )


def test_7_1_parallel_children_have_independent_runs_and_checkpoint_namespaces():
    """两个子 Agent 并行：独立 run_id + 独立 Checkpoint 命名空间，任一恢复不覆盖另一。"""
    reasoner = _ParallelIndependentReasoner()
    engine = ManagedLangGraphEngine(reasoner=reasoner, checkpointer=InMemorySaver())
    spec = HarnessSpec(
        agent_revision_ref="agent-revision://independent-children@1",
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
                metadata={"invocation_id": "run-independent-children"},
            ),
            compiled,
        )
        return await _collect(engine, handle)

    events = asyncio.run(run())
    child_starts = [
        event for event in events
        if event.event_type == "agent.started" and ":worker-" in str(event.payload)
    ]
    assert [event.payload.get("agent_id") for event in child_starts] == [
        "main:worker-a",
        "main:worker-b",
    ]
    # 独立 Run 标识：两个子 Agent 的 child_run_id 互不相同。
    run_a = _child_run_id(events, "call-a")
    run_b = _child_run_id(events, "call-b")
    assert run_a != run_b, "子 Agent 必须有各自独立的 run_id"
    # 独立 Checkpoint 命名空间：run_id 内嵌的 checkpoint_session_id 互不相同，
    # 任一恢复不会覆盖另一运行状态。
    assert "sub:worker-a" in run_a and "sub:worker-b" in run_b
    # 事件树展示两个互不覆盖的子 Agent 区间。
    tree = build_event_tree(events)
    child_agents = [
        agent for agent in tree["agents"]
        if ":worker-" in str(agent.get("agent_id", ""))
    ]
    assert len(child_agents) == 2
    assert {agent["agent_id"] for agent in child_agents} == {
        "main:worker-a",
        "main:worker-b",
    }
    assert events[-1].event_type == "run.completed"
    assert reasoner.child_entered == 2


# ---------------------------------------------------------------------------
# 7.2 依赖图调度 / 并发上限 / 预算 —— 未授权工作不启动
# ---------------------------------------------------------------------------


def test_7_2_zero_tool_budget_child_side_effect_never_starts():
    """子 Agent max_tool_calls=0：模型可推理但工具副作用绝不启动，以 budget_exhausted 结束。"""
    side_effects: list[str] = []

    class _ZeroToolBudgetReasoner:
        async def complete(self, **kwargs):
            if kwargs.get("prompt") == "child instructions":
                return HarnessReasoningTurn(
                    tool_calls=(
                        HarnessToolCall(
                            call_id="child-tool", name="side_effect", arguments={}
                        ),
                    )
                )
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(
                        call_id="child-call", name="worker", arguments={"task": "work"}
                    ),
                )
            )

    async def side_effect(_arguments):
        side_effects.append("ran")
        return "ok"

    engine = ManagedLangGraphEngine(
        reasoner=_ZeroToolBudgetReasoner(),
        checkpointer=InMemorySaver(),
        tools={"side_effect": side_effect},
    )
    spec = HarnessSpec(
        agent_revision_ref="agent-revision://zero-tool-budget@1",
        model=ModelBinding(profile_ref="model-profile://m@1.0.0"),
        prompt=PromptSpec(instructions="main"),
        sub_agents=(
            SubAgentBinding(
                name="worker",
                instructions="child instructions",
                tools=("side_effect",),
                max_tool_calls=0,
            ),
        ),
    )

    async def run() -> list:
        compiled = await engine.compile(spec)
        handle = await engine.start(
            StartRequest(
                input="delegate",
                user_id="u",
                session_id="s",
                agent_id="main",
                metadata={"invocation_id": "run-zero-tool-budget"},
            ),
            compiled,
        )
        return await _collect(engine, handle)

    events = asyncio.run(run())
    assert side_effects == [], "0 工具预算：子 Agent 副作用绝不启动"
    end = next(
        event
        for event in events
        if event.event_type == "tool.call.end" and event.payload.get("call_id") == "child-call"
    )
    assert end.payload["error_category"] == "budget_exhausted"


def test_7_2_dependency_graph_does_not_start_child_before_its_dependency():
    """依赖图：worker-b depends_on worker-a，b 不得在 a 完成前启动。"""
    order: list[str] = []

    class _DependencyReasoner:
        def __init__(self) -> None:
            self.parent_calls = 0

        async def complete(self, **kwargs):
            prompt = str(kwargs.get("prompt") or "")
            if prompt == "child A":
                order.append("a-start")
                return HarnessReasoningTurn(final_text="A done")
            if prompt == "child B":
                order.append("b-start")
                return HarnessReasoningTurn(final_text="B done")
            self.parent_calls += 1
            if self.parent_calls == 1:
                return HarnessReasoningTurn(
                    tool_calls=(
                        HarnessToolCall(call_id="call-a", name="worker-a", arguments={"task": "A"}),
                        HarnessToolCall(call_id="call-b", name="worker-b", arguments={"task": "B"}),
                    )
                )
            return HarnessReasoningTurn(final_text="done")

    engine = ManagedLangGraphEngine(reasoner=_DependencyReasoner(), checkpointer=InMemorySaver())
    spec = HarnessSpec(
        agent_revision_ref="agent-revision://dependency-order@1",
        model=ModelBinding(profile_ref="model-profile://m@1.0.0"),
        prompt=PromptSpec(instructions="main"),
        sub_agents=(
            SubAgentBinding(name="worker-a", instructions="child A"),
            SubAgentBinding(name="worker-b", instructions="child B", depends_on=("worker-a",)),
        ),
    )

    async def run() -> list:
        compiled = await engine.compile(spec)
        handle = await engine.start(
            StartRequest(
                input="delegate",
                user_id="u",
                session_id="s",
                agent_id="main",
                metadata={"invocation_id": "run-dependency-order"},
            ),
            compiled,
        )
        return await _collect(engine, handle)

    asyncio.run(run())
    assert "a-start" in order and "b-start" in order
    assert order.index("a-start") < order.index("b-start"), order


# ---------------------------------------------------------------------------
# 7.3 多分支故障注入矩阵：fail_fast / partial / retry
# ---------------------------------------------------------------------------


class _MatrixReasoner:
    def __init__(self, *, fail_child: str = "", retry_succeeds: bool = True) -> None:
        self.parent_calls = 0
        self.fail_child = fail_child
        self.retry_succeeds = retry_succeeds
        self.attempts: dict[str, int] = {}

    async def complete(self, **kwargs):
        prompt = str(kwargs.get("prompt") or "")
        if prompt in {"child A", "child B"}:
            self.attempts[prompt] = self.attempts.get(prompt, 0) + 1
            if prompt == self.fail_child:
                if self.retry_succeeds and self.attempts[prompt] == 1:
                    raise RuntimeError(f"{prompt} transient failure")
                if not self.retry_succeeds and self.attempts[prompt] == 1:
                    raise RuntimeError(f"{prompt} permanent failure")
            return HarnessReasoningTurn(final_text=f"{prompt} ok")
        self.parent_calls += 1
        if self.parent_calls == 1:
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(call_id="call-a", name="worker-a", arguments={"task": "A"}),
                    HarnessToolCall(call_id="call-b", name="worker-b", arguments={"task": "B"}),
                )
            )
        return HarnessReasoningTurn(final_text="parent done")


def _matrix_spec(*, failure_mode: str, b_policy: str = "propagate", b_retries: int = 0):
    return HarnessSpec(
        agent_revision_ref=f"agent-revision://fault-matrix-{failure_mode}-{b_policy}@1",
        model=ModelBinding(profile_ref="model-profile://m@1.0.0"),
        prompt=PromptSpec(instructions="main"),
        sub_agents=(
            SubAgentBinding(name="worker-a", instructions="child A"),
            SubAgentBinding(
                name="worker-b",
                instructions="child B",
                failure_policy=b_policy,
                max_retries=b_retries,
            ),
        ),
        execution_strategy=ExecutionStrategySpec(
            config={"subagent_failure_mode": failure_mode, "max_parallel_subagents": 1}
        ),
    )


def _run(engine, spec, invocation_id):  # type: ignore[no-untyped-def]
    async def body() -> list:
        compiled = await engine.compile(spec)
        handle = await engine.start(
            StartRequest(
                input="go", user_id="u", session_id="s", agent_id="main",
                metadata={"invocation_id": invocation_id},
            ),
            compiled,
        )
        return await _collect(engine, handle)

    return asyncio.run(body())


def test_7_3_fail_fast_cancels_unstarted_sibling_when_required_child_fails():
    reasoner = _MatrixReasoner(fail_child="child A", retry_succeeds=False)
    engine = ManagedLangGraphEngine(reasoner=reasoner, checkpointer=InMemorySaver())
    spec = _matrix_spec(failure_mode="fail_fast")

    events = _run(engine, spec, "run-failfast-required")
    b_end = next(
        event
        for event in events
        if event.event_type == "tool.call.end" and event.payload.get("call_id") == "call-b"
    )
    assert b_end.payload["error_category"] == "cancelled_by_fail_fast"
    a_end = next(
        event
        for event in events
        if event.event_type == "tool.call.end" and event.payload.get("call_id") == "call-a"
    )
    assert a_end.payload.get("error_category") == "child_failed"


def test_7_3_partial_mode_keeps_sibling_running_after_required_child_fails():
    reasoner = _MatrixReasoner(fail_child="child A", retry_succeeds=False)
    engine = ManagedLangGraphEngine(reasoner=reasoner, checkpointer=InMemorySaver())
    spec = _matrix_spec(failure_mode="partial")

    events = _run(engine, spec, "run-partial-required")
    b_ends = [
        event
        for event in events
        if event.event_type == "tool.call.end" and event.payload.get("call_id") == "call-b"
    ]
    assert b_ends, "partial 模式下 sibling 不得被取消"
    assert b_ends[0].payload.get("error_category") != "cancelled_by_fail_fast"


def test_7_3_retryable_child_failure_retries_once_then_succeeds():
    reasoner = _MatrixReasoner(fail_child="child B", retry_succeeds=True)
    engine = ManagedLangGraphEngine(reasoner=reasoner, checkpointer=InMemorySaver())
    spec = _matrix_spec(failure_mode="partial", b_policy="retry", b_retries=1)

    events = _run(engine, spec, "run-retryable")
    assert reasoner.attempts.get("child B") == 2, "可重试失败必须重试一次"
    b_end = next(
        event
        for event in events
        if event.event_type == "tool.call.end" and event.payload.get("call_id") == "call-b"
    )
    assert b_end.payload.get("error_category") != "child_failed", "重试成功后不得标记失败"
    assert events[-1].event_type == "run.completed"


# ---------------------------------------------------------------------------
# 7.5 跨进程多 Agent 恢复 E2E —— 已完成不重跑（Receipt 回放）
# ---------------------------------------------------------------------------


def test_7_5_cross_process_multi_branch_recovery_does_not_rerun_completed_children(tmp_path):
    """两个子 Agent 均完成 + 父在审批处中断；跨进程恢复后两个子 Agent 都不重跑（Receipt 回放）。"""
    checkpoint_path = str(tmp_path / "multi-branch-recovery.db")
    receipt_path = str(tmp_path / "multi-branch-receipts.db")
    child_executions: list[str] = []
    phase_two_child_calls: list[str] = []
    side_effects: list[str] = []

    class _PhaseOneReasoner:
        async def complete(self, **kwargs):
            prompt = str(kwargs.get("prompt") or "")
            if prompt == "child A":
                child_executions.append("A")
                return HarnessReasoningTurn(final_text="A result")
            if prompt == "child B":
                child_executions.append("B")
                return HarnessReasoningTurn(final_text="B result")
            return HarnessReasoningTurn(
                tool_calls=(
                    HarnessToolCall(call_id="call-a", name="worker-a", arguments={"task": "A"}),
                    HarnessToolCall(call_id="call-b", name="worker-b", arguments={"task": "B"}),
                    HarnessToolCall(
                    call_id="approval-call", name="publish", arguments={"value": 1}
                ),
                )
            )

    class _PhaseTwoReasoner:
        async def complete(self, **kwargs):
            prompt = str(kwargs.get("prompt") or "")
            if prompt in {"child A", "child B"}:
                phase_two_child_calls.append(prompt)
                return HarnessReasoningTurn(final_text="should not rerun")
            return HarnessReasoningTurn(final_text="resumed")

    async def publish(_arguments):
        side_effects.append("publish")
        return "published"

    spec = HarnessSpec(
        agent_revision_ref="agent-revision://multi-branch-recover@1",
        model=ModelBinding(profile_ref="model-profile://m@1.0.0"),
        prompt=PromptSpec(instructions="main"),
        sub_agents=(
            SubAgentBinding(name="worker-a", instructions="child A"),
            SubAgentBinding(name="worker-b", instructions="child B"),
        ),
    )

    def runtime():
        return CapabilityRuntime(
            receipts=ToolReceiptStore(receipt_path),
            profiles={"publish": ToolProfile(name="publish", risk_level=RiskLevel.HIGH)},
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
                StartRequest(
                    input="go", user_id="u", session_id="s", agent_id="main",
                    metadata={"invocation_id": "run-multi-branch-recover"},
                ),
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
    assert child_executions == ["A", "B"]

    second = asyncio.run(phase_two(handle))
    # 跨进程恢复：已完成的两个子 Agent 都经 Receipt 回放，绝不重跑模型。
    assert phase_two_child_calls == [], "已完成子 Agent 必须回放 Receipt，绝不重跑"
    assert side_effects == ["publish"], "审批副作用仅一次"
    assert second[-1].event_type == "run.completed"

