"""CompactionRecord（长任务 Context 与 Memory 增强方案 §6.4）。

压缩的**投影变化记录**，不是新的聊天事实源（完整 Transcript 不删除）。
相比 ``ContextCheckpoint``（引擎内部的压缩检查点），CompactionRecord 面向
平台观测：补齐压缩前后 Token、事件范围、摘要模型引用和质量校验字段。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


def _compaction_id(material: str) -> str:
    return f"cmp_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:12]}"


@dataclass(frozen=True)
class CompactionRecord:
    """一次上下文压缩的完整记录（可持久化、可审计）。"""

    compaction_id: str
    run_id: str
    trigger: str
    before_tokens: int
    after_tokens: int
    #: 被压缩的 seq 区间（闭开）。
    compacted_event_range: tuple[int, int]
    preserved_event_ids: tuple[str, ...] = field(default=())
    summary: str = ""
    #: 摘要正文作为 Artifact 保存后的引用（artifact://...）。
    summary_artifact_ref: str = ""
    #: 压缩前受控 Memory Flush 提交的候选引用（mem://...）。
    memory_candidate_refs: tuple[str, ...] = field(default=())
    #: 生成摘要的模型引用（本地截断降级时为空）。
    summary_model_ref: str = ""
    retained_critical_facts: tuple[str, ...] = field(default=())
    dropped_critical_facts: tuple[str, ...] = field(default=())
    #: 摘要缺失但经重注入保留在模型输入中的关键事实（§8.4.1）。
    reinjected_critical_facts: tuple[str, ...] = field(default=())

    # ---- 质量校验（§6.4 quality_checks） ----
    tool_pairs_complete: bool = True
    approvals_preserved: bool = True
    goal_preserved: bool = True
    critical_facts_preserved: bool = True

    def quality_checks(self) -> dict[str, bool]:
        return {
            "tool_pairs_complete": self.tool_pairs_complete,
            "approvals_preserved": self.approvals_preserved,
            "goal_preserved": self.goal_preserved,
            "critical_facts_preserved": self.critical_facts_preserved,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "compaction_id": self.compaction_id,
            "run_id": self.run_id,
            "trigger": self.trigger,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "compacted_event_range": list(self.compacted_event_range),
            "preserved_event_ids": list(self.preserved_event_ids),
            "summary_artifact_ref": self.summary_artifact_ref,
            "memory_candidate_refs": list(self.memory_candidate_refs),
            "summary_model_ref": self.summary_model_ref,
            "retained_critical_facts": list(self.retained_critical_facts),
            "dropped_critical_facts": list(self.dropped_critical_facts),
            "reinjected_critical_facts": list(self.reinjected_critical_facts),
            "quality_checks": self.quality_checks(),
        }


def build_compaction_record(
    *,
    run_id: str,
    trigger: str,
    before_tokens: int,
    after_tokens: int,
    compacted_event_range: tuple[int, int],
    summary: str,
    retained_critical_facts: tuple[str, ...],
    dropped_critical_facts: tuple[str, ...],
    reinjected_critical_facts: tuple[str, ...] = (),
    summary_model_ref: str = "",
    memory_candidate_refs: tuple[str, ...] = (),
    tool_pairs_complete: bool = True,
    approvals_preserved: bool = True,
    goal_preserved: bool = True,
) -> CompactionRecord:
    """从压缩结果构建完整记录。

    ``critical_facts_preserved`` 按「重注入后仍丢弃」判定（方案 §6.4
    "不静默丢弃"）：摘要缺失的事实经重注入（§8.4.1）保留在模型输入中，
    不算最终丢弃——真实模型评测曾因此误报 constraint_retention<1.0。
    """
    still_dropped = set(dropped_critical_facts) - set(reinjected_critical_facts)
    critical_ok = not still_dropped
    material = f"{run_id}:{trigger}:{before_tokens}:{compacted_event_range}:{summary[:64]}"
    return CompactionRecord(
        compaction_id=_compaction_id(material),
        run_id=run_id,
        trigger=trigger,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        compacted_event_range=compacted_event_range,
        summary=summary,
        summary_model_ref=summary_model_ref,
        memory_candidate_refs=memory_candidate_refs,
        retained_critical_facts=retained_critical_facts,
        dropped_critical_facts=dropped_critical_facts,
        reinjected_critical_facts=reinjected_critical_facts,
        tool_pairs_complete=tool_pairs_complete,
        approvals_preserved=approvals_preserved,
        goal_preserved=goal_preserved,
        critical_facts_preserved=critical_ok,
    )


__all__ = ["CompactionRecord", "build_compaction_record"]
