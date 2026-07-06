"""L4 semantic 熔断单测:连续失败超阈值后跳过 LLM 直接走 extractive。

Codex 指出:summarize_compaction 捕获异常返回 extractive,外层看成功,
governance compact failure 不触发。因此 semantic 熔断需独立计数(本模块级)。
"""

from __future__ import annotations

import asyncio
import pytest

from ksadk.conversations import semantic_summary as ss
from ksadk.conversations.semantic_summary import (
    CompactionSummaryResult,
    summarize_compaction,
)


class _FailingClient:
    """模拟 LLM 调用总是失败的 client。"""

    @property
    def is_available(self) -> bool:
        return True

    async def summarize(self, *, model, messages, timeout_ms):
        raise RuntimeError("simulated LLM timeout")


class _SuccessClient:
    """模拟 LLM 调用成功的 client(返回带 <summary> 块)。"""

    @property
    def is_available(self) -> bool:
        return True

    async def summarize(self, *, model, messages, timeout_ms):
        return "<analysis>x</analysis><summary>ok summary</summary>", {"input_tokens": 10}


def _setup_client(monkeypatch, client):
    monkeypatch.setattr(ss, "resolve_summary_model_client", lambda: client)
    # 让 semantic_compaction_disabled 返回 False(确保走 semantic 路径)。
    monkeypatch.setattr(ss, "semantic_compaction_disabled", lambda: False)


@pytest.fixture(autouse=True)
def _reset_semantic_failures():
    """每个测试前重置 semantic 失败计数,避免测试间污染。"""
    ss._reset_semantic_failures()
    yield
    ss._reset_semantic_failures()


@pytest.mark.asyncio
async def test_semantic_failure_increments_counter(monkeypatch):
    _setup_client(monkeypatch, _FailingClient())
    # 确保不被熔断拦截(阈值设高)。
    monkeypatch.setenv("KSADK_MAX_CONSECUTIVE_SEMANTIC_FAILURES", "100")

    result = await summarize_compaction(
        groups_to_compact=[],
        previous_summary="",
        pinned_state={},
        model_metadata=None,
        model="test-model",
    )

    assert result.summary_strategy == "extractive"  # 失败回退 extractive
    assert result.fallback_reason == "simulated LLM timeout"
    assert ss._semantic_summary_failures == 1


@pytest.mark.asyncio
async def test_circuit_opens_after_threshold_failures(monkeypatch):
    _setup_client(monkeypatch, _FailingClient())
    monkeypatch.setenv("KSADK_MAX_CONSECUTIVE_SEMANTIC_FAILURES", "2")

    # 第一次失败:计数=1,未熔断,走 try。
    r1 = await summarize_compaction(
        groups_to_compact=[], previous_summary="", pinned_state={},
        model_metadata=None, model="m",
    )
    assert r1.fallback_reason == "simulated LLM timeout"
    assert ss._semantic_summary_failures == 1

    # 第二次失败:计数=2,达到阈值。
    r2 = await summarize_compaction(
        groups_to_compact=[], previous_summary="", pinned_state={},
        model_metadata=None, model="m",
    )
    assert r2.fallback_reason == "simulated LLM timeout"
    assert ss._semantic_summary_failures == 2

    # 第三次:熔断已开,直接走 extractive 不调 LLM,fallback_reason 变成 circuit_open。
    r3 = await summarize_compaction(
        groups_to_compact=[], previous_summary="", pinned_state={},
        model_metadata=None, model="m",
    )
    assert r3.summary_strategy == "extractive"
    assert r3.fallback_reason == "semantic_circuit_open"
    # 熔断后不再调 LLM,计数不增加。
    assert ss._semantic_summary_failures == 2


@pytest.mark.asyncio
async def test_success_resets_counter(monkeypatch):
    _setup_client(monkeypatch, _FailingClient())
    monkeypatch.setenv("KSADK_MAX_CONSECUTIVE_SEMANTIC_FAILURES", "5")

    # 一次失败。
    await summarize_compaction(
        groups_to_compact=[], previous_summary="", pinned_state={},
        model_metadata=None, model="m",
    )
    assert ss._semantic_summary_failures == 1

    # 切回成功 client。
    _setup_client(monkeypatch, _SuccessClient())
    r = await summarize_compaction(
        groups_to_compact=[], previous_summary="", pinned_state={},
        model_metadata=None, model="m",
    )
    assert r.summary_strategy == "semantic"
    assert ss._semantic_summary_failures == 0  # 成功清零


@pytest.mark.asyncio
async def test_circuit_disabled_when_threshold_zero(monkeypatch):
    _setup_client(monkeypatch, _FailingClient())
    monkeypatch.setenv("KSADK_MAX_CONSECUTIVE_SEMANTIC_FAILURES", "0")

    # 阈值 0 = 禁用熔断,失败多次仍走 try(每次都调 LLM 失败)。
    for _ in range(5):
        r = await summarize_compaction(
            groups_to_compact=[], previous_summary="", pinned_state={},
            model_metadata=None, model="m",
        )
        assert r.fallback_reason == "simulated LLM timeout"  # 不是 circuit_open
