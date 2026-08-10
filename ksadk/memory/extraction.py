"""Memory Candidate 抽取（方案 §9.2 / §10.3 / §10.4）。

压缩前从 ``groups_to_compact`` 的事件里确定性提取记忆候选。首期只做确定性提取，不调用模型
（方案 §9.3：优先确定性提取；模型辅助可关闭）：

- 用户显式"记住/remember/别忘了" → ``profile`` 候选（reason=explicit_user_request）。
- 工具返回的稳定事实（含 "确认/confirmed/最终/final" 字样）→ ``fact`` 候选（reason=tool_fact）。

提取结果交 ``MemoryPolicy.evaluate`` 评估；secret/PII、一次性当前任务状态、模型猜测由 Policy
拒绝（方案 §10.4）。本期不做 LLM 辅助抽取，避免把模型猜测写入长期记忆。
"""

from __future__ import annotations

import re
import uuid
from typing import Iterable, Mapping, Sequence

from ksadk.memory.models import MemoryCandidate, MemoryScope, MemoryType

# 显式记忆意图（中英）。
_EXPLICIT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)记住[:：]?\s*(.+)"),
    re.compile(r"(?i)别忘了[:：]?\s*(.+)"),
    re.compile(r"(?i)remember\s+(?:that\s+)?(.+)", re.IGNORECASE),
    re.compile(r"(?i)请记[:：]?\s*(.+)"),
)
# 工具稳定事实信号。
_FACT_SIGNALS = ("confirmed", "最终确认", "final", "verified", "确认成功")


def _event_text(event: any) -> str:  # type: ignore[name-defined]
    try:
        from ksadk.conversations.context import extract_event_text
        return extract_event_text(event)
    except Exception:  # noqa: BLE001
        return str(getattr(event, "text", "") or "")


def propose_memory_candidates(
    events: Sequence[any],  # type: ignore[name-defined]
    *,
    scope: MemoryScope = "user",
    scope_id: str = "",
) -> list[MemoryCandidate]:
    """从待压缩事件提取记忆候选（方案 §9.2）。

    纯确定性、无 LLM。返回候选列表交 Coordinator flush；Policy 决定 commit/reject。
    """
    candidates: list[MemoryCandidate] = []
    if not events:
        return candidates
    for event in events:
        text = _event_text(event).strip()
        if not text:
            continue
        event_type = getattr(event, "event_type", "") or ""
        author = getattr(event, "author", "") or ""
        seq = getattr(event, "seq_id", 0) or 0
        event_id = getattr(event, "id", "") or f"evt_{seq}"

        # 1. 用户显式记忆意图
        if author == "user" or event_type == "user_message":
            for pattern in _EXPLICIT_PATTERNS:
                m = pattern.search(text)
                if m:
                    content = (m.group(1) or text).strip().strip("。.，,")
                    if not content:
                        continue
                    candidates.append(MemoryCandidate(
                        candidate_id=f"cand_{uuid.uuid4().hex[:16]}",
                        operation="add",
                        memory_type="profile",
                        scope=scope,
                        scope_id=scope_id,
                        content=content[:1000],
                        confidence=0.9,
                        importance=0.8,
                        source_event_ids=[event_id],
                        reason="explicit_user_request",
                    ))
                    break

        # 2. 工具稳定事实（assistant/tool 事件含确认信号）
        if event_type in ("tool_result", "assistant_message") and any(sig in text.lower() for sig in _FACT_SIGNALS):
            candidates.append(MemoryCandidate(
                candidate_id=f"cand_{uuid.uuid4().hex[:16]}",
                operation="add",
                memory_type="fact",
                scope=scope,
                scope_id=scope_id,
                content=text[:1000],
                confidence=0.7,
                importance=0.6,
                source_event_ids=[event_id],
                reason="tool_fact",
            ))
    return candidates


class MemoryExtractor:
    """方案 §9.2 的 ``MemoryExtractor.propose()`` 接口封装。"""

    def __init__(self, *, scope: MemoryScope = "user", scope_id: str = "") -> None:
        self._scope = scope
        self._scope_id = scope_id

    def propose(self, events: Sequence[any]) -> list[MemoryCandidate]:  # type: ignore[name-defined]
        return propose_memory_candidates(events, scope=self._scope, scope_id=self._scope_id)


__all__ = ["MemoryExtractor", "propose_memory_candidates"]
