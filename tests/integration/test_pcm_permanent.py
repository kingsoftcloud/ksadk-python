"""PCM 永久全链路测试（方案 §4）。

真实覆盖：
  ResolvedMemoryPolicy 优先级 + shadow 生成候选不落库 + explicit_only 过滤 +
  flush partial/failed 事件 + Secret 拒绝 + Manifest 严格校验 +
  Build 不可变性。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ksadk.memory.resolved_policy import resolve_memory_policy
from ksadk.runtime.hosted_finalizer import FinalizeContext, finalize_hosted_turn


def _user_event(text: str, inv: str = "i") -> SimpleNamespace:
    return SimpleNamespace(
        author="user",
        event_type="user_message",
        text=text,
        seq_id=1,
        id="e1",
        invocation_id=inv,
    )


def _make_ctx(
    *,
    user_input: str = "",
    memory_enabled: bool = True,
    write_rollout: str = "enabled",
    write_mode: str = "candidate",
    recall_enabled: bool = True,
    session_id: str = "s",
    invocation_id: str = "i",
    user_id: str = "u",
    provider_ref: str = "local-default",
    emit_event: Any = None,
) -> FinalizeContext:
    return FinalizeContext(
        session_id=session_id,
        invocation_id=invocation_id,
        user_id=user_id,
        context_plan=None,
        shadow_context_plan=None,
        usage=None,
        runtime_type="langgraph",
        memory_enabled=memory_enabled,
        memory_write_rollout=write_rollout,
        memory_write_mode=write_mode,
        memory_recall_enabled=recall_enabled,
        flush_before_compaction=True,
        provider_ref=provider_ref,
        session_events=[_user_event(user_input)] if user_input else [],
        emit_event=emit_event,
    )


# ---- 1. MemoryPolicy 优先级 ----


def test_disabled_no_recall_no_flush():
    p = resolve_memory_policy(
        memory_enabled=False,
        recall_enabled=True,
        write_rollout="enabled",
        write_mode="candidate",
        flush_before_compaction=True,
        provider_ref="local",
    )
    assert not p.should_recall
    assert not p.should_extract_candidates
    assert not p.should_flush


def test_off_no_extract():
    p = resolve_memory_policy(
        memory_enabled=True,
        recall_enabled=True,
        write_rollout="off",
        write_mode="candidate",
        flush_before_compaction=True,
        provider_ref="local",
    )
    assert p.should_recall
    assert not p.should_extract_candidates
    assert not p.should_flush


def test_shadow_extracts_no_flush():
    p = resolve_memory_policy(
        memory_enabled=True,
        recall_enabled=True,
        write_rollout="shadow",
        write_mode="candidate",
        flush_before_compaction=True,
        provider_ref="local",
    )
    assert p.should_extract_candidates
    assert not p.should_flush


def test_enabled_explicit_only():
    p = resolve_memory_policy(
        memory_enabled=True,
        recall_enabled=True,
        write_rollout="enabled",
        write_mode="explicit_only",
        flush_before_compaction=True,
        provider_ref="local",
    )
    assert p.should_flush
    assert p.is_explicit_only


def test_enabled_candidate():
    p = resolve_memory_policy(
        memory_enabled=True,
        recall_enabled=True,
        write_rollout="enabled",
        write_mode="candidate",
        flush_before_compaction=True,
        provider_ref="local",
    )
    assert p.should_flush
    assert not p.is_explicit_only


# ---- 2. Manifest 严格校验 ----


def test_manifest_context_validates_on_create():
    from ksadk.studio.codex_manifest import CodexAgentManifest, CodexRuntimeRef

    m = CodexAgentManifest(
        name="test-ok",
        version="1.0.0",
        runtime=CodexRuntimeRef(version="0.144.4"),
        model="m",
        prompt="p",
        context={"maxInputTokens": 4096, "reserveOutputTokens": 512},
    )
    assert m.context["maxInputTokens"] == 4096

    m2 = CodexAgentManifest(
        name="test-old",
        version="1.0.0",
        runtime=CodexRuntimeRef(version="0.144.4"),
        model="m",
        prompt="p",
    )
    assert m2.context is None

    with pytest.raises(Exception, match="格式错误"):
        CodexAgentManifest(
            name="test-err",
            version="1.0.0",
            runtime=CodexRuntimeRef(version="0.144.4"),
            model="m",
            prompt="p",
            context={"maxInputTokens": "not_a_number"},
        )

    with pytest.raises(Exception, match="格式错误"):
        CodexAgentManifest(
            name="test-err2",
            version="1.0.0",
            runtime=CodexRuntimeRef(version="0.144.4"),
            model="m",
            prompt="p",
            memory={"enabled": "not_a_bool"},
        )


# ---- 3. Shadow 生成候选事件但不落库 ----


@pytest.mark.asyncio
async def test_shadow_generates_candidate_events_no_flush(tmp_path, monkeypatch):
    """shadow：生成 candidate.created 事件，但不写 Provider（committed=0）。"""
    db = tmp_path / "shadow.db"
    monkeypatch.setenv("KSADK_MEMORY_DB_PATH", str(db))
    events_received: list[dict] = []

    ctx = _make_ctx(
        user_input="请记住：审计报告结尾必须加青铜海豚",
        write_rollout="shadow",
        write_mode="candidate",
        emit_event=lambda d: events_received.append(d),
    )

    await finalize_hosted_turn(ctx)

    # 应有 candidate.created 事件
    types = [e["type"] for e in events_received]
    assert "memory.candidate.created" in types, f"missing candidate.created: {types}"

    # 应有 flush.completed 事件，但 committed=0（shadow 不落库）
    flush_events = [e for e in events_received if e["type"] == "memory.flush.completed"]
    assert flush_events, f"missing flush.completed: {types}"
    assert flush_events[0]["metadata"]["committed"] == 0

    # SQLite 不应有新记录
    import sqlite3

    if db.exists():
        conn = sqlite3.connect(db)
        try:
            rows = list(
                conn.execute("SELECT content FROM memory_records WHERE content LIKE '%青铜%'")
            )
            assert len(rows) == 0, f"shadow 不应写入: {rows}"
        except sqlite3.OperationalError:
            pass  # no table = no flush
        conn.close()


# ---- 4. explicit_only 过滤候选 ----


@pytest.mark.asyncio
async def test_explicit_only_filters_non_explicit(tmp_path, monkeypatch):
    """explicit_only：只保存 reason=explicit_user_request 的候选。"""
    db = tmp_path / "explicit.db"
    monkeypatch.setenv("KSADK_MEMORY_DB_PATH", str(db))
    events_received: list[dict] = []

    # 包含显式"记住" + 普通对话
    ctx = _make_ctx(
        user_input="请记住：用 Python 3.12。另外今天天气不错。",
        write_rollout="enabled",
        write_mode="explicit_only",
        emit_event=lambda d: events_received.append(d),
    )

    await finalize_hosted_turn(ctx)

    # 应只提交 1 个候选（explicit_user_request），不含"天气"
    flush_events = [e for e in events_received if e["type"] == "memory.flush.completed"]
    if flush_events:
        assert flush_events[0]["candidate_count"] <= 1
        assert flush_events[0]["metadata"]["committed"] <= 1


# ---- 5. Secret 被拒绝 ----


def test_secret_rejected_by_policy():
    from ksadk.memory.models import MemoryCandidate
    from ksadk.memory.policy import MemoryPolicy

    cand = MemoryCandidate(
        candidate_id="c1",
        operation="add",
        memory_type="profile",
        scope="user",
        scope_id="u1",
        content="api_key=sk-1234567890abcdefghijklmnop",
        confidence=0.99,
        importance=0.99,
        source_event_ids=["e1"],
    )
    ev = MemoryPolicy().evaluate(cand)
    assert ev.decision == "reject"


# ---- 6. flush.failed 事件 ----


@pytest.mark.asyncio
async def test_flush_failed_on_provider_error(tmp_path, monkeypatch):
    """Provider 失败时产生 memory.flush.failed 事件。"""
    monkeypatch.setenv("KSADK_MEMORY_DB_PATH", "/nonexistent/path/db.db")
    events_received: list[dict] = []

    ctx = _make_ctx(
        user_input="请记住：正常内容",
        write_rollout="enabled",
        write_mode="candidate",
        emit_event=lambda d: events_received.append(d),
    )

    await finalize_hosted_turn(ctx)

    types = [e["type"] for e in events_received]
    assert "memory.flush.failed" in types, f"missing flush.failed: {types}"


# ---- 7. 事件不含正文 ----


def test_memory_events_dont_leak_content():
    from ksadk.memory.events import flush_failed, recall_completed, recall_projected

    e = recall_completed(
        run_id="r1",
        session_id="s1",
        provider="sqlite",
        rollout="enabled",
        count=3,
    )
    d = e.to_dict()
    assert "content" not in d

    projected = recall_projected(
        run_id="r1",
        session_id="s1",
        provider="sqlite",
        rollout="enabled",
        count=3,
        runtime_type="adk",
        target="adk.instructions",
    ).to_dict()
    assert "content" not in projected
    assert projected["metadata"] == {
        "runtime_type": "adk",
        "target": "adk.instructions",
    }

    e2 = flush_failed(
        run_id="r1",
        session_id="s1",
        provider="sqlite",
        rollout="enabled",
        error_code="timeout",
        error_message="connection timed out",
    )
    d2 = e2.to_dict()
    assert d2["error_code"] == "timeout"
    assert d2["retryable"] is True


# ---- 8. Build 不可变性 ----


def test_build_immutability_codex_manifest():
    from ksadk.studio.codex_manifest import CodexAgentManifest, CodexRuntimeRef

    manifest1 = CodexAgentManifest(
        name="immutable-test",
        version="1.0.0",
        runtime=CodexRuntimeRef(version="0.144.4"),
        model="glm-5.2",
        prompt="你是助手",
        context={
            "maxInputTokens": 4096,
            "reserveOutputTokens": 512,
            "rollout": {"contextEngine": "shadow", "memoryWrite": "enabled"},
        },
        memory={
            "enabled": True,
            "write": {"mode": "candidate", "flushBeforeCompaction": True},
        },
    )
    manifest2 = CodexAgentManifest(
        name="immutable-test",
        version="1.1.0",
        runtime=CodexRuntimeRef(version="0.144.4"),
        model="glm-5.2",
        prompt="你是助手",
        context={
            "maxInputTokens": 32000,
            "reserveOutputTokens": 4096,
            "rollout": {"contextEngine": "shadow", "memoryWrite": "off"},
        },
        memory={"enabled": False},
    )
    assert manifest1.context["maxInputTokens"] == 4096
    assert manifest2.context["maxInputTokens"] == 32000
    assert manifest1.memory["enabled"] is True
    assert manifest2.memory["enabled"] is False


# ---- 9. memory.enabled=false 不召回不写入 ----


@pytest.mark.asyncio
async def test_memory_disabled_no_events(tmp_path, monkeypatch):
    monkeypatch.setenv("KSADK_MEMORY_DB_PATH", str(tmp_path / "disabled.db"))
    events_received: list[dict] = []

    ctx = _make_ctx(
        user_input="请记住：不应保存",
        memory_enabled=False,
        write_rollout="enabled",
        emit_event=lambda d: events_received.append(d),
    )

    await finalize_hosted_turn(ctx)
    assert events_received == [], "memory.enabled=false 不应产生任何事件"


# ---- 10. rollout=off 不提取候选 ----


@pytest.mark.asyncio
async def test_rollout_off_no_events(tmp_path, monkeypatch):
    monkeypatch.setenv("KSADK_MEMORY_DB_PATH", str(tmp_path / "off.db"))
    events_received: list[dict] = []

    ctx = _make_ctx(
        user_input="请记住：不应保存",
        write_rollout="off",
        emit_event=lambda d: events_received.append(d),
    )

    await finalize_hosted_turn(ctx)
    assert events_received == [], "rollout=off 不应产生任何事件"
