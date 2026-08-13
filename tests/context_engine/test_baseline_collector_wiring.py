"""env-gated 基线采集挂载测试（评测方案 §10 阶段 1）。

验证：
- 默认关闭（不影响行为）；
- 开启后 canonical conversation execution 路径每 turn 采集一条记录；
- 记录只含 hash/计数/分类，不含 Prompt 正文（安全要求 §19）；
- 采集异常不影响主链路（不抛出）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ksadk.context_engine.baseline import (
    baseline_collection_enabled,
    get_baseline_collector,
    record_baseline_turn,
    reset_baseline_collector_for_tests,
)
from ksadk.context_engine.shadow_plan import build_shadow_context_plan_dict
from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.runtime import (
    BaseRuntime,
    CancelResult,
    RunHandle,
    RuntimeAdapter,
    RuntimeExecutor,
    RuntimeLaunchContext,
    RuntimeRegistry,
    StartRequest,
)
from ksadk.runtime.conversation_execution import iter_runtime_conversation_events
from ksadk.sessions.in_memory import InMemorySessionService


@pytest.fixture(autouse=True)
def _reset_baseline():
    reset_baseline_collector_for_tests()
    yield
    reset_baseline_collector_for_tests()


class _Runtime(BaseRuntime):
    runtime_type = "langgraph"

    def native_capabilities(self) -> dict[str, object]:
        return {"Framework": "langgraph"}


class _UsageAdapter(RuntimeAdapter):
    def __init__(self) -> None:
        super().__init__(_Runtime())
        self.requests: list[StartRequest] = []

    async def start(self, request: StartRequest) -> RunHandle:
        self.requests.append(request)
        return RunHandle(
            run_id=str(request.metadata["invocation_id"]),
            session_id=request.session_id,
            runtime_type="langgraph",
        )

    async def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        common = {
            "agent_id": "agent-1",
            "user_id": "user-1",
            "session_id": handle.session_id,
            "invocation_id": handle.run_id,
        }
        yield RuntimeEvent.create(
            EventType.RUN_STARTED, seq_id=1, payload={"status": "in_progress"}, **common
        )
        yield RuntimeEvent.create(
            EventType.TEXT_COMPLETED,
            seq_id=2,
            phase="final_answer",
            payload={"text": "answer"},
            **common,
        )
        yield RuntimeEvent.create(
            EventType.RUN_COMPLETED,
            seq_id=3,
            payload={"status": "completed", "duration_ms": 12},
            **common,
        )

    async def cancel(self, _handle: RunHandle) -> CancelResult:
        return CancelResult.NOT_RUNNING

    async def resume(self, handle, target, payload):
        return handle

    async def checkpoint(self, _handle):
        raise NotImplementedError

    async def close(self, handle: RunHandle) -> None:
        return None


def test_baseline_disabled_by_default_is_noop() -> None:
    assert not baseline_collection_enabled()
    assert get_baseline_collector() is None
    # record_baseline_turn 未启用时不抛异常、不记录。
    record_baseline_turn({"plan_id": "x"})
    assert get_baseline_collector() is None


def test_baseline_enabled_records_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KSADK_BASELINE_COLLECT", "true")
    collector = get_baseline_collector()
    assert collector is not None
    plan = build_shadow_context_plan_dict(
        instructions="你是助手", user_input="hi", runtime_type="langgraph"
    )
    record_baseline_turn(
        plan,
        session_id="s",
        invocation_id="i",
        model="m",
        usage={"input_tokens": 100, "output_tokens": 20},
        turn_latency_ms=42,
    )
    assert len(collector.records) == 1
    record = collector.records[0]
    assert record.runner_type == "langgraph"
    assert record.accounting_accuracy == "estimated"
    assert record.capability_hash.startswith("sha256:")
    assert record.runtime_reported_input_tokens == 100
    assert record.turn_latency_ms == 42


def test_baseline_record_does_not_contain_prompt_plaintext(monkeypatch: pytest.MonkeyPatch) -> None:
    """安全要求 §19：基线只记 hash/计数/分类，不含 Prompt 正文。"""
    monkeypatch.setenv("KSADK_BASELINE_COLLECT", "1")
    collector = get_baseline_collector()
    assert collector is not None
    secret = "SECRET-INSTRUCTION-DO-NOT-LEAK"
    plan = build_shadow_context_plan_dict(
        instructions=secret, user_input="hi", runtime_type="langgraph"
    )
    record_baseline_turn(plan, session_id="s", invocation_id="i")
    assert secret not in repr(collector.records[0])
    # 只记 content_hash，不记 content。
    assert collector.records[0].prompt_content_hash.startswith("sha256:")


@pytest.mark.asyncio
async def test_canonical_path_collects_baseline_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """canonical conversation execution 主链路在 env 开启时自动采集一条 turn 记录。"""
    monkeypatch.setenv("KSADK_BASELINE_COLLECT", "true")
    service = InMemorySessionService()
    adapter = _UsageAdapter()
    registry = RuntimeRegistry()
    registry.register("langgraph", lambda _context: adapter)
    executor = RuntimeExecutor(registry)
    context = RuntimeLaunchContext(runtime_type="langgraph", project_dir=".")

    events = [
        event
        async for event in iter_runtime_conversation_events(
            executor=executor,
            launch_context=context,
            agent_id="agent-1",
            user_id="user-1",
            messages=[{"role": "user", "content": "帮我做个总结"}],
            session_id=None,
            model=None,
            instructions="你是助手",
            session_service_provider=lambda: service,
        )
    ]
    assert [event.event_type for event in events] == [
        EventType.RUN_STARTED,
        EventType.TEXT_COMPLETED,
        EventType.RUN_COMPLETED,
    ]
    collector = get_baseline_collector()
    assert collector is not None and len(collector.records) == 1
    record = collector.records[0]
    assert record.runner_type == "langgraph"
    assert record.accounting_accuracy == "estimated"
    assert record.session_id == adapter.requests[0].session_id
    # canonical 路径产真实 shadow plan（非 opaque）。
    assert record.planned_input_tokens > 0


def test_record_baseline_turn_swallows_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    """采集异常绝不影响主链路：即使 plan 损坏也不抛出。"""
    monkeypatch.setenv("KSADK_BASELINE_COLLECT", "on")

    class _BadMapping:
        def get(self, key, default=None):
            raise RuntimeError("boom")

    # 传入坏对象也不抛异常。
    record_baseline_turn(_BadMapping(), session_id="s", invocation_id="i")  # type: ignore[arg-type]
