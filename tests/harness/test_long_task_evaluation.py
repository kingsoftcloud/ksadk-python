"""长任务固定数据集评测与发布门禁测试（P2 补强）。"""

from __future__ import annotations

from ksadk.harness.evaluation import (
    FIXED_DATASET,
    FactDroppingReasoner,
    default_engine_factory,
    evaluate_long_task,
    release_gate,
)


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


def test_degraded_reasoner_fails_gate():
    report = evaluate_long_task(
        engine_factory=default_engine_factory(FactDroppingReasoner())
    )
    gate = release_gate(report)
    assert not gate["passed"]
    violated = {v["metric"] for v in gate["violations"]}
    assert "fact_retention" in violated
    assert gate["violations"][0]["threshold"] == 1.0
