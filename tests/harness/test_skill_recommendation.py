"""Bound Skill discovery and recommendation stay metadata-only."""

from __future__ import annotations

from ksadk.harness.skill_runtime import SkillManifest, SkillRuntime


class _Source:
    manifests = {
        "skill://budget@1.0.0": SkillManifest(
            name="预算分析",
            summary="分析预算与实际支出偏差并生成报告",
        ),
        "skill://contract@1.0.0": SkillManifest(
            name="合同审查",
            summary="识别合同条款风险与缺失内容",
        ),
        "skill://weather@1.0.0": SkillManifest(
            name="天气查询",
            summary="查询城市天气与空气质量",
        ),
    }

    def manifest(self, skill_id: str) -> SkillManifest:
        return self.manifests[skill_id]

    def full_text(self, skill_id: str) -> str:
        raise AssertionError("recommendation must not load Skill instructions")

    def resource(self, skill_id: str, resource_ref: str) -> bytes:
        raise AssertionError("recommendation must not load Skill resources")


def test_recommendation_prioritizes_relevant_bound_skill_without_disclosure():
    runtime = SkillRuntime(_Source())

    catalog = runtime.recommend(
        tuple(_Source.manifests),
        query="请分析本月预算偏差，并说明实际支出超支原因",
    )

    assert catalog[0]["skill_id"] == "skill://budget@1.0.0"
    assert catalog[0]["recommended"] == "true"
    assert float(catalog[0]["recommendation_score"]) > 0
    assert runtime.level("run-1", "skill://budget@1.0.0") == 0


def test_recommendation_keeps_revision_order_when_query_has_no_match():
    runtime = SkillRuntime(_Source())
    refs = tuple(_Source.manifests)

    catalog = runtime.recommend(refs, query="完全无关的请求")

    assert tuple(item["skill_id"] for item in catalog) == refs
    assert all(item["recommended"] == "false" for item in catalog)


def test_recommendation_limit_marks_only_top_candidates():
    runtime = SkillRuntime(_Source())

    catalog = runtime.recommend(
        tuple(_Source.manifests),
        query="分析预算合同天气",
        limit=1,
    )

    assert sum(item["recommended"] == "true" for item in catalog) == 1
