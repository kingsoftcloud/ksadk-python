"""长任务方案 P2：混合检索管线（Rerank / 多样性 / Token 截断）测试。"""

from __future__ import annotations

from ksadk.memory.models import MemoryRecord
from ksadk.memory.retrieval import RetrievalConfig, keyword_coverage, rerank_records


def _record(
    memory_id: str,
    content: str,
    *,
    slot_key: str = "",
    importance: float = 0.5,
) -> MemoryRecord:
    return MemoryRecord(
        memory_id=memory_id,
        tenant_id="t",
        workspace_id="w",
        scope="user",
        scope_id="user:u1",
        memory_type="fact",
        content=content,
        summary=content[:200],
        status="active",
        confidence=0.8,
        importance=importance,
        valid_from="",
        valid_to="",
        expires_at="",
        source_session_id="",
        source_event_ids=[],
        source_seq_range=None,
        content_hash="",
        version=1,
        metadata={"slot_key": slot_key} if slot_key else {},
    )


class TestRerank:
    def test_keyword_coverage_ranks_relevant_first(self):
        records = [
            _record("m1", "今天天气不错，适合散步"),
            _record("m2", "Q3 预算口径：以财务系统冻结数为准"),
            _record("m3", "用户喜欢羽毛球"),
        ]
        out = rerank_records(records, query="预算 口径")
        assert out[0].memory_id == "m2"

    def test_vector_score_blends_when_injected(self):
        records = [
            _record("m_kw", "预算口径文档"),
            _record("m_vec", "财务冻结数说明"),
        ]
        out = rerank_records(
            records,
            query="预算",
            config=RetrievalConfig(vector_weight=0.8),
            vector_score=lambda r: 1.0 if r.memory_id == "m_vec" else 0.0,
        )
        assert out[0].memory_id == "m_vec"

    def test_no_vector_injection_degrades_to_keyword(self):
        records = [_record("a", "预算"), _record("b", "无关")]
        out = rerank_records(records, query="预算", config=RetrievalConfig(vector_weight=0.9))
        assert out[0].memory_id == "a"

    def test_diversity_same_slot_only_best_kept(self):
        records = [
            _record("s1", "预算口径 A", slot_key="budget:rule"),
            _record("s2", "预算口径 B", slot_key="budget:rule"),
            _record("s3", "其他事实"),
        ]
        out = rerank_records(records, query="预算 口径")
        ids = [r.memory_id for r in out]
        assert "s1" in ids and "s2" not in ids, "同 slot 只留最高分"

    def test_token_budget_truncation(self):
        records = [
            _record("big1", "预算 " + "细节" * 2000),
            _record("small", "预算口径"),
        ]
        out = rerank_records(
            records, query="预算", config=RetrievalConfig(max_tokens=50)
        )
        total_chars = sum(len(r.content) for r in out)
        assert total_chars < 500, "超出 Token 预算必须截断"


class TestKeywordCoverage:
    def test_full_and_partial_coverage(self):
        assert keyword_coverage("预算 口径", "预算口径以系统为准") == 1.0
        assert keyword_coverage("预算 冻结", "预算口径以系统为准") == 0.5
        assert keyword_coverage("budget", "Q3 budget freeze") == 1.0
        assert keyword_coverage("", "任意") == 0.0
