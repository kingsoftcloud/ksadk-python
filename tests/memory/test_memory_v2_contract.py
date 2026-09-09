"""Memory v2 契约测试 —— Provider / Policy / Coordinator（方案 §17.4）。

所有 Provider 共用同一套测试（方案 §17.4）。这里对 SQLite Provider 跑完整契约，Policy 与
Coordinator 单测覆盖敏感信息拒绝、阈值、冲突、scope 隔离、删除与错误隔离。
"""

from __future__ import annotations

from dataclasses import replace

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


def test_sqlite_provider_creates_a_fresh_parent_directory(tmp_path):
    db_path = tmp_path / "new-workspace" / ".agentengine" / "ui" / "memory.db"

    local = SqliteMemoryProvider(db_path=db_path)
    try:
        assert db_path.is_file()
        assert local.capabilities().versioned_update is True
    finally:
        local.close()


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


def test_sqlite_provider_mixed_cjk_query_does_not_block_ascii_fact(provider):
    provider.upsert(
        _record(memory_id="m-python", scope_id="u-python", content="用户偏好用 Python 3.12"),
        expected_version=None,
    )

    result = provider.search(
        MemorySearchRequest(
            query="Python 3.12 是什么",
            scopes=[("user", "u-python")],
            memory_types=["fact"],
        )
    )

    assert [item.memory_id for item in result.records] == ["m-python"]


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


def test_provider_delete_enforces_scope(provider):
    provider.upsert(_record(memory_id="m-scope", scope_id="u1"), expected_version=None)

    result = provider.delete(
        MemoryDeleteRequest(memory_id="m-scope", scope="user", scope_id="u2", hard=True)
    )

    assert result.deleted is False
    assert provider.get("m-scope") is not None


def test_provider_cleanup_honors_retention_and_ttl(provider):
    old = replace(
        _record(memory_id="m-old", content="old"),
        created_at="2020-01-01T00:00:00Z",
        importance=0.1,
    )
    recent = replace(_record(memory_id="m-recent", content="recent"), importance=0.1)
    expired = replace(
        _record(memory_id="m-expired", content="expired"),
        expires_at="2020-01-01T00:00:00Z",
    )
    provider.upsert(old, expected_version=None)
    provider.upsert(recent, expected_version=None)
    provider.upsert(expired, expected_version=None)

    assert provider.cleanup(expire_days=90) == 2
    assert provider.get("m-old") is None
    assert provider.get("m-expired") is None
    assert provider.get("m-recent") is not None


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


def test_coordinator_supersedes_previous_fact_in_same_slot(provider):
    coord = MemoryCoordinator(provider)
    old_candidate = MemoryCandidate(
        candidate_id="old-candidate",
        operation="add",
        memory_type="profile",
        scope="user",
        scope_id="u1",
        content="我喜欢吃芥末",
        confidence=0.9,
        importance=0.8,
        source_event_ids=["e1"],
        slot_key="profile.preference.food",
        reason="explicit_user_request",
    )
    assert coord.flush_candidates([old_candidate]).committed == 1
    old_result = provider.search(
        MemorySearchRequest(
            query="",
            scopes=[("user", "u1")],
            memory_types=["profile"],
            filters={"slot_key": "profile.preference.food"},
        )
    )
    assert len(old_result.records) == 1
    old_record = old_result.records[0]

    corrected = MemoryCandidate(
        candidate_id="new-candidate",
        operation="update",
        memory_type="profile",
        scope="user",
        scope_id="u1",
        content="我喜欢吃西红柿",
        confidence=0.95,
        importance=0.9,
        source_event_ids=["e2"],
        slot_key="profile.preference.food",
        reason="explicit_user_correction",
    )
    assert coord.flush_candidates([corrected]).committed == 1

    superseded = provider.get(old_record.memory_id)
    assert superseded is not None
    assert superseded.status == "superseded"
    assert superseded.metadata["superseded_by"]

    active = provider.search(
        MemorySearchRequest(
            query="喜欢吃",
            scopes=[("user", "u1")],
            memory_types=["profile"],
            filters={"slot_key": "profile.preference.food"},
        )
    ).records
    assert [record.content for record in active] == ["我喜欢吃西红柿"]
    assert active[0].metadata["supersedes"] == [old_record.memory_id]


def test_coordinator_supersedes_legacy_profile_without_slot_metadata(provider):
    provider.upsert(
        _record(
            memory_id="legacy-mustard",
            content="我喜欢吃芥末",
            memory_type="profile",
        ),
        expected_version=None,
    )
    corrected = MemoryCandidate(
        candidate_id="correction",
        operation="update",
        memory_type="profile",
        scope="user",
        scope_id="u1",
        content="我喜欢吃西红柿",
        confidence=0.95,
        importance=0.9,
        source_event_ids=["e2"],
        slot_key="profile.preference.food",
        reason="explicit_user_correction",
    )
    assert MemoryCoordinator(provider).flush_candidates([corrected]).committed == 1
    assert provider.get("legacy-mustard").status == "superseded"
    active = provider.search(
        MemorySearchRequest(
            query="喜欢吃",
            scopes=[("user", "u1")],
            memory_types=["profile"],
        )
    ).records
    assert [record.content for record in active] == ["我喜欢吃西红柿"]


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
