"""Shadow 基线验收测试（第二步）。

验证 P0 shadow 接线的架构正确性与"零行为变更"，不跑真实模型：

| 验收项 | 通过标准 |
|---|---|
| 旧行为回归 | runner payload 不含 shadow 字段；shadow plan 不影响 history/instructions |
| 单 Turn 单 Plan | 一个 Turn 只生成一份初始 ContextPlan；fallback preprocessing 复用 prepared_turn |
| Ownership | canonical 路径 runtime_type 取的 capability 与 adapter 实例一致（capability hash 相同） |
| Token 分类 | tokens_by_kind 各分项之和 == planned_input_tokens，无重复无漏算 |
| Trace 安全 | span 属性只记 hash/长度/分类，不含 Prompt/凭证正文 |
| Native 隔离 | Codex 不被注入第二份历史，不执行 KsADK compaction |
"""

from __future__ import annotations

import random
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from ksadk.context_engine.capabilities import (
    capabilities_for_runtime_type,
    capability_hash,
)
from ksadk.context_engine.shadow_plan import build_shadow_context_plan_dict
from ksadk.conversations.runtime_input import _build_runner_request_payload
from ksadk.conversations.runtime_observability import (
    _set_context_plan_attributes,
    _set_prompt_cache_attributes,
)
from ksadk.conversations.runtime_payloads import PreparedConversationTurn
from ksadk.runtime.adapter import StartRequest
from ksadk.runtime_context import PlatformInvocationContext

# ---------------------------------------------------------------------------
# 旧行为回归：shadow 字段不泄漏进 runner payload / 不影响 history/instructions
# ---------------------------------------------------------------------------


def _make_prepared(
    *, instructions: str = "你是助手", user_input: str = "hi"
) -> PreparedConversationTurn:
    plan = build_shadow_context_plan_dict(instructions=instructions, user_input=user_input)
    return PreparedConversationTurn(
        session_id="s",
        invocation_id="i",
        user_input=user_input,
        user_display_input=user_input,
        history=[{"role": "user", "content": "上一轮"}],
        input_content=[],
        input_messages=[],
        user_parts=[],
        attachments=[],
        attachment_results=[],
        current_attachments=[],
        current_attachment_results=[],
        has_current_files=False,
        instructions=instructions,
        shadow_context_plan=plan,
    )


def test_runner_payload_does_not_carry_shadow_fields() -> None:
    """shadow_context_plan 只用于观测，绝不能进入 runner 实际消费的 payload（零行为变更）。"""
    prepared = _make_prepared()
    ctx = PlatformInvocationContext(
        agent_id="a",
        user_id="u",
        account_id="",
        session_id="s",
        history=list(prepared.history),
        input_content=[],
        input_messages=[],
        input_parts=[],
        attachments=[],
        attachment_results=[],
        current_attachments=[],
        current_attachment_results=[],
        has_current_files=False,
        runner_type="langgraph",
    )
    payload = _build_runner_request_payload(
        prepared=prepared, model="m", runtime_context=ctx, runner=None
    )
    assert "shadow_context_plan" not in payload
    assert "shadow_context_plan" not in payload.get("request_metadata", {})
    # instructions / history 仍是原始值，未被 shadow 改写。
    assert payload["instructions"] == "你是助手"
    assert payload["history"] == [{"role": "user", "content": "上一轮"}]


def test_shadow_plan_does_not_mutate_prepared_fields() -> None:
    prepared = _make_prepared(instructions="原始指令", user_input="原始输入")
    plan = build_shadow_context_plan_dict(
        instructions=prepared.instructions,
        user_input=prepared.user_input,
        history=prepared.history,
    )
    # 构造 shadow plan 是只读旁路，prepared 字段不变。
    assert prepared.instructions == "原始指令"
    assert prepared.user_input == "原始输入"
    assert plan["planned_input_tokens"] > 0


# ---------------------------------------------------------------------------
# 单 Turn 单 Plan：fallback preprocessing 复用 prepared_turn（asdict roundtrip）
# ---------------------------------------------------------------------------


def test_prepared_turn_roundtrip_preserves_single_shadow_plan() -> None:
    """canonical 路径 asdict(prepared) → StartRequest metadata → preprocessing 复用，
    只有一份 shadow plan，不被重新规划或丢失。"""
    from dataclasses import asdict

    prepared = _make_prepared()
    raw = asdict(prepared)
    restored = PreparedConversationTurn(**dict(raw))
    assert restored.shadow_context_plan is not None
    assert restored.shadow_context_plan == prepared.shadow_context_plan
    # 同一 plan_id（未重新生成）。
    assert restored.shadow_context_plan["plan_id"] == prepared.shadow_context_plan["plan_id"]


# ---------------------------------------------------------------------------
# Ownership：canonical runtime_type 与 adapter 实例声明一致
# ---------------------------------------------------------------------------


def _langgraph_runner():
    class _State(TypedDict, total=False):
        value: str

    graph = StateGraph(_State)
    graph.add_node("finish", lambda _state: {"value": "done"})
    graph.add_edge(START, "finish")
    graph.add_edge("finish", END)
    from ksadk.runners.langgraph_runner import LangGraphRunner

    runner = LangGraphRunner(
        SimpleNamespace(
            entry_point="src/agent.py",
            agent_variable="root_agent",
            type=SimpleNamespace(value="langgraph"),
        ),
        ".",
    )
    runner._agent = graph.compile()
    return runner


def test_canonical_runtime_type_capability_matches_adapter_instance() -> None:
    """build_run_input 阶段（runtime_type）与 adapter 实例声明的 capability hash 相同 → 无 mismatch。"""
    from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter

    runner = _langgraph_runner()
    adapter = RunnerRuntimeAdapter(runner, runtime_type="langgraph")
    dispatch_caps = capabilities_for_runtime_type("langgraph")
    assert adapter.describe_context_capabilities() == dispatch_caps
    assert capability_hash(adapter.describe_context_capabilities()) == capability_hash(
        dispatch_caps
    )


# ---------------------------------------------------------------------------
# Token 分类：tokens_by_kind 各分项之和 == planned_input_tokens（property-based）
# ---------------------------------------------------------------------------


def _random_turn(rng: random.Random) -> dict[str, str]:
    roles = ("user", "assistant")
    contents = (
        "帮我写一个 Python 函数",
        "好的，这是实现：def f(): pass",
        "继续优化性能",
        "已优化，复杂度降到 O(n)",
        "解释这段逻辑",
        "这段逻辑的作用是去重",
    )
    return {"role": rng.choice(roles), "content": rng.choice(contents)}


@pytest.mark.parametrize("seed", range(20))
def test_tokens_by_kind_sum_equals_planned(seed: int) -> None:
    """随机 round 序列下，tokens_by_kind 各分项之和 == planned_input_tokens，无重复无漏算。"""
    rng = random.Random(seed)
    history = [_random_turn(rng) for _ in range(rng.randint(0, 8))]
    instructions = rng.choice(["你是助手", "你是代码助手，用中文", ""])
    user_input = rng.choice(["继续", "帮我做个总结", ""])
    metadata = {}
    if rng.random() > 0.5:
        metadata["memory_context"] = {"formatted_text": "记住用 Python 3.12"}
    if rng.random() > 0.5:
        metadata["kb_context"] = {"formatted_text": "知识库片段"}
    plan = build_shadow_context_plan_dict(
        instructions=instructions,
        history=history,
        user_input=user_input,
        request_metadata=metadata,
    )
    tokens_by_kind = plan["tokens_by_kind"]
    assert sum(tokens_by_kind.values()) == plan["planned_input_tokens"]
    # 每个分项非负。
    assert all(v >= 0 for v in tokens_by_kind.values())
    # compiled_prompt 与 current_input 不被重复计入 history_round。
    from ksadk.conversations.model_context import estimate_text_tokens

    assert tokens_by_kind["current_input"] == estimate_text_tokens(user_input)
    assert tokens_by_kind["compiled_prompt"] == estimate_text_tokens(instructions)


# ---------------------------------------------------------------------------
# Trace 安全：span 只记 hash/长度/分类，不含 Prompt/凭证正文
# ---------------------------------------------------------------------------


class _RecordingSpan:
    def __init__(self) -> None:
        self.attrs: dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.attrs[key] = value


def test_trace_attributes_only_record_hashes_and_counts_not_content() -> None:
    """span 属性只记 plan_id/hash/计数/分类，绝不含 instructions 正文或 memory 正文。"""
    prepared = _make_prepared(instructions="SECRET-INSTRUCTION-DO-NOT-LEAK")
    plan = prepared.shadow_context_plan
    span = _RecordingSpan()
    _set_context_plan_attributes(span, plan)
    _set_prompt_cache_attributes(
        span, session_id="s", plan=plan, usage={"cache_read_input_tokens": 10}
    )

    dumped = repr(span.attrs)
    assert "SECRET-INSTRUCTION-DO-NOT-LEAK" not in dumped
    # 只记 prompt_content_hash（sha256 前缀），不记 content。
    assert span.attrs.get("prompt.content_hash", "").startswith("sha256:")
    # tokens_by_kind 是计数字典，不含正文。
    assert "compiled_prompt" in str(span.attrs.get("context.tokens_by_kind", ""))


# ---------------------------------------------------------------------------
# Native 隔离：Codex 不被注入第二份历史，不执行 KsADK compaction
# ---------------------------------------------------------------------------


class _FakeCodexClient:
    def __init__(self) -> None:
        self.started_configs: list[dict[str, Any]] = []

    async def start_thread(self, config: dict[str, Any] | None = None) -> str:
        self.started_configs.append(dict(config or {}))
        return "codex_thread_1"

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_codex_does_not_get_second_history_injection_or_ksadk_compaction() -> None:
    """CodexRuntimeAdapter.start 只把 base_instructions/model/cwd 移交后端 thread，
    不展开 payload.history 做第二份历史注入（ADR-008）。其 capability 声明 compaction
    owner=native，KsADK 不对 native 路径运行第二套 compaction。"""
    from ksadk.codex.runtime import CodexRuntimeAdapter

    adapter = CodexRuntimeAdapter(_FakeCodexClient(), sandbox_read_only=True)
    caps = adapter.describe_context_capabilities()
    assert caps.integration_mode == "native_runtime"
    assert caps.compaction_owner == "native"  # KsADK 不越权 compaction

    request = StartRequest(
        input="继续",
        user_id="u",
        session_id="s",
        agent_id="a",
        config={"base_instructions": "你是 codex 助手"},
        metadata={"history": [{"role": "user", "content": "上一轮问题"}]},
    )
    await adapter.start(request)
    thread_config = adapter._client.started_configs[-1]  # type: ignore[attr-defined]
    assert "history" not in thread_config  # 不做第二份历史注入
    assert thread_config.get("base_instructions") == "你是 codex 助手"


# ---------------------------------------------------------------------------
# Canonical 主链路产 Plan（runtime_type 非 opaque）
# ---------------------------------------------------------------------------


def test_canonical_launch_context_runtime_type_yields_non_opaque_plan() -> None:
    """Studio/Server/AG-UI 主链路经 launch_context.runtime_type 取得非 opaque 的 plan。"""
    for runtime_type, expected_accuracy in (
        ("langgraph", "estimated"),
        ("codex", "runtime_reported"),
        ("adk", "runtime_reported"),
    ):
        plan = build_shadow_context_plan_dict(
            instructions="你是助手", user_input="hi", runtime_type=runtime_type
        )
        assert plan["accounting_accuracy"] == expected_accuracy, runtime_type
        assert plan["runtime_type"] == runtime_type
        assert plan["capability_hash"].startswith("sha256:")
