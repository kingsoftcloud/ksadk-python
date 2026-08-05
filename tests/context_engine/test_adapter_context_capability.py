"""RuntimeAdapter Context capability 合同测试（shadow 接线修正）。

验证 capability 主合同位于平台边界 ``RuntimeAdapter``/``BaseRuntime``，且各 adapter
经合同取到的 ownership 与其真实行为一致（Codex native、LangGraph framework_assisted），
不依赖 Runner 类名猜测。canonical conversation execution 路径用 ``runtime_type`` 取得的
capability 必须与 adapter 实例声明一致（capability hash 相同），保证 build_run_input
阶段（尚未拿到 adapter）与执行阶段的 ownership 可比对。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from ksadk.context_engine.capabilities import (
    capabilities_for_runtime_type,
    capability_hash,
    codex_context_capabilities,
    langgraph_context_capabilities,
)
from ksadk.codex.runtime import CodexRuntimeAdapter
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter


class _State(TypedDict, total=False):
    value: str


def _langgraph_runner() -> LangGraphRunner:
    graph = StateGraph(_State)
    graph.add_node("finish", lambda _state: {"value": "done"})
    graph.add_edge(START, "finish")
    graph.add_edge("finish", END)
    runner = LangGraphRunner(
        SimpleNamespace(entry_point="src/agent.py", agent_variable="root_agent", type=SimpleNamespace(value="langgraph")),
        ".",
    )
    runner._agent = graph.compile()
    return runner


class _FakeCodexClient:
    async def start_thread(self, config: dict[str, Any] | None = None) -> str:
        return "codex_thread_1"

    async def close(self) -> None:
        return None


def test_runner_runtime_adapter_aggregates_inner_runner_capability() -> None:
    """``RunnerRuntimeAdapter`` 经 ``_RunnerAsBaseRuntime`` 汇总内部 Runner capability。"""
    runner = _langgraph_runner()
    adapter = RunnerRuntimeAdapter(runner, runtime_type="langgraph")
    caps = adapter.describe_context_capabilities()
    assert caps == langgraph_context_capabilities()
    assert caps.token_accounting == "estimated"
    assert caps.history_owner == "ksadk"


def test_codex_runtime_adapter_declares_native_ownership() -> None:
    adapter = CodexRuntimeAdapter(_FakeCodexClient(), sandbox_read_only=True)
    caps = adapter.describe_context_capabilities()
    assert caps == codex_context_capabilities()
    assert caps.integration_mode == "native_runtime"
    assert caps.history_owner == "native"
    assert caps.compaction_owner == "native"


def test_base_runtime_default_uses_runtime_type_dispatch() -> None:
    """``BaseRuntime`` 默认按 runtime_type 分派，不依赖类名。"""
    from ksadk.runtime.runner_adapter import _RunnerAsBaseRuntime

    # 一个没有 describe_context_capabilities 的假 runner，仍能通过 runtime_type 分派。
    fake_runner = SimpleNamespace()
    base = _RunnerAsBaseRuntime(fake_runner, runtime_type="codex")
    caps = base.describe_context_capabilities()
    assert caps == codex_context_capabilities()

    base_unknown = _RunnerAsBaseRuntime(fake_runner, runtime_type="weird-framework")
    assert base_unknown.describe_context_capabilities().token_accounting == "opaque"


def test_adapter_capability_matches_runtime_type_dispatch() -> None:
    """canonical 路径用 runtime_type 取的 capability 必须与 adapter 实例声明一致。

    build_run_input 阶段（runtime_type）与执行阶段（adapter 实例）的 ownership 可比对；
    capability hash 相同 → 无 capability mismatch。
    """
    runner = _langgraph_runner()
    adapter = RunnerRuntimeAdapter(runner, runtime_type="langgraph")
    adapter_caps = adapter.describe_context_capabilities()
    dispatch_caps = capabilities_for_runtime_type("langgraph")
    assert adapter_caps == dispatch_caps
    assert capability_hash(adapter_caps) == capability_hash(dispatch_caps)

    codex_adapter = CodexRuntimeAdapter(_FakeCodexClient(), sandbox_read_only=True)
    codex_dispatch = capabilities_for_runtime_type("codex")
    assert codex_adapter.describe_context_capabilities() == codex_dispatch
    assert capability_hash(codex_adapter.describe_context_capabilities()) == capability_hash(codex_dispatch)
