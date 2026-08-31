"""Context 策略调优建议（只读、可解释、不会自动改线上配置）。

Harness 已经通过 :func:`context_inspection` 暴露预算、压缩、缓存、Memory
召回和恢复信号。本模块把这些稳定指标转换为 Studio 可展示的建议。它只输出
建议与证据，不直接修改 ``HarnessSpec``；策略变更仍须经过 Draft、Revision
和验证，避免单次异常 Run 静默改变线上行为。
"""

from __future__ import annotations

from typing import Any, Sequence

from ksadk.harness.events import RuntimeEvent
from ksadk.harness.observability import context_inspection


def context_tuning_recommendations(
    events: Sequence[RuntimeEvent],
) -> dict[str, Any]:
    """生成 JSON 兼容的 Context 调优建议。

    输出不包含 Prompt、消息、Memory、Tool 返回或底层错误正文，可以直接供
    Studio 和发布评审消费。建议按 ``critical/high/medium/low`` 排序；没有
    证据时返回空列表，而不是猜测配置。
    """

    inspection = context_inspection(events)
    current = inspection["current"]
    cache = inspection["cache"]
    compaction = inspection["compaction"]
    memory = inspection["memory"]
    recoveries = inspection["recoveries"]
    recommendations: list[dict[str, Any]] = []

    utilization = float(current.get("utilization_ratio") or 0.0)
    if utilization > 1.0:
        recommendations.append(
            _recommendation(
                "context_budget_exceeded",
                "critical",
                "降低常驻 Context 或提高模型上下文预算",
                "实际或预计输入已超过本次预算，存在 Context Overflow 风险。",
                {"utilization_ratio": utilization},
                "review_context_budget",
            )
        )
    elif utilization >= 0.85:
        recommendations.append(
            _recommendation(
                "context_budget_near_limit",
                "high",
                "提前整理 Working Context",
                "Context 使用率已接近预算上限，建议在下一轮前主动压缩。",
                {"utilization_ratio": utilization},
                "lower_compaction_threshold",
            )
        )

    quality_failures = len(compaction.get("quality_failures") or ())
    if quality_failures:
        recommendations.append(
            _recommendation(
                "compaction_quality_failed",
                "critical",
                "暂停自动放宽压缩并检查摘要质量",
                "压缩质量门禁失败；应先修复事实或工具配对保留问题。",
                {"quality_failure_count": quality_failures},
                "inspect_compaction_trace",
            )
        )
    elif int(compaction.get("count") or 0) >= 3:
        recommendations.append(
            _recommendation(
                "frequent_compaction",
                "medium",
                "减少重复注入并整理稳定 Prompt",
                "同一观测窗口发生多次压缩，可能存在历史重复注入或常驻段过大。",
                {"compaction_count": int(compaction["count"])},
                "review_context_sections",
            )
        )

    diagnostics = int(cache.get("diagnostic_count") or 0)
    break_count = int(cache.get("break_count") or 0)
    hit_ratio = float(cache.get("provider_hit_ratio") or 0.0)
    if break_count:
        recommendations.append(
            _recommendation(
                "prompt_cache_breaks",
                "medium",
                "稳定 Prompt 前缀与缓存边界",
                "观测到 Provider Prompt Cache 边界变化。",
                {"break_count": break_count, "provider_hit_ratio": hit_ratio},
                "inspect_cache_diagnostics",
            )
        )
    elif diagnostics >= 3 and hit_ratio < 0.2:
        recommendations.append(
            _recommendation(
                "low_prompt_cache_hit_ratio",
                "low",
                "检查稳定 Prompt 是否混入动态内容",
                "已有多次缓存诊断，但 Provider 缓存命中率偏低。",
                {"diagnostic_count": diagnostics, "provider_hit_ratio": hit_ratio},
                "review_stable_prompt_boundary",
            )
        )

    candidates = int(memory.get("candidate_count") or 0)
    injected = int(memory.get("injected_count") or 0)
    if candidates >= 5 and injected == 0:
        recommendations.append(
            _recommendation(
                "memory_candidates_not_injected",
                "medium",
                "检查 Memory 召回阈值和作用域",
                "召回已有候选，但没有任何记录进入 Context。",
                {"candidate_count": candidates, "injected_count": injected},
                "inspect_memory_recall",
            )
        )

    recovery_count = int(recoveries.get("count") or 0)
    if recovery_count:
        recommendations.append(
            _recommendation(
                "context_recovery_observed",
                "high",
                "检查 Context 降级与恢复原因",
                "本观测窗口发生过 Context 恢复或降级，正式发布前应确认原因。",
                {
                    "recovery_count": recovery_count,
                    "reason_codes": list(recoveries.get("reason_codes") or ()),
                },
                "inspect_recovery_events",
            )
        )

    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    recommendations.sort(key=lambda item: (order[item["severity"]], item["code"]))
    return {
        "schema_version": 1,
        "status": "action_required" if recommendations else "healthy",
        "auto_apply": False,
        "recommendation_count": len(recommendations),
        "recommendations": recommendations,
    }


def _recommendation(
    code: str,
    severity: str,
    title: str,
    rationale: str,
    evidence: dict[str, Any],
    action: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "title": title,
        "rationale": rationale,
        "evidence": evidence,
        "suggested_action": action,
    }


__all__ = ["context_tuning_recommendations"]
