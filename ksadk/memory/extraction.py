"""Memory Candidate 抽取（方案 §9.2 / §10.3 / §10.4）。

压缩前从 ``groups_to_compact`` 的事件里确定性提取记忆候选。首期只做确定性提取，不调用模型
（方案 §9.3：优先确定性提取；模型辅助可关闭）：

- 用户显式"记住/remember/别忘了" → ``profile`` 候选（reason=explicit_user_request）。
- 工具返回的稳定事实（含 "确认/confirmed/最终/final" 字样）→ ``fact`` 候选（reason=tool_fact）。

提取结果交 ``MemoryPolicy.evaluate`` 评估；secret/PII、一次性当前任务状态、模型猜测由 Policy
拒绝（方案 §10.4）。本期不做 LLM 辅助抽取，避免把模型猜测写入长期记忆。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import replace
from types import SimpleNamespace
from typing import Sequence

from ksadk.memory.models import MemoryCandidate, MemoryScope

# 显式记忆意图（中英）。
_EXPLICIT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)记住[:：]?\s*(.+)"),
    re.compile(r"(?i)别忘了[:：]?\s*(.+)"),
    re.compile(r"(?i)remember\s+(?:that\s+)?(.+)", re.IGNORECASE),
    re.compile(r"(?i)请记[:：]?\s*(.+)"),
)
# A declared conversation/turn horizon belongs to Session history, not the
# cross-session Memory provider. Inspect the memory-bearing first statement:
# a later "For this turn reply ACK" only scopes the answer format.
_SESSION_RETENTION = re.compile(
    r"\b(?:for|during|within|in)\s+(?:only\s+)?(?:this|the\s+current)\s+"
    r"(?:conversation|chat|session|turn)\b"
    r"|(?:本次|这次|此次|当前|这个|本)(?:会话|对话|聊天)"
    r"|(?:本轮|这轮)",
    re.IGNORECASE,
)


def _session_only_memory(text: str) -> bool:
    statement = re.split(r"[。！？!?\r\n]|\.(?=\s|$)", text.strip(), maxsplit=1)[0]
    return _SESSION_RETENTION.search(statement) is not None


# 明确纠正同一偏好槽位。首期只覆盖语义边界清晰的动作型偏好，避免把
# “喜欢音乐”和“喜欢运动”等无关事实误判为冲突。
_PREFERENCE_CORRECTION = re.compile(
    r"(?P<prefix>(?:我|本人)?喜欢(?P<action>吃|喝|用|看|听|玩))"
    r"(?:的)?(?:是)?\s*(?P<new>.+?)\s*(?:，|,)?\s*(?:而)?不是\s*"
    r"(?P<old>.+?)(?:[。.!！]|$)",
    re.IGNORECASE,
)
_PREFERENCE_SLOT = re.compile(r"(?:我|本人)?喜欢(?P<action>吃|喝|用|看|听|玩)")
# 一般祈使纠偏（"以后报表不要用英文，请改成中文"）：语义边界不如动作型偏好
# 清晰，但"否定旧值 + 指定新值"结构明确，仍按显式纠错（update）处理。
_GENERAL_CORRECTION = re.compile(
    r"(?:以后|今后)?(?P<subject>[一-龥A-Za-z0-9]{1,12})?"
    r"(?:请)?(?:不要|别)(?P<old>.{1,20}?)[，,]?"
    r"(?:请)?(?:改成|改为|换成|改用)(?P<new>.+?)(?:[。.!！]|$)",
    re.IGNORECASE,
)
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
_FACT_SIGNALS = (
    "confirmed",
    "最终确认",
    "final",
    "verified",
    "确认成功",
    "查询成功",
    "已认证",
    "已核实",
)


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


def is_explicit_memory_instruction(text: str) -> bool:
    """Recognize a complete durable instruction, not a quoted substring.

    Candidate extraction intentionally searches larger transcripts. A host
    granting an immediate explicit write needs the narrower whole-input test.
    Unsupported wording can use the host's exact-call consent path; this is
    not a general semantic intent classifier.
    """
    text = text.strip()
    if _session_only_memory(text):
        return False
    if any(pattern.fullmatch(text) for pattern in _EXPLICIT_PATTERNS):
        return True
    if _PREFERENCE_CORRECTION.fullmatch(text) or _GENERAL_CORRECTION.fullmatch(text):
        return True
    hobby = _HOBBY_DECLARATION.fullmatch(text)
    return bool(hobby and hobby.group("correction"))


def propose_memory_candidates(
    events: Sequence[any],  # type: ignore[name-defined]
    *,
    scope: MemoryScope = "user",
    scope_id: str = "",
    max_content_chars: int | None = 1000,
) -> list[MemoryCandidate]:
    """从待压缩事件提取记忆候选（方案 §9.2）。

    纯确定性、无 LLM。返回候选列表交 Coordinator flush；Policy 决定 commit/reject。
    """
    candidates: list[MemoryCandidate] = []
    if max_content_chars is not None and (
        type(max_content_chars) is not int or max_content_chars < 1
    ):
        raise ValueError("memory candidate limit must be a positive integer or None")

    def bounded(content):
        return content if max_content_chars is None else content[:max_content_chars]
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
            if _session_only_memory(text):
                continue
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
                            content=bounded(content),
                            confidence=0.95,
                            importance=0.9,
                            source_event_ids=[event_id],
                            slot_key=derive_profile_slot_key(content),
                            reason="explicit_user_correction",
                        )
                    )
                    continue
            general = _GENERAL_CORRECTION.search(text)
            if general:
                subject = (general.group("subject") or "").strip()
                new_value = general.group("new").strip().strip("。.，, ")
                if new_value:
                    content = f"{subject}改为{new_value}" if subject else f"改为{new_value}"
                    candidates.append(
                        MemoryCandidate(
                            candidate_id=f"cand_{uuid.uuid4().hex[:16]}",
                            operation="update",
                            memory_type="profile",
                            scope=scope,
                            scope_id=scope_id,
                            content=bounded(content),
                            confidence=0.9,
                            importance=0.8,
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
                            content=bounded(content),
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
                            content=bounded(content),
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
                            content=bounded(content),
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
                    content=bounded(text),
                    confidence=0.7,
                    importance=0.6,
                    source_event_ids=[event_id],
                    slot_key="",
                    reason="tool_fact",
                )
            )
    return candidates


def explicit_memory_target(text: str) -> tuple[str, str] | None:
    """Decode one whole explicit goal without truncating its requested fact.

    This is a pure intent interpretation. The synthetic extraction input never
    supplies persisted provenance or write authorization; those remain the
    host's actual goal, approval and Run event identities.
    """
    if not is_explicit_memory_instruction(text):
        return None
    candidates = propose_memory_candidates(
        (SimpleNamespace(text=text, author="user", event_type="user_message"),),
        max_content_chars=None,
    )
    explicit = [candidate for candidate in candidates
                if candidate.reason.startswith("explicit_user")]
    if len(explicit) != 1:
        return None
    return explicit[0].operation, explicit[0].content


NATIVE_USER_EXTRACTION_POLICY = "native-original-user-profile/v2"


def native_user_memory_candidate(
    text: str, *, source_event_id: str, scope: MemoryScope, scope_id: str,
) -> MemoryCandidate | None:
    """Interpret a complete original user statement, never a transcript substring.

    The host must supply canonical user input and its durable source identity.
    Confidence describes known user authorship, not the truth of a preference
    or permission to persist it. The existing Policy still requires independent
    observations for implicit declarations. No model/summary/tool text is admitted.
    """
    text = text.strip()
    explicit = is_explicit_memory_instruction(text)
    implicit = any(pattern.fullmatch(text) for pattern in _IMPLICIT_PREFERENCE_PATTERNS)
    hobby = _HOBBY_DECLARATION.fullmatch(text)
    if not source_event_id.strip() or not (explicit or implicit or hobby):
        return None
    proposed = propose_memory_candidates(
        (SimpleNamespace(text=text, author="user", event_type="user_message"),),
        scope=scope, scope_id=scope_id, max_content_chars=None,
    )
    if not proposed and hobby and not hobby.group("correction"):
        proposed = [MemoryCandidate(
            candidate_id="pending", operation="add", memory_type="profile", scope=scope,
            scope_id=scope_id, content=f"我的爱好是{hobby.group('value').strip()}",
            confidence=1, importance=0.65, source_event_ids=[source_event_id],
            reason="implicit_user_preference", slot_key="profile.preference.hobby",
        )]
    if len(proposed) != 1 or (not explicit and proposed[0].reason != "implicit_user_preference"):
        return None
    value = proposed[0]
    identity = json.dumps((NATIVE_USER_EXTRACTION_POLICY, scope, scope_id, source_event_id,
                           value.operation, value.content), ensure_ascii=False)
    return replace(value, candidate_id="candidate_" + hashlib.sha256(identity.encode()).hexdigest(),
                   confidence=1, source_event_ids=[source_event_id])


class MemoryExtractor:
    """方案 §9.2 的 ``MemoryExtractor.propose()`` 接口封装。"""

    def __init__(self, *, scope: MemoryScope = "user", scope_id: str = "") -> None:
        self._scope = scope
        self._scope_id = scope_id

    def propose(self, events: Sequence[any]) -> list[MemoryCandidate]:  # type: ignore[name-defined]
        return propose_memory_candidates(events, scope=self._scope, scope_id=self._scope_id)


__all__ = [
    "MemoryExtractor", "derive_profile_slot_key", "explicit_memory_target",
    "is_explicit_memory_instruction", "propose_memory_candidates",
    "native_user_memory_candidate", "NATIVE_USER_EXTRACTION_POLICY",
]
