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
from typing import Sequence

from ksadk.memory.models import MemoryCandidate, MemoryScope

# 显式记忆意图（中英）。
_EXPLICIT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)记住[:：]?\s*(.+)"),
    re.compile(r"(?i)别忘了[:：]?\s*(.+)"),
    re.compile(r"(?i)remember\s+(?:that\s+)?(.+)", re.IGNORECASE),
    re.compile(r"(?i)请记[:：]?\s*(.+)"),
)
# 明确纠正同一偏好槽位。首期只覆盖语义边界清晰的动作型偏好，避免把
# “喜欢音乐”和“喜欢运动”等无关事实误判为冲突。
_PREFERENCE_CORRECTION = re.compile(
    r"(?P<prefix>(?:我|本人)?喜欢(?P<action>吃|喝|用|看|听|玩))"
    r"(?:的)?(?:是)?\s*(?P<new>.+?)\s*(?:，|,)?\s*(?:而)?不是\s*"
    r"(?P<old>.+?)(?:[。.!！]|$)",
    re.IGNORECASE,
)
_PREFERENCE_SLOT = re.compile(r"(?:我|本人)?喜欢(?P<action>吃|喝|用|看|听|玩)")
_HOBBY_DECLARATION = re.compile(
    r"(?:我|本人)?的?爱好(?P<correction>其实|现在|改)?(?:是|改成|变成)\s*(?P<value>.+?)"
    r"(?:[。.!！]|$)",
    re.IGNORECASE,
)
_IMPLICIT_PREFERENCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:我|本人)?的?偏好(?:是|为)\s*(.+?)(?:[。.!！]|$)", re.IGNORECASE),
    re.compile(
        r"(?:我|本人)?(?:平时)?(?:喜欢|习惯)(吃|喝|用|看|听|玩)\s*(.+?)(?:[。.!！]|$)",
        re.IGNORECASE,
    ),
)
# 工具稳定事实信号。
_FACT_SIGNALS = ("confirmed", "最终确认", "final", "verified", "确认成功")


def _event_text(event: any) -> str:  # type: ignore[name-defined]
    try:
        from ksadk.conversations.context import extract_event_text

        return extract_event_text(event)
    except Exception:  # noqa: BLE001
        return str(getattr(event, "text", "") or "")


def derive_profile_slot_key(content: str) -> str:
    """为边界明确的可变偏好生成稳定槽位；无法确定时返回空串。"""
    if _HOBBY_DECLARATION.search(str(content or "")):
        return "profile.preference.hobby"
    match = _PREFERENCE_SLOT.search(str(content or ""))
    if not match:
        return ""
    action = match.group("action")
    labels = {
        "吃": "food",
        "喝": "drink",
        "用": "tool",
        "看": "viewing",
        "听": "listening",
        "玩": "activity",
    }
    return f"profile.preference.{labels[action]}"


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
            hobby = _HOBBY_DECLARATION.search(text)
            if hobby and hobby.group("correction"):
                new_value = hobby.group("value").strip().strip("。.，, ")
                if new_value:
                    content = f"我的爱好是{new_value}"
                    candidates.append(
                        MemoryCandidate(
                            candidate_id=f"cand_{uuid.uuid4().hex[:16]}",
                            operation="update",
                            memory_type="profile",
                            scope=scope,
                            scope_id=scope_id,
                            content=content[:1000],
                            confidence=0.95,
                            importance=0.9,
                            source_event_ids=[event_id],
                            slot_key=derive_profile_slot_key(content),
                            reason="explicit_user_correction",
                        )
                    )
                    continue
            correction = _PREFERENCE_CORRECTION.search(text)
            if correction:
                prefix = correction.group("prefix").strip()
                new_value = correction.group("new").strip().strip("。.，, ")
                if new_value:
                    content = f"{prefix}{new_value}"
                    candidates.append(
                        MemoryCandidate(
                            candidate_id=f"cand_{uuid.uuid4().hex[:16]}",
                            operation="update",
                            memory_type="profile",
                            scope=scope,
                            scope_id=scope_id,
                            content=content[:1000],
                            confidence=0.95,
                            importance=0.9,
                            source_event_ids=[event_id],
                            slot_key=derive_profile_slot_key(content),
                            reason="explicit_user_correction",
                        )
                    )
                    continue
            for pattern in _EXPLICIT_PATTERNS:
                m = pattern.search(text)
                if m:
                    content = (m.group(1) or text).strip().strip("。.，,")
                    if not content:
                        continue
                    candidates.append(
                        MemoryCandidate(
                            candidate_id=f"cand_{uuid.uuid4().hex[:16]}",
                            operation="add",
                            memory_type="profile",
                            scope=scope,
                            scope_id=scope_id,
                            content=content[:1000],
                            confidence=0.9,
                            importance=0.8,
                            source_event_ids=[event_id],
                            slot_key=derive_profile_slot_key(content),
                            reason="explicit_user_request",
                        )
                    )
                    break
            else:
                # 隐式偏好只生成低置信候选；MemoryPolicy 仍要求达到观察次数阈值，
                # explicit_only 模式也会过滤它，避免一次闲聊直接成为长期事实。
                for pattern in _IMPLICIT_PREFERENCE_PATTERNS:
                    match = pattern.search(text)
                    if not match:
                        continue
                    content = match.group(0).strip().strip("。.，,")
                    candidates.append(
                        MemoryCandidate(
                            candidate_id=f"cand_{uuid.uuid4().hex[:16]}",
                            operation="add",
                            memory_type="profile",
                            scope=scope,
                            scope_id=scope_id,
                            content=content[:1000],
                            confidence=0.75,
                            importance=0.65,
                            source_event_ids=[event_id],
                            slot_key=derive_profile_slot_key(content),
                            reason="implicit_user_preference",
                        )
                    )
                    break

        # 2. 工具稳定事实（assistant/tool 事件含确认信号）
        if event_type in ("tool_result", "assistant_message") and any(
            sig in text.lower() for sig in _FACT_SIGNALS
        ):
            candidates.append(
                MemoryCandidate(
                    candidate_id=f"cand_{uuid.uuid4().hex[:16]}",
                    operation="add",
                    memory_type="fact",
                    scope=scope,
                    scope_id=scope_id,
                    content=text[:1000],
                    confidence=0.7,
                    importance=0.6,
                    source_event_ids=[event_id],
                    slot_key="",
                    reason="tool_fact",
                )
            )
    return candidates


class MemoryExtractor:
    """方案 §9.2 的 ``MemoryExtractor.propose()`` 接口封装。"""

    def __init__(self, *, scope: MemoryScope = "user", scope_id: str = "") -> None:
        self._scope = scope
        self._scope_id = scope_id

    def propose(self, events: Sequence[any]) -> list[MemoryCandidate]:  # type: ignore[name-defined]
        return propose_memory_candidates(events, scope=self._scope, scope_id=self._scope_id)


__all__ = ["MemoryExtractor", "derive_profile_slot_key", "propose_memory_candidates"]
