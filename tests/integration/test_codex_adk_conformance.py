"""真实 Codex/ADK Conformance（方案 §6.4 / §17.6 / PCM-RUNNER-002/003）。

扩展既有 conformance 骨架：真实 ``CodexRuntimeAdapter`` + fake client 跑完整 start→stream
路径，断言 native_runtime 不被 hosted 接管、history 不被二次注入、ownership 跨 resume 不变；
ADK 路径在 ``ksadk[adk]`` 未装时 importorskip。云端预发 E2E 见 ``test_cloud_managed_e2e``。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ksadk.context_engine.capabilities import (
    adk_context_capabilities,
    codex_context_capabilities,
    langgraph_context_capabilities,
    capabilities_for_runtime_type,
)
from ksadk.codex.runtime import CodexRuntimeAdapter
from ksadk.runtime.adapter import StartRequest


# ---- Codex native Conformance（真实 adapter + fake client）----


class _StreamingFakeCodexClient:
    """记录 start_thread + run_turn，模拟一个完成 turn 的 Codex 后端。"""

    def __init__(self) -> None:
        self.started_configs: list[dict[str, Any]] = []
        self.run_turn_inputs: list[dict[str, Any]] = []
        self._closed = False

    async def start_thread(self, config: dict[str, Any] | None = None) -> str:
        self.started_configs.append(dict(config or {}))
        return "codex_thread_c1"

    async def run_turn(self, thread_id: str, user_input: str, **kwargs):
        self.run_turn_inputs.append({"thread_id": thread_id, "user_input": user_input, "kwargs": kwargs})
        # 模拟 Codex 后端流式产出 + 完成
        yield {"type": "text", "text": "Codex-OK"}
        yield {"type": "completed", "usage": {"input_tokens": 20, "prompt_tokens": 20}}

    async def interrupt_active_turn(self, thread_id): return None
    async def resume_thread(self, thread_id, config): return None
    async def close(self): self._closed = True


@pytest.mark.asyncio
async def test_codex_conformance_native_ownership_across_lifecycle():
    """Codex native Conformance：start→stream 全程 native_runtime，base_instructions 进 thread，history 不二次注入。"""
    client = _StreamingFakeCodexClient()
    adapter = CodexRuntimeAdapter(client, sandbox_read_only=True)
    caps = adapter.describe_context_capabilities()
    assert caps == codex_context_capabilities()
    assert caps.integration_mode == "native_runtime"
    assert caps.prompt_owner == "native"

    request = StartRequest(
        input="继续",
        user_id="u", session_id="s",
        config={"base_instructions": "你是 codex 助手", "model": "codex-model"},
        metadata={"history": [{"role": "user", "content": "上一轮"}]},
    )
    handle = await adapter.start(request)
    # base_instructions 移交后端 thread；history 不展开进 thread_config（ADR-008）
    assert client.started_configs[-1].get("base_instructions") == "你是 codex 助手"
    assert "history" not in client.started_configs[-1]
    # stream 产出事件
    events = [e async for e in adapter.stream(handle)]
    assert events  # 有事件产出
    await adapter.close(handle)
    assert client._closed


@pytest.mark.asyncio
async def test_codex_conformance_hosted_pipeline_does_not_take_over_native():
    """PCM-RUNNER-003：Managed Codex 即使开 V2，hosted pipeline 不接管（assembled_input 恒 None）。"""
    from ksadk.conversations.runtime_preparation import build_run_input
    from ksadk.sessions.in_memory import InMemorySessionService

    service = InMemorySessionService()
    prepared = await build_run_input(
        agent_id="codex-a", user_id="u", session_id="s-codex",
        messages=[{"role": "user", "content": "x"}], model="m",
        instructions="q", agent_system="你是助手", agent_task="",
        prompt_integration_mode="ksadk_hosted", runtime_type="codex",
        session_service_provider=lambda: service,
    )
    assert prepared.context_plan is None
    assert prepared.assembled_input is None
    assert prepared.shadow_context_plan["integration_mode"] == "native_runtime"
    assert prepared.shadow_context_plan["prompt_owner"] == "native"
    assert prepared.shadow_context_plan["deployment_mode"] == "local"


# ---- ADK Conformance（framework_assisted）----

def test_adk_conformance_framework_assisted_ownership():
    """ADK Conformance：framework_assisted，history/compaction owner=framework，native_skills=True。"""
    pytest.importorskip("google.adk", reason="ksadk[adk] extra 未安装")
    caps = adk_context_capabilities()
    assert caps.integration_mode == "framework_assisted"
    assert caps.history_owner == "framework"
    assert caps.compaction_owner == "framework"
    assert caps.native_skills is True
    assert caps.memory_read is True
    # 与 capabilities_for_runtime_type 一致（registry 分派）
    assert capabilities_for_runtime_type("adk") == caps


def test_adk_conformance_hosted_pipeline_skips_framework_assisted():
    """ADK framework_assisted：prompt_owner=framework，hosted pipeline 不接管（仅 ksadk-owned 接管）。"""
    from ksadk.conversations.runtime_preparation import build_run_input
    from ksadk.sessions.in_memory import InMemorySessionService
    import asyncio

    service = InMemorySessionService()
    prepared = asyncio.run(build_run_input(
        agent_id="adk-a", user_id="u", session_id="s-adk",
        messages=[{"role": "user", "content": "x"}], model="m",
        instructions="q", agent_system="你是助手", agent_task="用 uv",
        prompt_integration_mode="ksadk_hosted", runtime_type="adk",
        session_service_provider=lambda: service,
    ))
    # adk prompt_owner=framework != ksadk → hosted pipeline 不接管
    assert prepared.context_plan is None
    assert prepared.assembled_input is None
    assert prepared.shadow_context_plan["prompt_owner"] == "framework"


# ---- capability mismatch 影响 native Conformance（熔断隔离）----

def test_circuit_breaker_isolates_by_runtime_type():
    """熔断 codex 不影响 langgraph（按 runtime_type 隔离，方案 §6.1）。"""
    from ksadk.context_engine.capabilities import (
        is_capability_circuit_open, mark_capability_mismatch, reset_capability_circuit,
    )
    reset_capability_circuit(runtime_type="codex")
    reset_capability_circuit(runtime_type="langgraph")
    mark_capability_mismatch(runtime_type="codex")
    assert is_capability_circuit_open(runtime_type="codex")
    assert not is_capability_circuit_open(runtime_type="langgraph")
    reset_capability_circuit(runtime_type="codex")


def test_unknown_runtime_default_conformance_opaque():
    """未知自定义 Runner 走保守 framework_assisted + opaque（方案 §6.1 / §9）。"""
    caps = capabilities_for_runtime_type("totally-unknown-framework")
    assert caps.integration_mode == "framework_assisted"
    assert caps.token_accounting == "opaque"
    assert caps.prompt_owner == "framework"
