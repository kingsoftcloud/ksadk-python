"""长任务质量指标（长任务方案 §11，P2）。

从 RuntimeEvent 流与召回/写入事实计算可重复的离线指标：

- Goal/Constraint Retention：压缩后目标/约束是否保留；
- Evidence Fidelity：关键事实（ID/金额/日期）在压缩摘要中的保留率；
- Tool Pair Integrity：tool.call.begin/end 配对完整率；
- Memory Precision / Recall：写入事实中有用且正确的比例 / 需要时成功召回的比例。

指标函数是纯函数——绑定固定测试集调用（避免不同环境数据直接比较）。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from ksadk.harness.context_engine import extract_critical_facts
from ksadk.harness.events import EventType, RuntimeEvent


def compaction_fidelity(events: Sequence[RuntimeEvent]) -> dict[str, float]:
    """从事件流计算压缩保真指标（无压缩事件时全部 1.0）。"""
    completed = [e for e in events if e.event_type == EventType.CONTEXT_COMPACTION_COMPLETED]
    tool_begins = [e for e in events if e.event_type == EventType.TOOL_CALL_BEGIN]
    tool_ends = [e for e in events if e.event_type == EventType.TOOL_CALL_END]
    return {
        "goal_retention": _avg(
            [_check(e, "goal_preserved") for e in completed]
        ),
        "constraint_retention": _avg(
            [_check(e, "critical_facts_preserved") for e in completed]
        ),
        "tool_pair_integrity": (
            len(tool_ends) / len(tool_begins) if tool_begins else 1.0
        ),
        "compaction_count": float(len(completed)),
    }


def evidence_fidelity(summary_text: str, original_text: str) -> float:
    """摘要相对原文的关键事实保留率（Evidence Fidelity）。"""
    expected = extract_critical_facts(original_text)
    if not expected:
        return 1.0
    retained = extract_critical_facts(summary_text)
    return len(expected & retained) / len(expected)


def memory_precision_recall(
    *,
    written: Iterable[str],
    relevant: Iterable[str],
    recalled: Iterable[str],
) -> dict[str, float]:
    """Memory Precision / Recall（离线评测，绑定固定测试集）。

    - precision = |relevant ∩ written| / |written|（写入中有用且正确的比例）；
    - recall = |relevant ∩ recalled| / |relevant|（需要时成功召回的比例）。
    """
    written_set = {w.strip() for w in written if w.strip()}
    relevant_set = {r.strip() for r in relevant if r.strip()}
    recalled_set = {r.strip() for r in recalled if r.strip()}
    precision = (
        len(written_set & relevant_set) / len(written_set) if written_set else 1.0
    )
    recall = len(relevant_set & recalled_set) / len(relevant_set) if relevant_set else 1.0
    return {"precision": precision, "recall": recall}


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 1.0


def _check(event: RuntimeEvent, key: str) -> float:
    return float(event.payload.get("quality_checks", {}).get(key, True))


def summarize_run(run_events: Sequence[RuntimeEvent], **extra: Any) -> dict[str, Any]:
    """一次长任务 Run 的指标汇总（供评测门禁消费）。"""
    report: dict[str, Any] = {"compaction": compaction_fidelity(run_events)}
    report.update(extra)
    return report


__all__ = [
    "compaction_fidelity",
    "evidence_fidelity",
    "memory_precision_recall",
    "summarize_run",
]
