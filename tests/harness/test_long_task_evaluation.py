"""长任务固定数据集评测与发布门禁测试（P2 补强）。"""

from __future__ import annotations

from ksadk.harness.evaluation import (
    FIXED_DATASET,
    FactDroppingReasoner,
    default_engine_factory,
    evaluate_long_task,
    release_gate,
)
from ksadk.harness.events import EventType, RuntimeEvent


def test_fixed_dataset_is_frozen_shape():
    assert len(FIXED_DATASET) == 3
    assert all(c.expected_facts for c in FIXED_DATASET)
    # 关键事实不重复、用例 id 唯一。
    ids = [c.case_id for c in FIXED_DATASET]
    assert len(set(ids)) == len(ids)


def test_baseline_eval_passes_release_gate():
    report = evaluate_long_task()
    assert report["aggregate"]["fact_retention"] == 1.0
    assert report["aggregate"]["usage_manifest_paired_ratio"] == 1.0
    # 跨压缩：至少一个用例真实发生压缩，基线才有意义。
    assert any(c["compaction_count"] >= 1 for c in report["cases"])
    gate = release_gate(report)
    assert gate["passed"], gate["violations"]


def test_degraded_reasoner_rescued_by_reinjection():
    """坏摘要（丢一半关键事实）被重注入兜底——fact_retention 仍 1.0。

    §8.4.1 重注入的设计意义：摘要模型丢事实不等于最终丢事实，
    dropped-but-reinjected 不计入 fact_retention（与
    CompactionRecord critical_facts_preserved 同一口径）。
    """
    report = evaluate_long_task(
        engine_factory=default_engine_factory(FactDroppingReasoner())
    )
    assert report["aggregate"]["fact_retention"] == 1.0


def test_gate_fails_when_facts_finally_dropped():
    """重注入也救不回的丢失（合成事件）→ 门禁必须拦截。"""
    dropped_event = RuntimeEvent.create(
        EventType.CONTEXT_COMPACTION_COMPLETED,
        agent_id="a",
        user_id="u",
        session_id="s",
        invocation_id="r",
        seq_id=1,
        payload={
            "phase": "after",
            "trigger": "proactive",
            "compacted_until_seq_id": 10,
            "dropped_critical_facts": ["INV-2026-0001"],
            "reinjected_critical_facts": [],
        },
    )
    from ksadk.harness.evaluation import fact_retention

    case = FIXED_DATASET[0]
    assert fact_retention(case, [dropped_event]) < 1.0
    report = {"aggregate": {"fact_retention": 0.0}}
    gate = release_gate(report)
    assert not gate["passed"]
    violated = {v["metric"] for v in gate["violations"]}
    assert "fact_retention" in violated
