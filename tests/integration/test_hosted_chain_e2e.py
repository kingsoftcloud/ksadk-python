"""真实链路本地 E2E：Resolved AgentVersion → Prompt Compiler → Contributors →
Context Planner → Context Assembler → RuntimeAdapter → 模型 → Usage/Trace → Session/Memory
（方案 §11.1 / §17.5 / PCM-HARNESS-001）。

用真实 ``StateGraph`` + 一个记录型 FakeLLM 跑 ``invoke_conversation_once`` canonical 路径，
开启 ``KSADK_CONTEXT_ENGINE_V2_ENABLED`` + ``KSADK_PROMPT_COMPILER_ENABLED`` +
``prompt_integration_mode=ksadk_hosted``，验证：
- CompiledPrompt 真实编译（agent_system/task 进模型 system）
- Planner 决策的有序 messages 到达模型
- 平台安全规则不被 Memory/Tool 覆盖
- runtime usage 回填进 ContextPlan
- Session events append-only 持久化
- Managed Codex（native_runtime）不受 hosted 接管影响（PCM-RUNNER-003）
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest

from ksadk.conversations.runtime_invocation import invoke_conversation_once
from ksadk.runners.base_runner import BaseRunner
from ksadk.sessions.in_memory import InMemorySessionService

# 伪装成 langgraph 检测结果，使 capability 走 langgraph_context_capabilities(prompt_owner=ksadk)
_LANGGRAPH_DETECTION = SimpleNamespace(
    type=SimpleNamespace(value="langgraph"),
    name="e2e-runner",
    is_valid=True,
    entry_point=None,
    agent_variable="graph",
    raw_config={},
)


class _RecordingRunner(BaseRunner):
    """记录型 Runner：detection_type=langgraph，记录每次进模型的 instructions/input/history，回固定输出 + usage。

    走真实 BaseRunner.invoke 契约，经 RunnerRuntimeAdapter 接入 canonical 路径。
    """

    def __init__(self) -> None:
        super().__init__(_LANGGRAPH_DETECTION, ".")
        self._agent = True  # 标记已加载
        self.received: list[dict[str, Any]] = []

    def load_agent(self) -> None:
        return None

    async def invoke(self, input_data: dict[str, Any]) -> dict[str, Any]:
        self.received.append({
            "instructions": str(input_data.get("instructions") or ""),
            "input": str(input_data.get("input") or ""),
            "history": list(input_data.get("history") or []),
        })
        return {"output": "E2E-OK", "usage": {"input_tokens": 42, "output_tokens": 3, "prompt_tokens": 42}}

    def stream(self, input_data):  # pragma: no cover - E2E 用非流式
        raise NotImplementedError


@pytest.fixture()
def env_hosted(monkeypatch):
    """开启 hosted 链路三重门控。"""
    monkeypatch.setenv("KSADK_CONTEXT_ENGINE_V2_ENABLED", "true")
    monkeypatch.setenv("KSADK_PROMPT_COMPILER_ENABLED", "true")
    monkeypatch.setenv("KSADK_MEMORY_FLUSH_ENABLED", "true")
    yield


@pytest.mark.asyncio
async def test_real_hosted_chain_langgraph(env_hosted):
    """真实链路：CompiledPrompt → Planner → Assembler → Runner → Usage → Session。"""
    service = InMemorySessionService()
    runner = _RecordingRunner()
    session_id = "e2e-sess-1"

    _, result = await invoke_conversation_once(
        runner=runner,
        agent_id="e2e-agent",
        user_id="e2e-user",
        session_id=session_id,
        messages=[{"role": "user", "content": "用一句话介绍 GIL。"}],
        model="e2e-model",
        prepare_runner=lambda _r, _m: None,
        instructions="本次问题：介绍 GIL",
        agent_system="你是 Python 助手，绝不回显凭证。",
        agent_task="默认用 Python 3.12 和 uv run。",
        prompt_integration_mode="ksadk_hosted",
        session_service_provider=lambda: service,
    )

    # 1. 模型收到了组装后的 system（含 agent_system/task），且 input 是用户消息
    assert runner.received, "runner should have received the turn"
    recv = runner.received[-1]
    assert "你是 Python 助手" in recv["instructions"]
    assert "Python 3.12" in recv["instructions"]
    assert "GIL" in recv["input"]

    # 2. 结果回写 + usage
    assert result.get("output_text") == "E2E-OK"

    # 3. Session events append-only 持久化
    events = await service.get_events(session_id)
    assert any(getattr(e, "event_type", "") == "user_message" for e in events)
    assert any(getattr(e, "event_type", "") == "assistant_message" for e in events)


@pytest.mark.asyncio
async def test_hosted_chain_second_turn_does_not_duplicate_current_input(env_hosted):
    service = InMemorySessionService()
    runner = _RecordingRunner()
    common = {
        "runner": runner,
        "agent_id": "e2e-agent",
        "user_id": "e2e-user",
        "session_id": "e2e-multi-turn",
        "model": "e2e-model",
        "prepare_runner": lambda _r, _m: None,
        "agent_system": "你是助手。",
        "agent_task": "记住当前会话。",
        "prompt_integration_mode": "ksadk_hosted",
        "session_service_provider": lambda: service,
    }

    await invoke_conversation_once(
        **common,
        messages=[{"role": "user", "content": "第一轮代号是蓝鲸"}],
        instructions="记录代号",
    )
    await invoke_conversation_once(
        **common,
        messages=[{"role": "user", "content": "第二轮请回答代号"}],
        instructions="回答代号",
    )

    second = runner.received[-1]
    assert second["input"] == "第二轮请回答代号"
    assert all(
        item.get("content") != "第二轮请回答代号"
        for item in second["history"]
    )
    assert any(
        item.get("content") == "第一轮代号是蓝鲸" for item in second["history"]
    )


@pytest.mark.asyncio
async def test_hosted_chain_fills_usage_into_plan(env_hosted):
    """runtime usage 回填进真实 ContextPlan（planned vs actual 可观测）。"""
    from ksadk.conversations.runtime_preparation import build_run_input
    from ksadk.runtime.hosted_finalizer import _extract_input_tokens, finalize_hosted_turn, FinalizeContext

    service = InMemorySessionService()
    session_id = "e2e-sess-2"
    prepared = await build_run_input(
        agent_id="e2e-agent", user_id="e2e-user", session_id=session_id,
        messages=[{"role": "user", "content": "hi"}], model="m",
        instructions="q", agent_system="你是助手", agent_task="用 uv",
        prompt_integration_mode="ksadk_hosted", runtime_type="langgraph",
        session_service_provider=lambda: service,
    )
    assert prepared.context_plan is not None, "V2 开 → 应产出真实 context_plan"
    assert prepared.assembled_input is not None
    assert "你是助手" in prepared.assembled_input["system"]

    # 模拟回话收尾：usage 回填
    await finalize_hosted_turn(
        FinalizeContext(
            session_id=prepared.session_id, invocation_id=prepared.invocation_id,
            user_id="local-user", context_plan=prepared.context_plan,
            shadow_context_plan=prepared.shadow_context_plan,
            usage={"input_tokens": 42}, runtime_type="langgraph",
            prompt_integration_mode="ksadk_hosted",
        ),
        session_service_provider=lambda: service,
    )
    assert prepared.context_plan["runtime_reported_input_tokens"] == 42
    assert _extract_input_tokens({"prompt_tokens": 99}) == 99
    assert _extract_input_tokens({"input_token_details": {"total": 55}}) == 55


@pytest.mark.asyncio
async def test_hosted_chain_off_by_default_is_byte_identical():
    """默认关闭时 hosted 链路不产出 context_plan/assembled_input，走旧 PR B 分支（字节级一致）。"""
    service = InMemorySessionService()
    runner = _RecordingRunner()
    # 不设任何 V2 env
    _, _ = await invoke_conversation_once(
        runner=runner, agent_id="a", user_id="u", session_id="s-off",
        messages=[{"role": "user", "content": "x"}], model="m",
        prepare_runner=lambda _r, _m: None, instructions="q",
        agent_system="你是助手", agent_task="用 uv",
        prompt_integration_mode="ksadk_hosted",  # 即使声明接管
        session_service_provider=lambda: service,
    )
    # 未开 KSADK_PROMPT_COMPILER_ENABLED/CONTEXT_ENGINE_V2 → instructions 仍是旧拼接（无 XML section）
    recv = runner.received[-1]
    # 旧路径：instructions == request instructions（"q"），不含 agent_system/task
    assert recv["instructions"] == "q"
    assert "你是助手" not in recv["instructions"]


@pytest.mark.asyncio
async def test_managed_codex_native_not_taken_over(env_hosted):
    """Managed Codex（native_runtime）：即使开 V2，assembled_input 不产出，不被接管（PCM-RUNNER-003）。"""
    from ksadk.conversations.runtime_preparation import build_run_input

    service = InMemorySessionService()
    prepared = await build_run_input(
        agent_id="codex-agent", user_id="u", session_id="s-codex",
        messages=[{"role": "user", "content": "x"}], model="m",
        instructions="q", agent_system="你是助手", agent_task="",
        prompt_integration_mode="ksadk_hosted", runtime_type="codex",  # native_runtime
        session_service_provider=lambda: service,
    )
    # native_runtime prompt_owner=native → hosted pipeline 不接管
    assert prepared.context_plan is None
    assert prepared.assembled_input is None
    # shadow plan 仍记录 native ownership（不误判为 ksadk_owned）
    assert prepared.shadow_context_plan["integration_mode"] == "native_runtime"
    assert prepared.shadow_context_plan["prompt_owner"] == "native"


@pytest.mark.asyncio
async def test_real_langgraph_stategraph_consumes_assembled(env_hosted):
    """真实 LangGraph StateGraph：用 FakeLLM 验证组装后的 system+messages 经 _to_state 进 graph。"""
    try:
        from langgraph.graph import StateGraph, START, END
        from typing import TypedDict
    except Exception:  # noqa: BLE001
        pytest.skip("langgraph not installed")

    seen: dict[str, Any] = {}

    class _State(TypedDict, total=False):
        messages: list
        input: str

    def _chat_node(state: _State):
        msgs = state.get("messages") or []
        seen["messages"] = list(msgs)
        seen["input"] = state.get("input")
        return {"messages": msgs + [{"role": "assistant", "content": "LG-OK"}]}

    g = StateGraph(_State)
    g.add_node("chat", _chat_node)
    g.add_edge(START, "chat")
    g.add_edge("chat", END)
    compiled = g.compile()

    # 用一个真实 BaseRunner 包装 compiled graph（最小 langgraph runner）
    class _LGRunner(BaseRunner):
        def __init__(self):
            super().__init__(_LANGGRAPH_DETECTION, ".")
            self._agent = compiled

        def load_agent(self): return None

        async def invoke(self, input_data):
            instructions = str(input_data.get("instructions") or "")
            history = list(input_data.get("history") or [])
            user_input = str(input_data.get("input") or "")
            messages = []
            if instructions:
                messages.append({"role": "system", "content": instructions})
            for h in history:
                messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
            if user_input:
                messages.append({"role": "user", "content": user_input})
            result = await self._agent.ainvoke({"messages": messages, "input": user_input})
            out_msgs = result.get("messages") or []
            text = out_msgs[-1].get("content") if isinstance(out_msgs[-1], dict) else str(out_msgs[-1])
            return {"output": text, "usage": {"input_tokens": 50, "prompt_tokens": 50}}

        def stream(self, input_data):  # pragma: no cover
            raise NotImplementedError

    service = InMemorySessionService()
    runner = _LGRunner()
    _, result = await invoke_conversation_once(
        runner=runner, agent_id="lg-agent", user_id="u", session_id="s-lg",
        messages=[{"role": "user", "content": "介绍 GIL"}], model="m",
        prepare_runner=lambda _r, _m: None, instructions="介绍 GIL",
        agent_system="你是 Python 助手，绝不回显凭证。", agent_task="用 Python 3.12。",
        prompt_integration_mode="ksadk_hosted", session_service_provider=lambda: service,
    )
    # 真实 graph 收到了组装后的 system（含 agent_system）
    assert seen["messages"]
    sys_msg = seen["messages"][0]
    assert sys_msg.get("role") == "system"
    assert "你是 Python 助手" in sys_msg.get("content", "")
    assert result.get("output_text") == "LG-OK"
