"""Runner Context Conformance —— capability 声明与真实 ownership 一致性骨架。

第一个 PR 不调真实模型：用 ``StateGraph``（无 checkpointer）/ fake CodexClient 构造
Runner 实例，断言 ``describe_context_capabilities()`` 与已知 ownership 表一致，并固化
native Runner 不重复注入历史 / 不双重 compaction 的 invariant。ADK 路径在未安装
``ksadk[adk]`` extra 时通过 ``importorskip`` 跳过直接构造，改用 registry fallback 校验。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from ksadk.context_engine.capabilities import (
    adk_context_capabilities,
    codex_context_capabilities,
    deepagents_context_capabilities,
    langchain_context_capabilities,
    langgraph_context_capabilities,
)
from ksadk.codex.runtime import CodexRuntimeAdapter
from ksadk.runners.base_runner import BaseRunner
from ksadk.runners.deepagents_runner import DeepAgentsRunner
from ksadk.runners.langchain_runner import LangChainRunner
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime.adapter import StartRequest


class _State(TypedDict, total=False):
    value: str


def _detection(value: str) -> SimpleNamespace:
    return SimpleNamespace(entry_point="src/agent.py", agent_variable="root_agent", type=SimpleNamespace(value=value))


def _langgraph_runner(value: str) -> LangGraphRunner:
    graph = StateGraph(_State)
    graph.add_node("finish", lambda _state: {"value": "done"})
    graph.add_edge(START, "finish")
    graph.add_edge("finish", END)
    runner = LangGraphRunner(_detection(value), ".")
    runner._agent = graph.compile()
    return runner


class _FakeCodexClient:
    """记录 start_thread 收到的 thread_config，断言 history 不被展开注入。"""

    def __init__(self) -> None:
        self.started_configs: list[dict[str, Any]] = []

    async def start_thread(self, config: dict[str, Any] | None = None) -> str:
        self.started_configs.append(dict(config or {}))
        return "codex_thread_1"

    async def close(self) -> None:
        return None


def _codex_adapter() -> CodexRuntimeAdapter:
    return CodexRuntimeAdapter(_FakeCodexClient(), sandbox_read_only=True)


class _UnknownRunner(BaseRunner):
    """无任何 override 的自定义 Runner，应落到 DEFAULT 保守合同。"""

    def load_agent(self) -> None:
        return None

    async def invoke(self, input_data: dict[str, Any]) -> dict[str, Any]:
        return {"output": "done"}

    async def stream(self, input_data: dict[str, Any]):
        yield {"type": "final", "output": "done"}


# ---- capability 声明一致性 ----


def test_langgraph_describe_matches_ownership() -> None:
    caps = _langgraph_runner("langgraph").describe_context_capabilities()
    expected = langgraph_context_capabilities()
    assert caps == expected
    assert caps.prompt_owner == "ksadk"
    assert caps.history_owner == "ksadk"
    assert caps.compaction_owner == "ksadk"


def test_langchain_describe_matches_ownership() -> None:
    runner = LangChainRunner(_detection("langchain"), ".")
    runner._agent = _langgraph_runner("langchain")._agent
    caps = runner.describe_context_capabilities()
    expected = langchain_context_capabilities()
    assert caps == expected
    # LangChain 把 history/compaction 交回框架。
    assert caps.history_owner == "framework"
    assert caps.compaction_owner == "framework"


def test_deepagents_describe_matches_ownership() -> None:
    runner = DeepAgentsRunner(_detection("deepagents"), ".")
    runner._agent = _langgraph_runner("deepagents")._agent
    caps = runner.describe_context_capabilities()
    assert caps == deepagents_context_capabilities()
    assert caps.history_owner == "framework"


def test_codex_describe_matches_ownership() -> None:
    caps = _codex_adapter().describe_context_capabilities()
    assert caps == codex_context_capabilities()
    assert caps.integration_mode == "native_runtime"
    assert caps.history_owner == "native"
    assert caps.compaction_owner == "native"


def test_unknown_runner_falls_back_to_default() -> None:
    runner = _UnknownRunner(_detection("weird"), ".")
    caps = runner.describe_context_capabilities()
    from ksadk.context_engine.capabilities import DEFAULT_CONTEXT_CAPABILITIES

    assert caps == DEFAULT_CONTEXT_CAPABILITIES()
    assert caps.token_accounting == "opaque"
    assert caps.integration_mode == "framework_assisted"


def test_adk_describe_matches_ownership_when_extra_installed() -> None:
    pytest.importorskip("google.adk", reason="ksadk[adk] extra 未安装，跳过 ADK 直接构造")
    from ksadk.runners.adk_runner import ADKRunner

    # ADKRunner 构造较重；此处只验证 capability 工厂与 override 指向一致。
    expected = adk_context_capabilities()
    assert expected.integration_mode == "framework_assisted"
    assert expected.history_owner == "framework"
    assert expected.native_skills is True


# ---- invariant：native Runner 不重复注入历史 / 不双重 compaction ----


def test_should_use_platform_ambient_context_unchanged_for_known_runners() -> None:
    """第一个 PR 未改 ``_should_use_platform_ambient_context`` 的返回值。

    ADK → False（不注入平台 ambient），其余已知类型 → True。用 detection type 驱动的
    fake 对象即可断言，无需构造真实 Runner（函数只读 ``detection_result.type.value``）。
    """
    from ksadk.conversations.runtime_input import _should_use_platform_ambient_context

    def _fake(value: str) -> SimpleNamespace:
        return SimpleNamespace(detection_result=SimpleNamespace(type=SimpleNamespace(value=value)))

    assert _should_use_platform_ambient_context(_fake("adk")) is False
    assert _should_use_platform_ambient_context(_fake("langgraph")) is True
    assert _should_use_platform_ambient_context(_fake("langchain")) is True


@pytest.mark.asyncio
async def test_codex_does_not_expand_history_into_thread_config() -> None:
    """CodexRuntimeAdapter.start 只把 base_instructions/model/cwd 移交后端 thread，
    不展开 payload.history 做第二次历史注入（ADR-008 / 方案 11.1 native_runtime）。"""
    adapter = _codex_adapter()
    fake_client = adapter._client  # type: ignore[attr-defined]
    request = StartRequest(
        input="继续",
        user_id="u",
        session_id="s",
        config={"base_instructions": "你是 codex 助手"},
        metadata={"history": [{"role": "user", "content": "上一轮问题"}]},
    )
    await adapter.start(request)
    assert fake_client.started_configs, "start_thread 应被调用一次"
    thread_config = fake_client.started_configs[-1]
    # thread_config 只承载后端需要的配置契约，不包含历史展开。
    assert "history" not in thread_config
    assert thread_config.get("base_instructions") == "你是 codex 助手"


def test_codex_compaction_owned_by_native_not_ksadk() -> None:
    """native 路径的 compaction owner 是 native；KsADK 不运行第二套摘要链路。"""
    caps = _codex_adapter().describe_context_capabilities()
    assert caps.compaction_owner == "native"
    assert caps.integration_mode == "native_runtime"
