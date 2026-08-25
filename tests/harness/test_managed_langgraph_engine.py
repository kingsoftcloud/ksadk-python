"""ManagedLangGraphEngine Phase 1 测试（fake reasoner，无网络）。"""

from __future__ import annotations

import asyncio

from ksadk.events import EventType
from ksadk.harness.conformance import run_conformance_suite
from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
from ksadk.harness.reasoner import HarnessReasoner, HarnessReasoningTurn, HarnessToolCall
from ksadk.harness.spec import HarnessSpec, ModelBinding, PromptSpec
from ksadk.runtime import StartRequest


def _spec() -> HarnessSpec:
    return HarnessSpec(
        agent_revision_ref="agent-revision://proj-1@2",
        model=ModelBinding(profile_ref="model-profile://kimi-k3@1.0.0"),
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
        return "预算 42000"

    return {"budget_lookup": budget_lookup}


def _start_request() -> StartRequest:
    return StartRequest(
        agent_id="ar-1", user_id="user-1", session_id="sess-1", input="为什么超预算",
        runtime_type="managed-langgraph",
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


def test_single_tool_run_emits_paired_events_in_order():
    engine = ManagedLangGraphEngine(
        reasoner=_ScriptedReasoner([
            HarnessReasoningTurn(tool_calls=(
                HarnessToolCall(call_id="tc-1", name="budget_lookup", arguments={"q": "x"}),
            )),
            HarnessReasoningTurn(final_text="预算是 42000"),
        ]),
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
    engine = ManagedLangGraphEngine(reasoner=_ScriptedReasoner([HarnessReasoningTurn(final_text="ok")]))
    request = _start_request()

    async def drive():
        compiled = await engine.compile(_spec())
        handle = await engine.start(request, compiled)
        return handle

    handle = asyncio.run(drive())
    tid = handle.native_ref["thread_id"]
    assert tid.startswith("tenant:default/user:user-1/agent:ar-1/session:sess-1/run:")


def test_capabilities_honesty_without_checkpointer():
    engine = ManagedLangGraphEngine(reasoner=_ScriptedReasoner([HarnessReasoningTurn(final_text="ok")]))
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
    engine = ManagedLangGraphEngine(reasoner=_ScriptedReasoner([HarnessReasoningTurn(final_text="ok")]))
    events = _run(engine, _start_request())

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
