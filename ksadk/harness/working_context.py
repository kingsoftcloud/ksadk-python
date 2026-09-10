"""Working Context 更新机制（plan §8.5 + 长任务方案 §6.1 Patch 化）。

确定性规则维护 Session/Task 生命周期的影子工作上下文：
- Tool 失败 / 审批拒绝 → ``recent_tool_failures``（有界滑动窗口）；
- Tool 结果中的关键事实（ID/金额/日期/版本/审批号）→ ``verified_facts``
  （有界，去重）。

所有更新经 :mod:`working_context_patch` 的结构化 Patch（版本 + 来源 + 理由），
不使用模型调用——与 PCM 的"纯计算 + 受控调用"分层一致。
"""

from __future__ import annotations

from ksadk.harness.context_engine import extract_critical_facts
from ksadk.harness.state import WorkingContext
from ksadk.harness.working_context_patch import (
    PatchOperation,
    WorkingContextPatch,
    apply_patch,
)

_MAX_TOOL_FAILURES = 8
_MAX_VERIFIED_FACTS = 32


def record_tool_failure(
    working: WorkingContext,
    *,
    name: str,
    error: str,
    source_event_id: str = "",
) -> WorkingContext:
    """记录一次工具失败/审批拒绝（有界，保留最近 N 条；走结构化 Patch）。"""
    entry = f"{name}: {error}"[:512]
    bounded = (working.recent_tool_failures + (entry,))[-_MAX_TOOL_FAILURES:]
    return apply_patch(
        working,
        WorkingContextPatch(
            base_version=working.version,
            operations=(
                PatchOperation(
                    op="set", field_name="recent_tool_failures", value=bounded
                ),
            ),
            source_event_ids=(source_event_id,) if source_event_id else (),
            reason=f"tool_failure:{name}",
        ),
    )


def record_tool_result(
    working: WorkingContext,
    *,
    name: str,
    result_text: str,
    source_event_id: str = "",
) -> WorkingContext:
    """从工具结果抽取关键事实记入 verified_facts（去重、有界；走结构化 Patch）。"""
    facts = extract_critical_facts(result_text)
    if not facts:
        return working
    existing = set(working.verified_facts)
    merged = working.verified_facts + tuple(
        entry for entry in (f"{name} -> {fact}" for fact in sorted(facts)) if entry not in existing
    )
    return apply_patch(
        working,
        WorkingContextPatch(
            base_version=working.version,
            operations=(
                PatchOperation(
                    op="set", field_name="verified_facts", value=merged[-_MAX_VERIFIED_FACTS:]
                ),
            ),
            source_event_ids=(source_event_id,) if source_event_id else (),
            reason=f"tool_result:{name}",
        ),
    )


__all__ = ["record_tool_failure", "record_tool_result"]
