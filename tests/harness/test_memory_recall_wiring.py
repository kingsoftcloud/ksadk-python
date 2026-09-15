"""长任务方案 P1-3：引擎 Memory 召回接线（memory.recalled）测试。"""

from __future__ import annotations

import asyncio

from ksadk.harness.context_engine import HarnessContextEngine
from ksadk.harness.engine.langgraph import ManagedLangGraphEngine
from ksadk.harness.events import EventType
from ksadk.harness.memory_runtime import HarnessMemoryRuntime, MemoryWriteRequest
from ksadk.harness.reasoner import HarnessReasoner, HarnessReasoningTurn
from ksadk.harness.spec import HarnessSpec, MemoryPolicy, ModelBinding, PromptSpec
from ksadk.runtime import StartRequest


def _spec(memory_enabled: bool) -> HarnessSpec:
    return HarnessSpec(
        agent_revision_ref="agent-revision://proj-1@2",
        model=ModelBinding(profile_ref="model-profile://kimi-k3@1.0.0"),
        prompt=PromptSpec(instructions="你是财务分析助手。"),
        memory_policy=MemoryPolicy(
            enabled=memory_enabled,
            scopes=("session", "agent", "user", "org") if memory_enabled else ("session",),
        ),
    )


class _ScriptedReasoner(HarnessReasoner):
    def __init__(self) -> None:
        self.seen_messages: list[dict] = []

    async def complete(self, *, model, prompt, messages, tools):
        self.seen_messages = [dict(m) for m in messages]
        return HarnessReasoningTurn(final_text="ok")


def _seed_memory(runtime: HarnessMemoryRuntime, spec: HarnessSpec) -> None:
    runtime.write(
        MemoryWriteRequest(
            operation="add",
            content="用户偏好：报表统一用中文并按月汇总",
            scope="user",
            scope_id="user:user-1",
            source="user_explicit",
        ),
        spec,
        run_id="run_seed",
    )


def _request() -> StartRequest:
    return StartRequest(
        agent_id="ar-1",
        user_id="user-1",
        session_id="sess-1",
        input="请按我的偏好出报表",
        runtime_type="managed-langgraph",
    )


def test_memory_recalled_event_and_injection():
    memory = HarnessMemoryRuntime.local_sqlite()
    spec = _spec(memory_enabled=True)
    _seed_memory(memory, spec)
    reasoner = _ScriptedReasoner()
    engine = ManagedLangGraphEngine(
        reasoner=reasoner,
        context_engine=HarnessContextEngine(),
        memory_runtime=memory,
    )

    async def drive():
        compiled = await engine.compile(spec)
        handle = await engine.start(_request(), compiled)
        return [event async for event in engine.stream(handle)]

    events = asyncio.run(drive())
    recalled = [e for e in events if e.event_type == EventType.MEMORY_RECALLED]
    assert recalled, "注入 MemoryRuntime + policy 启用时必须发 memory.recalled"
    payload = recalled[0].payload
    assert payload["query"].startswith("请按我的偏好")
    assert payload["items"], "items 必须携带 memory_id/score/injected"
    item = payload["items"][0]
    assert item["memory_id"].startswith("mem_")
    assert 0.0 <= item["score"] <= 1.0
    assert item["injected"] is True
    # 召回内容真正进入模型输入（system 段）。
    assert any("用户偏好" in str(m.get("content")) for m in reasoner.seen_messages)


def test_no_recall_when_policy_disabled():
    memory = HarnessMemoryRuntime.local_sqlite()
    spec = _spec(memory_enabled=False)
    reasoner = _ScriptedReasoner()
    engine = ManagedLangGraphEngine(
        reasoner=reasoner,
        context_engine=HarnessContextEngine(),
        memory_runtime=memory,
    )

    async def drive():
        compiled = await engine.compile(spec)
        handle = await engine.start(_request(), compiled)
        return [event async for event in engine.stream(handle)]

    events = asyncio.run(drive())
    assert not [e for e in events if e.event_type == EventType.MEMORY_RECALLED]
    assert not any("长期记忆" in str(m.get("content")) for m in reasoner.seen_messages)
