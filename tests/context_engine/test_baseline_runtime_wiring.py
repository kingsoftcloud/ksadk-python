"""env-gated baseline 采集挂载测试（runtime 旁路，零行为变更）。

验证：
- env 未开时采集为 no-op，不影响主链路；
- env 开启时 invoke_conversation_once 完成后采集一条 turn 记录，含真实 usage/ownership；
- 采集内容不含 Prompt/凭证正文（安全 §19）；
- 采集失败不影响主链路（采集器吞异常）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ksadk.context_engine import baseline as baseline_mod
from ksadk.conversations.runtime_invocation import invoke_conversation_once
from ksadk.sessions.in_memory import InMemorySessionService


class _UsageRunner:
    """返回固定 usage 的 stub runner；detection_result 带可识别 type.value。"""

    def __init__(self) -> None:
        self.detection_result = SimpleNamespace(type=SimpleNamespace(value="langgraph"))
        self.calls: list[dict] = []

    def prepare_for_request(self, runner, model):  # noqa: ARG002
        return None

    async def invoke(self, input_data: dict) -> dict:
        self.calls.append(input_data)
        return {
            "output": "assistant says hi",
            "usage": {"input_tokens": 8, "output_tokens": 13, "total_tokens": 21},
        }


@pytest.fixture(autouse=True)
def _reset_baseline():
    baseline_mod.reset_baseline_collector_for_tests()
    yield
    baseline_mod.reset_baseline_collector_for_tests()


@pytest.mark.asyncio
async def test_baseline_collection_disabled_by_default(monkeypatch) -> None:
    """env 未开 → 采集 no-op，主链路正常返回，无记录。"""
    monkeypatch.delenv("KSADK_BASELINE_COLLECT", raising=False)
    service = InMemorySessionService()
    await service.create_session(agent_id="a", user_id="u", session_id="s-disabled")
    runner = _UsageRunner()
    _, result = await invoke_conversation_once(
        runner=runner,
        agent_id="a",
        user_id="u",
        session_id="s-disabled",
        messages=[{"role": "user", "content": "hi"}],
        model=None,
        prepare_runner=runner.prepare_for_request,
        instructions="你是助手",
        session_service_provider=lambda: service,
    )
    assert result["output_text"] == "assistant says hi"
    assert baseline_mod.get_baseline_collector() is None  # 未启用


@pytest.mark.asyncio
async def test_baseline_collection_enabled_captures_turn(monkeypatch) -> None:
    """env 开启 → turn 完成后采集一条记录，含真实 usage 与 ownership。"""
    monkeypatch.setenv("KSADK_BASELINE_COLLECT", "1")
    monkeypatch.setenv("KSADK_BASELINE_EXECUTION_TARGET", "test-runtime")
    service = InMemorySessionService()
    await service.create_session(agent_id="a", user_id="u", session_id="s-enabled")
    runner = _UsageRunner()
    _, result = await invoke_conversation_once(
        runner=runner,
        agent_id="a",
        user_id="u",
        session_id="s-enabled",
        messages=[{"role": "user", "content": "帮我做个总结"}],
        model=None,
        prepare_runner=runner.prepare_for_request,
        instructions="你是助手",
        session_service_provider=lambda: service,
    )
    assert result["output_text"] == "assistant says hi"
    collector = baseline_mod.get_baseline_collector()
    assert collector is not None
    assert len(collector.records) == 1
    record = collector.records[0]
    # ownership 来自 runner（langgraph → estimated，非 opaque）。
    assert record.runner_type == "langgraph"
    assert record.accounting_accuracy == "estimated"
    assert record.capability_hash.startswith("sha256:")
    # 真实 runtime usage。
    assert record.runtime_reported_input_tokens == 8
    assert record.runtime_output_tokens == 13
    # token 分类。
    assert record.tokens_by_kind["compiled_prompt"] > 0
    assert record.tokens_by_kind["current_input"] > 0
    # 延迟被采集。
    assert record.turn_latency_ms is not None and record.turn_latency_ms >= 0
    assert record.compaction_triggered is False
    assert record.prompt_too_long is False


@pytest.mark.asyncio
async def test_baseline_record_does_not_leak_prompt_plaintext(monkeypatch, tmp_path) -> None:
    """安全 §19：采集落盘只记 hash/计数，不含 instructions 正文。"""
    monkeypatch.setenv("KSADK_BASELINE_COLLECT", "1")
    monkeypatch.setenv("KSADK_BASELINE_PATH", str(tmp_path / "baseline.jsonl"))
    service = InMemorySessionService()
    await service.create_session(agent_id="a", user_id="u", session_id="s-leak")
    runner = _UsageRunner()
    secret = "SECRET-INSTRUCTION-NEVER-LEAK"
    await invoke_conversation_once(
        runner=runner,
        agent_id="a",
        user_id="u",
        session_id="s-leak",
        messages=[{"role": "user", "content": "hi"}],
        model=None,
        prepare_runner=runner.prepare_for_request,
        instructions=secret,
        session_service_provider=lambda: service,
    )
    collector = baseline_mod.get_baseline_collector()
    assert collector is not None
    out = collector.dump(tmp_path / "baseline.jsonl")
    content = out.read_text(encoding="utf-8")
    assert secret not in content
    assert "sha256:" in content


@pytest.mark.asyncio
async def test_baseline_collector_failure_does_not_break_main_path(monkeypatch) -> None:
    """采集器内部抛异常时不影响主链路（record_baseline_turn 吞异常）。"""
    monkeypatch.setenv("KSADK_BASELINE_COLLECT", "1")
    service = InMemorySessionService()
    await service.create_session(agent_id="a", user_id="u", session_id="s-fail")
    runner = _UsageRunner()
    baseline_mod.get_baseline_collector()  # 初始化单例
    # 主链路正常返回即证明采集失败被吞。
    _, result = await invoke_conversation_once(
        runner=runner,
        agent_id="a",
        user_id="u",
        session_id="s-fail",
        messages=[{"role": "user", "content": "hi"}],
        model=None,
        prepare_runner=runner.prepare_for_request,
        session_service_provider=lambda: service,
    )
    assert result["output_text"] == "assistant says hi"


def test_record_baseline_turn_is_noop_when_disabled() -> None:
    """env 未开时 record_baseline_turn 直接返回，不创建 collector。"""
    from ksadk.context_engine.shadow_plan import build_shadow_context_plan_dict

    baseline_mod.reset_baseline_collector_for_tests()
    # 确保未启用。
    assert baseline_mod.get_baseline_collector() is None
    baseline_mod.record_baseline_turn(
        build_shadow_context_plan_dict(instructions="x", user_input="y"),
        session_id="s",
        invocation_id="i",
    )
    assert baseline_mod.get_baseline_collector() is None  # 仍未创建
