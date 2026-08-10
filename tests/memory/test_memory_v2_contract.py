"""Memory v2 契约测试 —— Provider / Policy / Coordinator（方案 §17.4）。

所有 Provider 共用同一套测试（方案 §17.4）。这里对 SQLite Provider 跑完整契约，Policy 与
Coordinator 单测覆盖敏感信息拒绝、阈值、冲突、scope 隔离、删除与错误隔离。
"""

from __future__ import annotations

import pytest

from ksadk.memory.coordinator import (
    MemoryCoordinator,
    build_search_request,
    recall_to_context_item,
)
from ksadk.memory.models import (
    MemoryCandidate,
    MemoryDeleteRequest,
    MemoryRecord,
    MemorySearchRequest,
)
from ksadk.memory.policy import MemoryPolicy, detect_sensitive_labels
from ksadk.memory.providers.local_sqlite import SqliteMemoryProvider


def _record(
    memory_id="m1",
    *,
    scope="user",
    scope_id="u1",
    content="偏好中文",
    version=1,
    status="active",
    memory_type="fact",
    content_hash=None,
):
    from ksadk.memory.policy import content_hash as _ch

    return MemoryRecord(
        memory_id=memory_id,
        tenant_id="t",
        workspace_id="w",
        scope=scope,
        scope_id=scope_id,
        memory_type=memory_type,
        content=content,
        summary=content[:50],
        status=status,
        confidence=0.9,
        importance=0.8,
        valid_from="",
        valid_to="",
        expires_at="",
        source_session_id="s",
        source_event_ids=[],
        source_seq_range=None,
        content_hash=content_hash or _ch(content),
        version=version,
    )


@pytest.fixture()
def provider():
    p = SqliteMemoryProvider()
    yield p
    p.close()


# ---- Provider 契约（方案 §17.4）----


def test_provider_upsert_get_search(provider):
    p = provider
    p.upsert(_record(), expected_version=None)
    got = p.get("m1")
    assert got is not None and got.content == "偏好中文"
    res = p.search(
        MemorySearchRequest(query="中文", scopes=[("user", "u1")], memory_types=["fact"])
    )
    assert res.status == "ok" and len(res.records) == 1


def test_sqlite_provider_recalls_chinese_natural_language_query(provider):
    provider.upsert(
        _record(
            memory_id="m-natural-cjk",
            scope_id="u-natural",
            content="我的部署偏好：所有部署命令必须先 dry-run",
        ),
        expected_version=None,
    )

    result = provider.search(
        MemorySearchRequest(
            query="我之前要求的部署偏好是什么",
            scopes=[("user", "u-natural")],
            memory_types=["fact"],
        )
    )

    assert [item.memory_id for item in result.records] == ["m-natural-cjk"]


def test_provider_scope_isolation(provider):
    provider.upsert(_record(scope_id="u1"), expected_version=None)
    res = provider.search(
        MemorySearchRequest(query="中文", scopes=[("user", "u2")], memory_types=["fact"])
    )
    assert res.status == "ok" and res.records == []  # 跨 scope 不召回


def test_provider_expected_version_conflict(provider):
    provider.upsert(_record(version=1), expected_version=None)
    with pytest.raises(Exception):
        provider.upsert(_record(version=1), expected_version=999)


def test_provider_superseded_not_active(provider):
    provider.upsert(_record(memory_id="m1", status="superseded"), expected_version=None)
    provider.upsert(
        _record(memory_id="m2", content="偏好英文", status="active"), expected_version=None
    )
    res = provider.search(
        MemorySearchRequest(query="偏好", scopes=[("user", "u1")], memory_types=["fact"])
    )
    active_contents = [r.content for r in res.records]
    assert "偏好英文" in active_contents
    assert all(r.status == "active" for r in res.records)


def test_provider_hard_delete(provider):
    provider.upsert(_record(), expected_version=None)
    r = provider.delete(MemoryDeleteRequest(memory_id="m1", scope="user", scope_id="u1", hard=True))
    assert r.deleted
    assert provider.get("m1") is None


def test_provider_soft_delete_marks_deleted(provider):
    provider.upsert(_record(), expected_version=None)
    provider.delete(MemoryDeleteRequest(memory_id="m1", scope="user", scope_id="u1", hard=False))
    got = provider.get("m1")
    assert got is not None and got.status == "deleted"
    res = provider.search(
        MemorySearchRequest(query="中文", scopes=[("user", "u1")], memory_types=["fact"])
    )
    assert res.records == []  # deleted 不召回


def test_provider_search_no_scope_returns_empty(provider):
    res = provider.search(MemorySearchRequest(query="x", scopes=[], memory_types=["fact"]))
    assert res.status == "ok" and res.records == []


def test_provider_content_hash_dedupe(provider):
    from ksadk.memory.policy import content_hash

    ch = content_hash("同一事实")
    provider.upsert(
        _record(memory_id="a", content="同一事实", content_hash=ch), expected_version=None
    )
    provider.upsert(
        _record(memory_id="b", content="同一事实", content_hash=ch, version=2),
        expected_version=None,
    )
    res = provider.search(
        MemorySearchRequest(query="同一", scopes=[("user", "u1")], memory_types=["fact"])
    )
    assert len(res.records) == 1  # content_hash 去重


# ---- Policy（方案 §10.4 / §19）----


def test_policy_rejects_api_key():
    cand = MemoryCandidate(
        candidate_id="c",
        operation="add",
        memory_type="fact",
        scope="user",
        scope_id="u1",
        content="the api_key=sk-abcdefghijklmnopqrstuvwxyz is here",
        confidence=0.99,
        importance=0.99,
        source_event_ids=[],
    )
    ev = MemoryPolicy().evaluate(cand)
    assert ev.decision == "reject"


def test_policy_rejects_explicit_sensitive_label():
    cand = MemoryCandidate(
        candidate_id="c",
        operation="add",
        memory_type="fact",
        scope="user",
        scope_id="u1",
        content="normal",
        confidence=0.99,
        importance=0.99,
        source_event_ids=[],
        sensitive_labels=["token"],
    )
    assert MemoryPolicy().evaluate(cand).decision == "reject"


def test_policy_explicit_user_request_threshold():
    cand = MemoryCandidate(
        candidate_id="c",
        operation="add",
        memory_type="profile",
        scope="user",
        scope_id="u1",
        content="用 Python 3.12",
        confidence=0.65,
        importance=0.7,
        source_event_ids=[],
        reason="explicit_user_request",
    )
    assert MemoryPolicy().evaluate(cand).decision == "commit"


def test_policy_below_threshold_pending():
    cand = MemoryCandidate(
        candidate_id="c",
        operation="add",
        memory_type="profile",
        scope="user",
        scope_id="u1",
        content="maybe",
        confidence=0.5,
        importance=0.7,
        source_event_ids=[],
        reason="explicit_user_request",
    )
    assert MemoryPolicy().evaluate(cand).decision == "pending"


def test_policy_implicit_needs_observations():
    cand = MemoryCandidate(
        candidate_id="c",
        operation="add",
        memory_type="profile",
        scope="user",
        scope_id="u1",
        content="x",
        confidence=0.99,
        importance=0.99,
        source_event_ids=[],
        reason="implicit",
    )
    # observations=1 < 2 → pending
    assert MemoryPolicy().evaluate(cand, observations=1).decision == "pending"
    assert MemoryPolicy().evaluate(cand, observations=2).decision == "commit"


def test_policy_model_guess_rejected():
    cand = MemoryCandidate(
        candidate_id="c",
        operation="add",
        memory_type="fact",
        scope="user",
        scope_id="u1",
        content="guess",
        confidence=0.99,
        importance=0.99,
        source_event_ids=[],
        reason="model_guess",
    )
    assert MemoryPolicy().evaluate(cand).decision == "reject"


def test_policy_delete_without_target_rejected():
    cand = MemoryCandidate(
        candidate_id="c",
        operation="delete",
        memory_type="fact",
        scope="user",
        scope_id="u1",
        content="",
        confidence=0.99,
        importance=0.99,
        source_event_ids=[],
        reason="",
    )
    assert MemoryPolicy().evaluate(cand).decision == "reject"


def test_policy_conflict_supersede():
    existing = _record(memory_id="old", version=1)
    cand = MemoryCandidate(
        candidate_id="c",
        operation="update",
        memory_type="fact",
        scope="user",
        scope_id="u1",
        content="新偏好",
        confidence=0.9,
        importance=0.8,
        source_event_ids=[],
        conflicts_with=["old"],
        reason="explicit_user_request",
    )
    ev = MemoryPolicy().evaluate(cand, existing=existing)
    assert ev.decision == "commit" and ev.operation == "update" and ev.new_version == 2


# ---- Coordinator（方案 §9.2 / §10.6 / §10.8）----


def test_coordinator_flush_secret_rejected(provider):
    coord = MemoryCoordinator(provider)
    cand = MemoryCandidate(
        candidate_id="c",
        operation="add",
        memory_type="fact",
        scope="user",
        scope_id="u1",
        content="api_key=sk-abcdefghijklmnopqrstuvwx",
        confidence=0.99,
        importance=0.99,
        source_event_ids=[],
    )
    fr = coord.flush_candidates([cand])
    assert fr.committed == 0 and fr.rejected == 1


def test_coordinator_recall_failure_no_error_in_text(provider):
    coord = MemoryCoordinator(provider)
    # 空 query → not_configured，不抛
    res = coord.recall(build_search_request(query="", user_id="u1"))
    assert res.status == "not_configured"
    ctx = recall_to_context_item(res)
    assert ctx is None  # 失败/无结果不注入噪声（§10.8）


def test_coordinator_recall_returns_context(provider):
    provider.upsert(_record(content="偏好中文回答"), expected_version=None)
    coord = MemoryCoordinator(provider)
    res = coord.recall(build_search_request(query="中文", user_id="u1"))
    ctx = recall_to_context_item(res)
    assert ctx is not None and "偏好中文回答" in ctx["formatted_text"]


def test_coordinator_delete_user_forget(provider):
    provider.upsert(_record(memory_id="m1"), expected_version=None)
    coord = MemoryCoordinator(provider)
    assert coord.delete("m1", scope="user", scope_id="u1", hard=True) is True
    assert provider.get("m1") is None


def test_coordinator_hard_delete_unsupported_returns_false():
    class NoHardDeleteProvider:
        def capabilities(self):
            from ksadk.memory.models import MemoryCapabilities

            return MemoryCapabilities(False, True, False, True, False, False, 8192)

        def delete(self, request):
            raise AssertionError("should not call")

        def search(self, request): ...
        def get(self, mid): ...
        def upsert(self, r, *, expected_version): ...
        def list_core(self, request): ...

    coord = MemoryCoordinator(NoHardDeleteProvider())
    assert coord.delete("m1", scope="user", scope_id="u1", hard=True) is False


def test_detect_sensitive_labels_finds_dsn():
    labels = detect_sensitive_labels("postgres://user:pass@host/db", [])
    assert "dsn" in labels
