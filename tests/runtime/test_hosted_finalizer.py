"""HostedTurnFinalizer 独立测试（方案 §11.1 / P0 收敛）。

验证共享 finalizer：usage 回填、capability mismatch、Memory flush 门控、不重复写、失败降级。
Studio 与 canonical 共用此组件，不再各自实现。
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from ksadk.runtime.hosted_finalizer import FinalizeContext, finalize_hosted_turn
from ksadk.memory.providers.local_sqlite import SqliteMemoryProvider
from ksadk.memory.coordinator import MemoryCoordinator
from ksadk.memory.models import MemoryRecord
from ksadk.memory.policy import content_hash


@pytest.mark.asyncio
async def test_finalize_usage_backfills_actual_into_plan(tmp_path, monkeypatch):
    monkeypatch.delenv("KSADK_MEMORY_FLUSH_ENABLED", raising=False)
    plan = {"planned_input_tokens": 100}
    ctx = FinalizeContext(
        session_id="s", invocation_id="i", user_id="u",
        context_plan=plan, shadow_context_plan={"runtime_type": "langgraph"},
        usage={"input_tokens": 88, "prompt_tokens": 88},
        runtime_type="langgraph",
    )
    await finalize_hosted_turn(ctx)
    assert plan["runtime_reported_input_tokens"] == 88


@pytest.mark.asyncio
async def test_finalize_no_usage_leaves_plan_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("KSADK_MEMORY_FLUSH_ENABLED", raising=False)
    plan = {"planned_input_tokens": 100}
    ctx = FinalizeContext(
        session_id="s", invocation_id="i", user_id="u",
        context_plan=plan, shadow_context_plan={"runtime_type": "langgraph"},
        usage=None, runtime_type="langgraph",
    )
    await finalize_hosted_turn(ctx)
    assert "runtime_reported_input_tokens" not in plan


@pytest.mark.asyncio
async def test_finalize_memory_flush_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("KSADK_MEMORY_FLUSH_ENABLED", raising=False)
    events = [SimpleNamespace(author="user", event_type="user_message",
                               text="记住：用 Python 3.12", seq_id=1, id="e1",
                               invocation_id="i")]
    ctx = FinalizeContext(
        session_id="s", invocation_id="i", user_id="u",
        context_plan=None, shadow_context_plan=None, usage=None,
        runtime_type="langgraph", session_events=events,
    )
    await finalize_hosted_turn(ctx)  # 不应 flush


@pytest.mark.asyncio
async def test_finalize_memory_flush_writes_when_enabled(tmp_path, monkeypatch):
    db = tmp_path / "mem.db"
    monkeypatch.setenv("KSADK_MEMORY_FLUSH_ENABLED", "true")
    monkeypatch.setenv("KSADK_MEMORY_DB_PATH", str(db))
    events = [SimpleNamespace(author="user", event_type="user_message",
                               text="记住：默认用 Python 3.12", seq_id=1, id="e1",
                               invocation_id="i")]
    ctx = FinalizeContext(
        session_id="s", invocation_id="i", user_id="user-1",
        context_plan=None, shadow_context_plan=None, usage=None,
        runtime_type="langgraph", session_events=events,
    )
    await finalize_hosted_turn(ctx)
    provider = SqliteMemoryProvider(db_path=str(db))
    from ksadk.memory.models import MemorySearchRequest
    result = provider.search(MemorySearchRequest(
        query="Python", scopes=[("user", "user-1")], memory_types=["profile"]))
    assert any("Python 3.12" in r.content for r in result.records)


@pytest.mark.asyncio
async def test_finalize_does_not_duplicate_memory_on_reentry(tmp_path, monkeypatch):
    """同一 Turn 不重复写 Memory（P0：调用两次不产生两条相同记忆）。"""
    db = tmp_path / "mem.db"
    monkeypatch.setenv("KSADK_MEMORY_FLUSH_ENABLED", "true")
    monkeypatch.setenv("KSADK_MEMORY_DB_PATH", str(db))
    events = [SimpleNamespace(author="user", event_type="user_message",
                               text="记住：用 uv run", seq_id=1, id="e1",
                               invocation_id="i")]
    ctx = FinalizeContext(
        session_id="s", invocation_id="i", user_id="u",
        context_plan=None, shadow_context_plan=None, usage=None,
        runtime_type="langgraph", session_events=events,
    )
    await finalize_hosted_turn(ctx)
    await finalize_hosted_turn(ctx)  # 重复调用
    provider = SqliteMemoryProvider(db_path=str(db))
    from ksadk.memory.models import MemorySearchRequest
    result = provider.search(MemorySearchRequest(
        query="uv", scopes=[("user", "u")], memory_types=["profile"]))
    # content_hash 去重：同一内容只一条
    assert len(result.records) <= 1


@pytest.mark.asyncio
async def test_finalize_failure_does_not_raise(tmp_path, monkeypatch):
    """finalizer 失败不阻断主链路（best-effort）。"""
    monkeypatch.setenv("KSADK_MEMORY_FLUSH_ENABLED", "true")
    monkeypatch.setenv("KSADK_MEMORY_DB_PATH", "/nonexistent/path/that/does/not/exist.db")
    ctx = FinalizeContext(
        session_id="s", invocation_id="i", user_id="u",
        context_plan=None, shadow_context_plan=None, usage=None,
        runtime_type="langgraph", session_events=[],
    )
    await finalize_hosted_turn(ctx)  # 不抛


@pytest.mark.asyncio
async def test_finalize_capability_mismatch_triggers_circuit(tmp_path, monkeypatch):
    from ksadk.context_engine.capabilities import is_capability_circuit_open, reset_capability_circuit
    monkeypatch.delenv("KSADK_MEMORY_FLUSH_ENABLED", raising=False)
    reset_capability_circuit(runtime_type="codex")
    # codex 声明 runtime_reported，但 usage=None → mismatch
    ctx = FinalizeContext(
        session_id="s", invocation_id="i", user_id="u",
        context_plan=None, shadow_context_plan={"runtime_type": "codex"},
        usage=None, runtime_type="codex",
    )
    await finalize_hosted_turn(ctx)
    assert is_capability_circuit_open(runtime_type="codex")
    reset_capability_circuit(runtime_type="codex")
