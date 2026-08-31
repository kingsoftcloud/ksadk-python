"""真实模型评测模块测试（离线部分；真实模型调用由 KSADK_REAL_MODEL_EVAL=1 门控）。"""

from __future__ import annotations

import os

import pytest

from ksadk.harness.real_model_eval import (
    LARGE_MEMORY_ANNOTATION_DATASET,
    MEMORY_ANNOTATION_DATASET,
    GoldenAnnotation,
    RealModelReasoner,
    evaluate_memory_annotation,
    rule_based_annotate,
    score_annotations,
)

_REAL_MODEL = os.getenv("KSADK_REAL_MODEL_EVAL") == "1"


def test_annotation_dataset_frozen_shape():
    assert len(MEMORY_ANNOTATION_DATASET) == 4
    assert {c.case_id for c in MEMORY_ANNOTATION_DATASET} == {
        "explicit-requests",
        "preference-correction",
        "tool-facts-and-noise",
        "no-memory-at-all",
    }


def test_large_annotation_dataset_has_100_unique_balanced_cases():
    assert len(LARGE_MEMORY_ANNOTATION_DATASET) == 100
    assert len({case.case_id for case in LARGE_MEMORY_ANNOTATION_DATASET}) == 100
    assert sum(not case.golden for case in LARGE_MEMORY_ANNOTATION_DATASET) == 25


def test_large_annotation_dataset_rule_pipeline_meets_offline_gate():
    report = evaluate_memory_annotation(
        dataset=LARGE_MEMORY_ANNOTATION_DATASET,
        include_model=False,
    )

    assert len(report["cases"]) == 100
    assert report["rule_based"] == {"precision": 1.0, "recall": 1.0, "f1": 1.0}


def test_rule_annotator_scores_against_golden():
    """规则标注器（生产路径）对金标的已知得分——回归锚点。"""
    case = MEMORY_ANNOTATION_DATASET[0]  # explicit-requests
    score = score_annotations(rule_based_annotate(case.events), case.golden)
    assert score == {"precision": 1.0, "recall": 1.0, "f1": 1.0}


def test_matcher_tolerates_rephrasing():
    """宽松匹配：语义等价但措辞不同（用户的/我的）不算 miss。"""
    golden = (GoldenAnnotation("我的报销必须走对公转账", "profile", "add", 0.9),)
    score = score_annotations(
        [{"content": "用户的报销必须走对公转账"}], golden
    )
    assert score["recall"] == 1.0


def test_matcher_rejects_unrelated():
    golden = (GoldenAnnotation("我的工号是 KS-04217", "profile", "add", 0.9),)
    score = score_annotations([{"content": "用户喜欢简洁的报表"}], golden)
    assert score["recall"] == 0.0 and score["precision"] == 0.0


def test_empty_golden_perfect_when_no_predictions():
    score = score_annotations([], ())
    assert score == {"precision": 1.0, "recall": 1.0, "f1": 1.0}


@pytest.mark.skipif(not _REAL_MODEL, reason="需要真实模型端点（KSADK_REAL_MODEL_EVAL=1）")
def test_real_model_reasoner_returns_usage():
    import asyncio

    async def drive():
        reasoner = RealModelReasoner()
        return await reasoner.complete(
            model="",
            prompt="",
            messages=[{"role": "user", "content": "回复：好的"}],
            tools=[],
        )

    turn = asyncio.run(drive())
    assert turn.final_text
    assert turn.usage and turn.usage["input_tokens"] > 0
