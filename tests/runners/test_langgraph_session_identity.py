from __future__ import annotations

from types import SimpleNamespace

import pytest

from ksadk.runners._session_identity import LangGraphSessionIdentityMixin
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.sessions import bind_session_service
from ksadk.sessions.continuity import ConversationSessionCore
from ksadk.sessions.in_memory import InMemorySessionService


class _EmptyGraph:
    def invoke(self, *_args, **_kwargs):
        return {}

    def get_state(self, config):
        return SimpleNamespace(config=config, values={}, metadata=None, created_at=None)


class _LegacyGraph(_EmptyGraph):
    def get_state(self, config):
        return SimpleNamespace(
            config=config,
            values={"messages": ["historical"]},
            metadata={"step": 1},
            created_at="2026-01-01T00:00:00Z",
        )


def _runner(tmp_path, graph=None) -> LangGraphRunner:
    runner = LangGraphRunner(
        SimpleNamespace(entry_point="agent.py", agent_variable="graph"), str(tmp_path)
    )
    runner._agent = graph or _EmptyGraph()
    return runner


@pytest.mark.asyncio
async def test_new_identity_session_gets_an_opaque_langgraph_thread(tmp_path, monkeypatch) -> None:
    canonical = InMemorySessionService()
    await canonical.create_session("agent-1", "aeu-owner-a", "session-1")
    runner = _runner(tmp_path)
    monkeypatch.setattr(runner, "_invocation_identity_scope_ref", lambda: "aei-owner-a")

    with bind_session_service(canonical):
        config = await runner._get_session_config("session-1")
        binding = await ConversationSessionCore(canonical).get_binding_by_session_id(
            "session-1", "langgraph"
        )

    thread_id = config["configurable"]["thread_id"]
    assert thread_id.startswith("lgt_")
    assert thread_id != "session-1"
    assert binding["thread_id"] == thread_id
    assert binding["owner_scope_ref"] == "aei-owner-a"


@pytest.mark.asyncio
async def test_langgraph_thread_binding_rejects_another_identity(tmp_path, monkeypatch) -> None:
    canonical = InMemorySessionService()
    await canonical.create_session("agent-1", "aeu-owner-a", "session-1")
    runner = _runner(tmp_path)
    monkeypatch.setattr(runner, "_invocation_identity_scope_ref", lambda: "aei-owner-a")
    with bind_session_service(canonical):
        await runner._get_session_config("session-1")

    monkeypatch.setattr(runner, "_invocation_identity_scope_ref", lambda: "aei-owner-b")
    with (
        bind_session_service(canonical),
        pytest.raises(ValueError, match="different business identity"),
    ):
        await runner._get_session_config("session-1")


@pytest.mark.asyncio
async def test_authorized_legacy_langgraph_checkpoint_keeps_original_thread_id(
    tmp_path, monkeypatch
) -> None:
    canonical = InMemorySessionService()
    await canonical.create_session("agent-1", "legacy-user", "legacy-session")
    runner = _runner(tmp_path, _LegacyGraph())
    monkeypatch.setattr(runner, "_invocation_identity_scope_ref", lambda: "aei-owner-a")
    monkeypatch.setattr(runner, "_invocation_uses_legacy_session", lambda: True)

    with bind_session_service(canonical):
        config = await runner._get_session_config("legacy-session")

    assert config["configurable"]["thread_id"] == "legacy-session"


@pytest.mark.asyncio
async def test_new_identity_session_does_not_adopt_colliding_legacy_langgraph_state(
    tmp_path, monkeypatch
) -> None:
    canonical = InMemorySessionService()
    await canonical.create_session("agent-1", "aeu-owner-a", "session-1")
    runner = _runner(tmp_path, _LegacyGraph())
    monkeypatch.setattr(runner, "_invocation_identity_scope_ref", lambda: "aei-owner-a")
    monkeypatch.setattr(runner, "_invocation_uses_legacy_session", lambda: False)

    with bind_session_service(canonical):
        config = await runner._get_session_config("session-1")

    assert config["configurable"]["thread_id"].startswith("lgt_")
    assert config["configurable"]["thread_id"] != "session-1"


def test_identity_resume_cannot_override_the_bound_thread(tmp_path) -> None:
    runner = _runner(tmp_path)
    config = {"configurable": {"thread_id": "lgt-owned", "checkpoint_ns": "agent-1"}}

    with pytest.raises(ValueError, match="does not belong"):
        runner._apply_checkpoint_resume_config(
            config,
            session_id="session-1",
            checkpoint_ref={"thread_id": "lgt-foreign", "checkpoint_id": "cp-1"},
            enforce_bound_thread=True,
        )


def test_identity_thread_id_includes_agent_scope() -> None:
    """同租户同 session、不同 agent → 不同 thread。

    共享 checkpoint 库下，thread_id 是唯一的隔离键；缺 agent 维度会让两个
    agent 的同名 session 落到同一个 thread 互相覆盖 checkpoint。
    """
    a = LangGraphSessionIdentityMixin._identity_thread_id(
        "sess-1", "aei-owner-a", "ar-agent-aaa"
    )
    b = LangGraphSessionIdentityMixin._identity_thread_id(
        "sess-1", "aei-owner-a", "ar-agent-bbb"
    )
    assert a.startswith("lgt_") and b.startswith("lgt_")
    assert a != b
    # 同一 (identity, agent, session) 映射必须稳定。
    assert a == LangGraphSessionIdentityMixin._identity_thread_id(
        "sess-1", "aei-owner-a", "ar-agent-aaa"
    )


@pytest.mark.asyncio
async def test_langgraph_binding_does_not_persist_checkpoint_ns(
    tmp_path, monkeypatch
) -> None:
    """binding 表不得再保存 checkpoint_ns 字段（历史上曾存租户 scope 造成事故）。"""
    canonical = InMemorySessionService()
    await canonical.create_session("agent-1", "aeu-owner-a", "session-1")
    runner = _runner(tmp_path)
    monkeypatch.setattr(runner, "_invocation_identity_scope_ref", lambda: "aei-owner-a")

    with bind_session_service(canonical):
        await runner._get_session_config("session-1")
        binding = await ConversationSessionCore(canonical).get_binding_by_session_id(
            "session-1", "langgraph"
        )

    assert "checkpoint_ns" not in binding
    assert binding["thread_id"].startswith("lgt_")


def test_get_config_carries_explicit_recursion_limit(tmp_path, monkeypatch) -> None:
    """astream_events 用 LangChain 库默认 25 填充调用 config，Pregel.astream 的
    合并里这个显式 25 会按序覆盖图实例 with_config 设置的图级默认值——业务侧
    recursion_limit 在流式路径上因此失效（生产 GraphRecursionError 根因）。
    runner 的调用 config 必须显式携带非默认值，优先级：图自身 config >
    KSADK_LANGGRAPH_RECURSION_LIMIT > 兜底 50。"""
    monkeypatch.delenv("KSADK_LANGGRAPH_RECURSION_LIMIT", raising=False)

    # 图自身 with_config 的设置优先（业务侧显式设置）。
    runner = _runner(tmp_path, graph=SimpleNamespace(config={"recursion_limit": 50}))
    config = runner._get_config("session-1")
    assert config["recursion_limit"] == 50
    assert config["configurable"]["thread_id"] == "session-1"

    # 图未设置时读环境变量。
    monkeypatch.setenv("KSADK_LANGGRAPH_RECURSION_LIMIT", "80")
    runner = _runner(tmp_path, graph=SimpleNamespace(config={}))
    assert runner._get_config("session-1")["recursion_limit"] == 80

    # 都没有时兜底 50，不再回落 LangChain 默认 25；图未加载同样兜底。
    monkeypatch.delenv("KSADK_LANGGRAPH_RECURSION_LIMIT")
    runner = _runner(tmp_path, graph=SimpleNamespace(config={}))
    assert runner._get_config("session-1")["recursion_limit"] == 50
    runner = _runner(tmp_path, graph=SimpleNamespace())
    assert runner._get_config("session-1")["recursion_limit"] == 50


def test_get_config_recursion_limit_rejects_invalid_values(tmp_path, monkeypatch) -> None:
    """非法环境值与图级非法值都必须安全回落，绝不让 25 之类的坏值进入 config。"""
    monkeypatch.setenv("KSADK_LANGGRAPH_RECURSION_LIMIT", "not-a-number")
    runner = _runner(tmp_path, graph=SimpleNamespace(config={"recursion_limit": "bogus"}))
    assert runner._get_config("session-1")["recursion_limit"] == 50

    monkeypatch.setenv("KSADK_LANGGRAPH_RECURSION_LIMIT", "-3")
    assert runner._get_config("session-1")["recursion_limit"] == 50


def test_checkpoint_resume_config_preserves_recursion_limit(tmp_path) -> None:
    """checkpoint 恢复路径重建 configurable，但顶层 recursion_limit 必须保留。"""
    runner = _runner(tmp_path, graph=SimpleNamespace(config={"recursion_limit": 50}))
    config = runner._get_config("session-1")
    resumed = runner._apply_checkpoint_resume_config(
        config,
        session_id="session-1",
        checkpoint_ref={"checkpoint_id": "ckpt-1", "thread_id": "lgt-owned"},
    )
    assert resumed["recursion_limit"] == 50
    assert resumed["configurable"]["checkpoint_id"] == "ckpt-1"
