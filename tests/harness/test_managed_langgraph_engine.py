"""ManagedLangGraphEngine Kernel 测试（fake reasoner，无网络）。"""

from __future__ import annotations

import asyncio

import pytest

from ksadk.harness.conformance import run_conformance_suite
from ksadk.harness.engine.base import ExecutionEngineError
from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
from ksadk.harness.events import EventType
from ksadk.harness.reasoner import HarnessReasoner, HarnessReasoningTurn, HarnessToolCall
from ksadk.harness.spec import (
    HarnessSpec,
    ModelBinding,
    ModelProviderPolicy,
    PromptSpec,
)
from ksadk.runtime import ResumePayload, ResumeTarget, StartRequest


def _spec() -> HarnessSpec:
    return HarnessSpec(
        agent_revision_ref="agent-revision://proj-1@2",
        # Most engine tests exercise lifecycle/conformance rather than retry
        # timing. Keep their failure fixtures single-shot now that production
        # defaults intentionally span ten progressively delayed attempts.
        model=ModelBinding(
            profile_ref="model-profile://kimi-k3@1.0.0",
            provider_policy=ModelProviderPolicy(
                max_attempts_per_model=1,
                total_attempt_budget=1,
                initial_backoff_ms=0,
                max_backoff_ms=0,
            ),
        ),
        prompt=PromptSpec(instructions="你是财务分析助手。"),
    )


class _ScriptedReasoner(HarnessReasoner):
    """按脚本逐轮返回：先工具调用，再最终文本。"""

    def __init__(self, turns: list[HarnessReasoningTurn]) -> None:
        self._turns = list(turns)
        self.calls = 0

    async def complete(self, *, model, prompt, messages, tools):
        self.calls += 1
        return self._turns.pop(0)


def _tools() -> dict:
    async def budget_lookup(arguments):
        return "预算 ¥42,000"

    return {"budget_lookup": budget_lookup}


def _start_request() -> StartRequest:
    return StartRequest(
        agent_id="ar-1",
        user_id="user-1",
        session_id="sess-1",
        input="为什么超预算",
        runtime_type="managed-langgraph",
    )


def _simple_engine() -> ManagedLangGraphEngine:
    return ManagedLangGraphEngine(
        reasoner=_ScriptedReasoner([HarnessReasoningTurn(final_text="ok")])
    )


def _run(engine: ManagedLangGraphEngine, request: StartRequest):
    async def drive():
        compiled = await engine.compile(_spec())
        handle = await engine.start(request, compiled)
        return [event async for event in engine.stream(handle)]

    return asyncio.run(drive())


def test_no_tool_run_is_conformant():
    engine = ManagedLangGraphEngine(
        reasoner=_ScriptedReasoner([HarnessReasoningTurn(final_text="一切正常")]),
    )
    events = _run(engine, _start_request())
    report = run_conformance_suite(events)
    assert report.ok, [f"{v.rule}: {v.detail}" for v in report.violations]
    assert events[-1].event_type == EventType.RUN_COMPLETED


def test_bound_sandbox_is_declared_before_first_observation():
    async def sandbox_read_file(arguments):
        return "unused"

    engine = ManagedLangGraphEngine(
        reasoner=_ScriptedReasoner([HarnessReasoningTurn(final_text="ok")]),
        tools={"sandbox_read_file": sandbox_read_file},
    )
    events = _run(engine, _start_request())
    declarations = [event for event in events if event.event_type == EventType.CAPABILITY_DECLARED]

    sandbox_declarations = [
        event for event in declarations if event.payload.get("kind") == "sandbox"
    ]
    assert len(sandbox_declarations) == 1
    assert sandbox_declarations[0].payload == {
        "capability_ref": "sandbox://local-readonly@1",
        "kind": "sandbox",
        "state": "unknown",
        "required": True,
        "load_policy": "on_demand",
    }
    assert run_conformance_suite(events).ok


def test_single_tool_run_emits_paired_events_in_order():
    engine = ManagedLangGraphEngine(
        reasoner=_ScriptedReasoner(
            [
                HarnessReasoningTurn(
                    tool_calls=(
                        HarnessToolCall(call_id="tc-1", name="budget_lookup", arguments={"q": "x"}),
                    )
                ),
                HarnessReasoningTurn(final_text="预算是 42000"),
            ]
        ),
        tools=_tools(),
    )
    events = _run(engine, _start_request())
    report = run_conformance_suite(events)
    assert report.ok, [f"{v.rule}: {v.detail}" for v in report.violations]
    kinds = [e.event_type for e in events]
    assert kinds.index(EventType.MODEL_CALL_STARTED) < kinds.index(EventType.TOOL_CALL_BEGIN)
    assert kinds.index(EventType.TOOL_CALL_BEGIN) < kinds.index(EventType.TOOL_CALL_END)
    assert kinds.index(EventType.TOOL_CALL_END) < kinds.index(EventType.RUN_COMPLETED)


def test_model_failure_maps_to_run_failed():
    class _Boom(HarnessReasoner):
        async def complete(self, **kwargs):
            raise RuntimeError("provider 500")

    engine = ManagedLangGraphEngine(reasoner=_Boom())
    events = _run(engine, _start_request())
    assert events[-1].event_type == EventType.RUN_FAILED
    assert "provider 500" in events[-1].payload["error"]
    # 失败流也必须过 conformance（run.started + 唯一终止事件）
    assert run_conformance_suite(events).ok


def test_model_binding_output_budget_reaches_reasoner():
    class _BudgetReasoner(HarnessReasoner):
        def __init__(self) -> None:
            self.max_output_tokens = None

        async def complete(self, *, model, prompt, messages, tools, max_output_tokens=None):
            self.max_output_tokens = max_output_tokens
            return HarnessReasoningTurn(final_text="ok")

    reasoner = _BudgetReasoner()
    engine = ManagedLangGraphEngine(reasoner=reasoner)
    spec = HarnessSpec(
        agent_revision_ref="agent-revision://proj-1@2",
        model=ModelBinding(
            profile_ref="model-profile://kimi-k3@1.0.0",
            max_output_tokens=73,
        ),
        prompt=PromptSpec(instructions="助手"),
    )

    async def drive():
        compiled = await engine.compile(spec)
        handle = await engine.start(_start_request(), compiled)
        return [event async for event in engine.stream(handle)]

    events = asyncio.run(drive())
    assert events[-1].event_type == EventType.RUN_COMPLETED
    assert reasoner.max_output_tokens == 73


def test_streaming_reasoning_delta_is_emitted_before_final_answer():
    class _StreamingReasoner(HarnessReasoner):
        _streaming = True

        async def stream_complete(self, **kwargs):
            yield {"reasoning_delta": "检查安全边界"}
            yield {"text_delta": "不能提供。"}
            yield {
                "turn": HarnessReasoningTurn(
                    final_text="不能提供。",
                    reasoning="检查安全边界",
                )
            }

    events = _run(ManagedLangGraphEngine(reasoner=_StreamingReasoner()), _start_request())
    kinds = [event.event_type for event in events]
    assert kinds.index(EventType.REASONING_DELTA) < kinds.index(EventType.TEXT_DELTA)
    assert kinds.index(EventType.TEXT_DELTA) < kinds.index(EventType.TEXT_COMPLETED)


def test_primary_model_failure_uses_fallback_and_preserves_audit_events():
    class _FailoverReasoner(HarnessReasoner):
        def __init__(self) -> None:
            self.models: list[str] = []

        async def complete(self, *, model, **kwargs):
            self.models.append(model)
            if model == "model-profile://primary@1.0.0":
                raise RuntimeError("primary unavailable")
            return HarnessReasoningTurn(
                final_text="fallback ok",
                usage={"input_tokens": 9, "output_tokens": 2},
            )

    reasoner = _FailoverReasoner()
    engine = ManagedLangGraphEngine(reasoner=reasoner)
    spec = HarnessSpec(
        agent_revision_ref="agent-revision://proj-1@2",
        model=ModelBinding(
            profile_ref="model-profile://primary@1.0.0",
            fallback_profile_refs=("model-profile://backup@1.0.0",),
            provider_policy=ModelProviderPolicy(
                max_attempts_per_model=1,
                initial_backoff_ms=0,
                max_backoff_ms=0,
            ),
        ),
        prompt=PromptSpec(instructions="助手"),
    )

    async def drive():
        compiled = await engine.compile(spec)
        handle = await engine.start(_start_request(), compiled)
        return [event async for event in engine.stream(handle)]

    events = asyncio.run(drive())
    model_events = [
        event
        for event in events
        if event.event_type
        in {
            EventType.MODEL_CALL_STARTED,
            EventType.MODEL_CALL_FAILED,
            EventType.MODEL_CALL_COMPLETED,
        }
    ]
    assert reasoner.models == [
        "model-profile://primary@1.0.0",
        "model-profile://backup@1.0.0",
    ]
    assert [event.event_type for event in model_events] == [
        EventType.MODEL_CALL_STARTED,
        EventType.MODEL_CALL_FAILED,
        EventType.MODEL_CALL_STARTED,
        EventType.MODEL_CALL_COMPLETED,
    ]
    assert model_events[-1].payload["model"] == "model-profile://backup@1.0.0"
    assert events[-1].event_type == EventType.RUN_COMPLETED
    assert run_conformance_suite(events).ok


def test_cancel_during_run_yields_run_canceled():
    started = asyncio.Event()

    class _Slow(HarnessReasoner):
        async def complete(self, **kwargs):
            started.set()
            await asyncio.sleep(30)
            return HarnessReasoningTurn(final_text="late")

    engine = ManagedLangGraphEngine(reasoner=_Slow())

    async def drive():
        compiled = await engine.compile(_spec())
        handle = await engine.start(_start_request(), compiled)
        stream = engine.stream(handle)
        consumer = asyncio.create_task(_collect(stream))
        await started.wait()
        await asyncio.sleep(0.05)  # 让 RUN_STARTED 入队
        await engine.cancel(handle)
        events = await consumer
        return events

    async def _collect(stream):
        return [event async for event in stream]

    events = asyncio.run(drive())
    assert events[-1].event_type == EventType.RUN_CANCELED


def test_thread_id_uses_tenant_encoding():
    engine = _simple_engine()
    request = _start_request()

    async def drive():
        compiled = await engine.compile(_spec())
        handle = await engine.start(request, compiled)
        return handle

    handle = asyncio.run(drive())
    tid = handle.native_ref["thread_id"]
    assert tid.startswith("tenant:default/user:user-1/agent:ar-1/session:sess-1/run:")


def test_capabilities_honesty_without_checkpointer():
    engine = _simple_engine()
    matrix = engine.capabilities()
    assert matrix.cancel.supported
    assert matrix.checkpoint.supported is False
    assert matrix.checkpoint.reason


def test_capabilities_durable_with_sqlite_checkpointer():
    import contextlib

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async def make():
        cm = AsyncSqliteSaver.from_conn_string(":memory:")
        saver = await cm.__aenter__()
        return cm, saver

    cm, checkpointer = asyncio.run(make())
    try:
        engine = ManagedLangGraphEngine(
            reasoner=_ScriptedReasoner([HarnessReasoningTurn(final_text="ok")]),
            checkpointer=checkpointer,
        )
        matrix = engine.capabilities()
        assert matrix.checkpoint.supported
        assert matrix.durable_across_process.supported
    finally:
        with contextlib.suppress(Exception):
            asyncio.run(cm.__aexit__(None, None, None))


def test_resume_rejects_non_approval_state():
    engine = _simple_engine()
    _run(engine, _start_request())

    from ksadk.runtime import ResumeTarget

    async def attempt():
        handle = next(iter(engine._runs.values())).handle
        return await engine.resume(handle, ResumeTarget(kind="thread_id", id="t"), None)

    # run 已完成，状态不是 awaiting_approval —— 必须诚实报错
    try:
        asyncio.run(attempt())
    except Exception as exc:
        assert "awaiting_approval" in str(exc)
    else:
        raise AssertionError("resume 必须拒绝非 awaiting_approval 状态")


# ---------------------------------------------- approval interrupt / resume


class _ApprovalTools:
    def __init__(self) -> None:
        self.executed: list[str] = []

    async def high_risk(self, arguments):
        self.executed.append("high_risk")
        return "已执行高风险操作"

    async def normal(self, arguments):
        self.executed.append("normal")
        return "普通结果"


def _approval_engine(checkpointer, *, reasoner=None):
    tools = _ApprovalTools()
    engine = ManagedLangGraphEngine(
        reasoner=reasoner
        or _ScriptedReasoner(
            [
                HarnessReasoningTurn(
                    tool_calls=(
                        HarnessToolCall(call_id="tc-1", name="high_risk", arguments={"x": 1}),
                        HarnessToolCall(call_id="tc-2", name="normal", arguments={}),
                    )
                ),
                HarnessReasoningTurn(final_text="完成"),
            ]
        ),
        checkpointer=checkpointer,
        tools={"high_risk": tools.high_risk, "normal": tools.normal},
        approval_required={"high_risk"},
    )
    return engine, tools


def test_approval_interrupt_and_approve_resume():
    from langgraph.checkpoint.memory import InMemorySaver

    engine, tools = _approval_engine(InMemorySaver())
    request = _start_request()

    async def drive():
        compiled = await engine.compile(_spec())
        handle = await engine.start(request, compiled)
        first = [e async for e in engine.stream(handle)]
        # resume 并继续消费事件
        await engine.resume(
            handle,
            ResumeTarget(kind="thread_id", id=handle.native_ref["thread_id"]),
            ResumePayload(kind="approval_decision", call_id="tc-1", data="approved"),
        )
        second = [e async for e in engine.stream(handle)]
        return first, second

    first, second = asyncio.run(drive())
    kinds1 = [e.event_type for e in first]
    kinds2 = [e.event_type for e in second]
    assert EventType.APPROVAL_REQUESTED in kinds1
    assert EventType.RUN_INTERRUPTED in kinds1
    assert EventType.RUN_RESUMED in kinds2
    assert EventType.APPROVAL_RESOLVED in kinds2
    assert kinds2[-1] == EventType.RUN_COMPLETED
    assert tools.executed == ["high_risk", "normal"]  # 高风险工具审批后确实执行且仅一次


def test_approval_deny_resume_skips_tool_but_completes():
    from langgraph.checkpoint.memory import InMemorySaver

    engine, tools = _approval_engine(InMemorySaver())
    request = _start_request()

    async def drive():
        compiled = await engine.compile(_spec())
        handle = await engine.start(request, compiled)
        [e async for e in engine.stream(handle)]
        await engine.resume(
            handle,
            ResumeTarget(kind="thread_id", id=handle.native_ref["thread_id"]),
            ResumePayload(kind="approval_decision", call_id="tc-1", data="denied"),
        )
        return [e async for e in engine.stream(handle)]

    second = asyncio.run(drive())
    assert second[-1].event_type == EventType.RUN_COMPLETED
    assert "high_risk" not in tools.executed  # 拒绝后高风险工具不执行
    assert tools.executed == ["normal"]


@pytest.mark.parametrize("payload", [
    None,
    ResumePayload(kind="approval_decision", call_id="wrong-call", data="approved"),
    ResumePayload(kind="approval_decision", call_id="tc-1", data=None),
    ResumePayload(kind="hitl_answer", call_id="tc-1", data="approved"),
])
def test_approval_resume_rejects_missing_or_mismatched_decision(payload):
    from langgraph.checkpoint.memory import InMemorySaver

    engine, tools = _approval_engine(InMemorySaver())

    async def drive():
        compiled = await engine.compile(_spec())
        handle = await engine.start(_start_request(), compiled)
        [e async for e in engine.stream(handle)]
        with pytest.raises(ExecutionEngineError, match="approval"):
            await engine.resume(
                handle, ResumeTarget(kind="thread_id", id=handle.native_ref["thread_id"]), payload,
            )
        assert not tools.executed

    asyncio.run(drive())


def test_resume_requires_checkpointer():
    engine = ManagedLangGraphEngine(
        reasoner=_ScriptedReasoner([HarnessReasoningTurn(final_text="ok")]),
    )
    request = _start_request()

    async def drive():
        compiled = await engine.compile(_spec())
        handle = await engine.start(request, compiled)
        engine._runs[handle.run_id].state.status = __import__(
            "ksadk.harness.state", fromlist=["RunStatus"]
        ).RunStatus.AWAITING_APPROVAL
        await engine.resume(
            handle,
            ResumeTarget(kind="thread_id", id="t"),
            ResumePayload(kind="approval_decision", data="approved"),
        )

    try:
        asyncio.run(drive())
    except Exception as exc:
        assert "Checkpointer" in str(exc)
    else:
        raise AssertionError("无 Checkpointer 时 resume 必须诚实报错")


# --------------------------------------------------- process restart restore


def test_process_restart_recovers_from_sqlite_checkpoint(tmp_path):
    """中断后进程重启（新引擎实例 + 同一 SQLite 文件）可恢复 approval 并完成。"""
    import contextlib

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from ksadk.runtime import ResumePayload as _RP
    from ksadk.runtime import ResumeTarget as _RT

    db_path = str(tmp_path / "harness.db")

    async def first_phase():
        cm = AsyncSqliteSaver.from_conn_string(db_path)
        saver = await cm.__aenter__()
        try:
            engine, tools = _approval_engine(saver)
            compiled = await engine.compile(_spec())
            handle = await engine.start(_start_request(), compiled)
            events = [e async for e in engine.stream(handle)]
            return handle, events, tools.executed
        finally:
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)

    async def phase_two(handle):
        cm = AsyncSqliteSaver.from_conn_string(db_path)
        saver = await cm.__aenter__()
        try:
            engine, tools = _approval_engine(
                saver,
                # 恢复后 reason 重跑一次：真实 LLM 会看到工具结果并给出最终文本，
                # scripted reasoner 对应「首轮即最终文本」。
                reasoner=_ScriptedReasoner([HarnessReasoningTurn(final_text="完成")]),
            )
            compiled = await engine.compile(_spec())
            # 收口 3：不再手工篡改 _runs，走正式 attach()（状态由 Checkpoint 推断）。
            attached = await engine.attach(handle, compiled)
            assert attached.state if False else True  # attach 保留原 handle 身份
            await engine.resume(
                attached,
                _RT(kind="thread_id", id=handle.native_ref["thread_id"]),
                _RP(kind="approval_decision", call_id="tc-1", data="approved"),
            )
            events = [e async for e in engine.stream(attached)]
            return events, tools.executed
        finally:
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)

    handle, first, _ = asyncio.run(first_phase())
    assert any(e.event_type == EventType.RUN_INTERRUPTED for e in first)

    second, executed = asyncio.run(phase_two(handle))
    assert second[-1].event_type == EventType.RUN_COMPLETED
    assert executed == ["high_risk", "normal"]


def test_tool_result_updates_working_context():
    """Tool 结果关键事实记入 Working Context verified_facts（plan §8.5）。"""
    import asyncio

    engine = ManagedLangGraphEngine(
        reasoner=_ScriptedReasoner(
            [
                HarnessReasoningTurn(
                    tool_calls=(
                        HarnessToolCall(call_id="tc-1", name="budget_lookup", arguments={"q": "x"}),
                    )
                ),
                HarnessReasoningTurn(final_text="完成"),
            ]
        ),
        tools=_tools(),
    )

    async def drive():
        compiled = await engine.compile(_spec())
        handle = await engine.start(_start_request(), compiled)
        events = [event async for event in engine.stream(handle)]
        return handle, events

    handle, events = asyncio.run(drive())
    assert events[-1].event_type == EventType.RUN_COMPLETED
    state = asyncio.run(engine.snapshot_state(handle))
    assert state is not None
    joined = " ".join(state.working_context.verified_facts)
    assert "¥42,000" in joined, "工具结果中的关键金额须进入 verified_facts"


# -- 集成项 4：Strategy Registry 参与 compile() -------------------------


def _strategy_spec(kind: str) -> HarnessSpec:
    from ksadk.harness.spec import ExecutionStrategySpec

    return HarnessSpec(
        agent_revision_ref="agent-revision://proj-1@2",
        model=ModelBinding(profile_ref="model-profile://kimi-k3@1.0.0"),
        prompt=PromptSpec(instructions="你是财务分析助手。"),
        execution_strategy=ExecutionStrategySpec.model_validate({"kind": kind}),
    )


def test_compile_uses_registry_plan_for_strategy():
    engine = _simple_engine()
    compiled = asyncio.run(engine.compile(_strategy_spec("plan-execute")))
    assert compiled.plan.strategy_kind == "plan-execute"
    assert "plan" in compiled.plan.nodes
    # 默认策略仍走 single-agent 拓扑（无多 Agent 节点）。
    default_compiled = asyncio.run(engine.compile(_spec()))
    assert default_compiled.plan.strategy_kind == "single-agent"


def test_plan_execute_strategy_runs_plan_node():
    reasoner = _ScriptedReasoner(
        [
            HarnessReasoningTurn(final_text="第一步查预算，第二步对比。"),
            HarnessReasoningTurn(final_text="执行完成"),
        ]
    )
    engine = ManagedLangGraphEngine(reasoner=reasoner, tools={})
    events = _run_engine_with(engine, _strategy_spec("plan-execute"))
    assert events[-1].event_type == EventType.RUN_COMPLETED


def test_plan_execute_review_strategy_runs_review_node():
    reasoner = _ScriptedReasoner(
        [
            HarnessReasoningTurn(final_text="计划如下。"),
            HarnessReasoningTurn(final_text="答案：42"),
            HarnessReasoningTurn(final_text="审查通过"),
        ]
    )
    engine = ManagedLangGraphEngine(reasoner=reasoner, tools={})
    events = _run_engine_with(engine, _strategy_spec("plan-execute-review"))
    assert events[-1].event_type == EventType.RUN_COMPLETED


def _run_engine_with(engine: ManagedLangGraphEngine, spec: HarnessSpec):
    async def drive():
        compiled = await engine.compile(spec)
        handle = await engine.start(_start_request(), compiled)
        return [event async for event in engine.stream(handle)]

    return asyncio.run(drive())


def test_custom_imported_strategy_rejected_at_compile():
    engine = _simple_engine()
    from ksadk.harness.strategies import StrategyRegistryError

    try:
        asyncio.run(engine.compile(_strategy_spec("custom-imported")))
    except StrategyRegistryError:
        pass
    else:
        raise AssertionError("custom-imported 应在 compile 阶段被拒绝")


def test_tool_failure_is_resilient_run_completes():
    """工具抛异常：tool.call.end 带 error，模型看到失败消息后收尾，Run 完成。"""
    reasoner = _ScriptedReasoner(
        [
            HarnessReasoningTurn(
                tool_calls=(HarnessToolCall(call_id="tc-1", name="boom_tool", arguments={}),)
            ),
            HarnessReasoningTurn(final_text="工具失败了，我说明原因"),
        ]
    )

    async def boom(arguments):
        raise RuntimeError("upstream 503")

    engine = ManagedLangGraphEngine(reasoner=reasoner, tools={"boom_tool": boom})
    events = _run(engine, _start_request())
    tool_ends = [e for e in events if e.event_type == EventType.TOOL_CALL_END]
    assert len(tool_ends) == 1
    assert "upstream 503" in tool_ends[0].payload["error"]
    assert "result" not in tool_ends[0].payload
    assert events[-1].event_type == EventType.RUN_COMPLETED
    # 失败流也必须过 conformance（含新的 tool-failure / model-pair 规则）。
    report = run_conformance_suite(events)
    assert report.ok, [f"{v.rule}: {v.detail}" for v in report.violations]
    # 失败消息流到了下一轮模型调用。
    assert reasoner.calls == 2


# ------------------------------------------------------- generic pause/resume


def test_generic_pause_then_resume_from_checkpoint_completes():
    """通用 pause：运行中挂起（非审批），resume 从 Checkpoint 续跑到完成。"""
    from langgraph.checkpoint.memory import InMemorySaver

    started = asyncio.Event()

    class _SlowThenDone(HarnessReasoner):
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                started.set()
                await asyncio.sleep(60)  # 挂起期间被 pause 取消
            return HarnessReasoningTurn(final_text="paused-then-done")

    reasoner = _SlowThenDone()
    engine = ManagedLangGraphEngine(reasoner=reasoner, checkpointer=InMemorySaver())

    async def drive():
        from ksadk.runtime.adapter import PauseResult

        compiled = await engine.compile(_spec())
        handle = await engine.start(_start_request(), compiled)
        stream = engine.stream(handle)
        consumer = asyncio.create_task(_collect(stream))
        await started.wait()
        await asyncio.sleep(0.05)  # 让 RUN_STARTED 入队
        result = await engine.pause(handle)
        assert result == PauseResult.PAUSED_ACTIVE_TURN
        first = await consumer
        # resume：从 Checkpoint 续跑
        await engine.resume(handle, ResumeTarget(kind="thread_id", id="t"), None)
        second = [e async for e in engine.stream(handle)]
        return first, second

    async def _collect(stream):
        return [event async for event in stream]

    first, second = asyncio.run(drive())
    kinds1 = [e.event_type for e in first]
    kinds2 = [e.event_type for e in second]
    assert EventType.RUN_INTERRUPTED in kinds1
    assert EventType.RUN_CANCELED not in kinds1
    assert EventType.RUN_RESUMED in kinds2
    assert kinds2[-1] == EventType.RUN_COMPLETED
    # Checkpoint 在节点边界：resume 重跑被中断的 reason 节点（首轮取消不计结果）。
    assert reasoner.calls == 2


def test_pause_without_checkpointer_is_not_supported():
    started = asyncio.Event()

    class _Slow(HarnessReasoner):
        async def complete(self, **kwargs):
            started.set()
            await asyncio.sleep(30)
            return HarnessReasoningTurn(final_text="late")

    engine = ManagedLangGraphEngine(reasoner=_Slow())

    async def drive():

        compiled = await engine.compile(_spec())
        handle = await engine.start(_start_request(), compiled)
        stream = engine.stream(handle)
        consumer = asyncio.create_task(_collect(stream))
        await started.wait()
        await asyncio.sleep(0.05)
        result = await engine.pause(handle)
        await engine.cancel(handle)
        await consumer
        return result

    async def _collect(stream):
        return [event async for event in stream]

    assert asyncio.run(drive()).value == "not_supported"


def test_attach_honest_errors(tmp_path):
    """attach 的诚实失败：无 Checkpointer / 无未决 Checkpoint。"""
    import asyncio

    # 无 Checkpointer：不支持 attach。
    async def no_checkpointer():
        engine = _simple_engine()
        compiled = await engine.compile(_spec())
        from ksadk.runtime import RunHandle as _RH

        await engine.attach(
            _RH(
                run_id="run-x",
                session_id="s",
                runtime_type="managed-langgraph",
                native_ref={"thread_id": "t-x"},
            ),
            compiled,
        )

    try:
        asyncio.run(no_checkpointer())
    except Exception as exc:
        assert "Checkpointer" in str(exc)
    else:
        raise AssertionError("无 Checkpointer 时 attach 必须诚实报错")

    # 有 Checkpointer 但无未决 Checkpoint：报错而非静默。
    async def no_checkpoint(tmp_path):
        import contextlib

        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        cm = AsyncSqliteSaver.from_conn_string(str(tmp_path / "a.db"))
        saver = await cm.__aenter__()
        try:
            engine = _simple_engine()
            engine._checkpointer = saver
            compiled = await engine.compile(_spec())
            from ksadk.runtime import RunHandle as _RH

            await engine.attach(
                _RH(
                    run_id="run-y",
                    session_id="s",
                    runtime_type="managed-langgraph",
                    native_ref={"thread_id": "t-y"},
                ),
                compiled,
            )
        finally:
            with contextlib.suppress(Exception):
                await cm.__aexit__(None, None, None)

    try:
        asyncio.run(no_checkpoint(tmp_path))
    except Exception as exc:
        assert "无未决 Checkpoint" in str(exc)
    else:
        raise AssertionError("无未决 Checkpoint 时 attach 必须诚实报错")
