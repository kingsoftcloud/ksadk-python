"""Phase 2 Memory Runtime 测试（plan §17 验收：隔离 / 权限 / 审计）。"""

from __future__ import annotations

import pytest

from ksadk.harness.memory_runtime import (
    HarnessMemoryError,
    HarnessMemoryRuntime,
    MemoryWriteRequest,
)
from ksadk.harness.spec import HarnessSpec, MemoryPolicy, ModelBinding, PromptSpec


def _spec(enabled: bool = True, scopes=("session", "agent", "user", "org")) -> HarnessSpec:
    return HarnessSpec(
        agent_revision_ref="agent-revision://proj-1@2",
        model=ModelBinding(profile_ref="model-profile://kimi-k3@1.0.0"),
        prompt=PromptSpec(instructions="你是助手"),
        memory_policy=MemoryPolicy(enabled=enabled, scopes=scopes),
    )


def _write(
    scope: str = "user", source: str = "user_explicit", content: str = "用户偏好：报表按月"
) -> MemoryWriteRequest:
    return MemoryWriteRequest(
        operation="add",
        content=content,
        scope=scope,  # type: ignore[arg-type]
        scope_id="user:u1",
        source=source,
    )


class TestControlledWrite:
    def test_write_with_source_commits_and_audits(self):
        rt = HarnessMemoryRuntime.local_sqlite()
        evaluation, event = rt.write(_write(), _spec(), run_id="r1")
        assert evaluation.decision == "commit"
        assert event is not None and event.event_type == "memory.write"
        assert event.payload["scope"] == "user"
        assert event.payload["source"] == "user_explicit"
        assert rt.consolidation_queue.pending_count == 1

    def test_write_without_source_rejected(self):
        rt = HarnessMemoryRuntime.local_sqlite()
        with pytest.raises(HarnessMemoryError, match="来源"):
            rt.write(_write(source=""), _spec(), run_id="r1")

    def test_raw_transcript_write_rejected(self):
        rt = HarnessMemoryRuntime.local_sqlite()
        with pytest.raises(HarnessMemoryError, match="整段聊天"):
            rt.write(_write(source="raw_transcript"), _spec(), run_id="r1")

    def test_sensitive_labels_rejected(self):
        rt = HarnessMemoryRuntime.local_sqlite()
        request = MemoryWriteRequest(
            operation="add",
            content="卡号 6222...",
            scope="user",
            scope_id="user:u1",
            source="user_explicit",
            sensitive_labels=("pii",),
        )
        with pytest.raises(HarnessMemoryError, match="脱敏"):
            rt.write(request, _spec(), run_id="r1")

    def test_scope_not_in_allowlist_rejected(self):
        rt = HarnessMemoryRuntime.local_sqlite()
        spec = _spec(scopes=("session", "agent"))  # 不含 user/org
        with pytest.raises(HarnessMemoryError, match="允许列表"):
            rt.write(_write(scope="user"), spec, run_id="r1")

    def test_memory_disabled_rejects_long_term_write(self):
        rt = HarnessMemoryRuntime.local_sqlite()
        with pytest.raises(HarnessMemoryError, match="未启用"):
            rt.write(_write(), _spec(enabled=False), run_id="r1")

    def test_org_scope_requires_user_explicit_source(self):
        rt = HarnessMemoryRuntime.local_sqlite()
        with pytest.raises(HarnessMemoryError, match="组织级"):
            rt.write(_write(scope="org", source="extraction"), _spec(), run_id="r1")


class TestIsolation:
    def test_users_isolated_by_scope_id(self):
        """不同用户 Memory 隔离：u1 的记忆 u2 检索不到。"""
        rt = HarnessMemoryRuntime.local_sqlite()
        rt.write(
            MemoryWriteRequest(
                operation="add",
                content="用户 u1 偏好报表按月",
                scope="user",
                scope_id="user:u1",
                source="user_explicit",
            ),
            _spec(),
            run_id="r1",
        )
        u1_hits = rt.recall(query="报表偏好", scopes=[("user", "user:u1")])
        u2_hits = rt.recall(query="报表偏好", scopes=[("user", "user:u2")])
        assert len(u1_hits.records) >= 1
        assert len(u2_hits.records) == 0

    def test_tenants_isolated_by_provider(self):
        rt1 = HarnessMemoryRuntime.local_sqlite(tenant_id="corp-a")
        rt2 = HarnessMemoryRuntime.local_sqlite(tenant_id="corp-b")
        rt1.write(_write(content="corp-a 的预算口径"), _spec(), run_id="r1")
        b_hits = rt2.recall(query="预算口径", scopes=[("user", "user:u1")])
        assert len(b_hits.records) == 0


class TestSemanticRecall:
    class _Scorer:
        def score(self, *, query, records):
            return {
                record.memory_id: (1.0 if "冻结数" in record.content else 0.0)
                for record in records
            }

    class _FailingScorer:
        def score(self, *, query, records):
            raise TimeoutError("semantic backend timeout")

    @staticmethod
    def _seed(runtime: HarnessMemoryRuntime) -> None:
        runtime.write(_write(content="预算口径文档"), _spec(), run_id="semantic-1")
        runtime.write(
            _write(content="预算冻结数是最终财务事实"), _spec(), run_id="semantic-2"
        )

    def test_optional_semantic_scorer_is_wired_into_default_recall(self):
        runtime = HarnessMemoryRuntime.local_sqlite(
            semantic_scorer=self._Scorer(), semantic_weight=0.9
        )
        self._seed(runtime)

        result = runtime.recall(query="预算", scopes=[("user", "user:u1")])

        assert result.records[0].content == "预算冻结数是最终财务事实"
        assert result.retrieval_strategy == "hybrid"
        assert result.reranker_status == "applied"

    def test_semantic_failure_degrades_without_failing_recall(self):
        runtime = HarnessMemoryRuntime.local_sqlite(
            semantic_scorer=self._FailingScorer(), semantic_weight=0.9
        )
        self._seed(runtime)

        result = runtime.recall(query="预算", scopes=[("user", "user:u1")])

        assert result.status == "ok"
        assert result.records
        assert result.retrieval_strategy == "keyword"
        assert result.reranker_status == "degraded"


class TestCoreMemory:
    def test_list_core_returns_committed_blocks_with_limits(self):
        rt = HarnessMemoryRuntime.local_sqlite(max_core_blocks=4)
        for i in range(6):
            rt.write(
                MemoryWriteRequest(
                    operation="add",
                    content=f"已确认事实 {i}：预算上限 {i * 1000}",
                    scope="user",
                    scope_id="user:u1",
                    source="user_explicit",
                ),
                _spec(),
                run_id=f"r{i}",
            )
        blocks = rt.list_core(scopes=[("user", "user:u1")])
        assert 0 < len(blocks) <= 4  # 常驻块数量有限（§9.2）
