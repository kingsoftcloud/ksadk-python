"""Memory 写入策略、敏感信息拒绝与冲突解决（方案 §10.4 / §19）。

策略与阈值必须属于 ``MemoryPolicy``，不能硬编码在 Runner（方案 §10.4 末）。Candidate 进入
Provider 前必须执行 Secret/PII 检查；硬拒绝标签（api_key/secret_key/access_key/cookie/
auth_header/signed_url/dsn/token/binary）一律 ``reject``，不写入长期记忆（方案 §19）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Literal

from ksadk.memory.models import MemoryCandidate, MemoryOperation, MemoryRecord, SensitiveLabel

# 硬拒绝敏感标签：出现任一即 reject，绝不写入（方案 §19）。
HARD_REJECT_LABELS: frozenset[SensitiveLabel] = frozenset(
    {
        "api_key",
        "secret_key",
        "access_key",
        "cookie",
        "auth_header",
        "signed_url",
        "dsn",
        "token",
        "binary",
    }
)

PolicyDecision = Literal["commit", "pending", "reject"]


@dataclass(frozen=True)
class MemoryPolicyThresholds:
    """候选写入阈值（方案 §10.4 初始值）。

    阈值属于 Policy，不硬编码在 Runner。可由部署/配置覆盖。
    """

    explicit_user_request: float = 0.60
    verified_tool_fact: float = 0.80
    implicit_preference: float = 0.85
    implicit_preference_min_observations: int = 2
    episode_importance: float = 0.70


@dataclass(frozen=True)
class MemoryEvaluation:
    """对单个 Candidate 的策略判定结果。"""

    decision: PolicyDecision
    operation: MemoryOperation
    reason: str
    new_version: int | None = None
    conflicts_with: list[str] = field(default_factory=list)


# 敏感信息正则（best-effort，方案 §19）。只做写入前拦截，不做完整 DLP。
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], SensitiveLabel], ...] = (
    (re.compile(r"(?i)api[_-]?key\s*[:=]\s*\S+"), "api_key"),
    (re.compile(r"(?i)secret[_-]?key\s*[:=]\s*\S+"), "secret_key"),
    (re.compile(r"(?i)access[_-]?key\s*[:=]\s*\S+"), "access_key"),
    (re.compile(r"(?i)AKIA[0-9A-Z]{16}"), "access_key"),
    (re.compile(r"(?i)cookie\s*[:=]\s*\S+"), "cookie"),
    (re.compile(r"(?i)authorization\s*[:=]\s*bearer\s+\S+"), "auth_header"),
    (
        re.compile(r"https?://\S+?(?:X-Amz-Signature|X-Amz-Security-Token|signed)=", re.I),
        "signed_url",
    ),
    (re.compile(r"(?i)(postgres|mysql|mongodb|redis)://\S+:\S+@\S+"), "dsn"),
    (re.compile(r"(?i)sk-[A-Za-z0-9]{20,}"), "token"),
)


def detect_sensitive_labels(
    content: str, candidate_labels: list[SensitiveLabel]
) -> list[SensitiveLabel]:
    """对 Candidate 正文做敏感信息检测（方案 §19）。

    先采纳 Candidate 自带的 ``sensitive_labels``，再用正则做 best-effort 补检。任一硬拒绝
    标签命中即整体拒绝。
    """
    labels: set[SensitiveLabel] = set()
    for label in candidate_labels:
        if label and label != "none":
            labels.add(label)
    text = str(content or "")
    for pattern, label in _SECRET_PATTERNS:
        if pattern.search(text):
            labels.add(label)
    # 二进制特征：大量非文本/重复字节不做完整检测，仅按显式标签处理。
    return sorted(labels)


def _content_hash(content: str) -> str:
    return "sha256:" + hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MemoryPolicy:
    """写入策略与冲突解决（方案 §10.4）。

    无状态、可复用。``evaluate`` 不接触 Provider；commit 由 Coordinator 执行。
    """

    thresholds: MemoryPolicyThresholds = field(default_factory=MemoryPolicyThresholds)

    def evaluate(
        self,
        candidate: MemoryCandidate,
        *,
        existing: MemoryRecord | None = None,
        observations: int = 1,
    ) -> MemoryEvaluation:
        """评估单个 Candidate（方案 §10.4 决策表）。

        - 硬拒绝敏感标签 → ``reject``（绝不写入）。
        - 用户明确"记住"（reason 含 explicit）→ 同步 propose，达阈值 commit。
        - 用户明确"忘掉"（operation=delete）→ 解析目标删除；歧义时不猜测 → reject。
        - 一次性当前任务状态 / 模型猜测 → 留 Session，不写 → ``reject``
          （reason 标 ``not_durable``）。
        - 与旧事实冲突 → ``update``/``supersede``，不覆盖历史来源。
        """
        labels = detect_sensitive_labels(candidate.content, list(candidate.sensitive_labels))
        hard_hit = any(label in HARD_REJECT_LABELS for label in labels)
        if hard_hit or candidate.is_hard_rejected():
            return MemoryEvaluation(
                decision="reject",
                operation="ignore",
                reason=f"sensitive_label_rejected:{','.join(labels) or 'explicit'}",
                conflicts_with=list(candidate.conflicts_with),
            )

        if candidate.operation == "delete":
            # 删除需明确目标；reason 为空或含 "ambiguous" 时不猜测（方案 §10.4）。
            if not candidate.conflicts_with and not candidate.reason.strip():
                return MemoryEvaluation(
                    decision="reject",
                    operation="ignore",
                    reason="delete_without_target",
                )
            return MemoryEvaluation(
                decision="commit",
                operation="delete",
                reason="explicit_delete",
                conflicts_with=list(candidate.conflicts_with),
            )

        # 一次性当前任务状态 / 模型猜测不写长期记忆（方案 §10.4）。
        reason_lc = candidate.reason.lower()
        if "model_guess" in reason_lc or "transient" in reason_lc or "current_plan" in reason_lc:
            return MemoryEvaluation(
                decision="reject",
                operation="ignore",
                reason="not_durable",
            )

        # 阈值判定（方案 §10.4 初始阈值）。
        threshold = self._threshold_for(candidate, observations)
        if candidate.confidence < threshold:
            return MemoryEvaluation(
                decision="pending",
                operation=candidate.operation,
                reason=f"below_threshold:{candidate.confidence:.2f}<{threshold:.2f}",
            )

        # 冲突：update/supersede，不覆盖历史来源（方案 §10.4）。
        if existing is not None and candidate.operation != "add":
            return MemoryEvaluation(
                decision="commit",
                operation="update",
                reason="conflict_supersede",
                new_version=existing.version + 1,
                conflicts_with=[existing.memory_id],
            )

        return MemoryEvaluation(
            decision="commit",
            operation=candidate.operation,
            reason="threshold_met",
            new_version=1,
        )

    def _threshold_for(self, candidate: MemoryCandidate, observations: int) -> float:
        t = self.thresholds
        reason_lc = candidate.reason.lower()
        if "explicit" in reason_lc or "user_request" in reason_lc:
            return t.explicit_user_request
        if "tool_fact" in reason_lc or "verified" in reason_lc:
            return t.verified_tool_fact
        if candidate.memory_type == "episode":
            return t.episode_importance
        # implicit preference
        if observations < t.implicit_preference_min_observations:
            # 观察次数不足，抬高到 implicit 阈值且要求更多观察 → pending。
            return float("inf")
        return t.implicit_preference


def content_hash(content: str) -> str:
    """暴露给 Coordinator/Provider 的稳定 content hash。"""
    return _content_hash(content)


__all__ = [
    "HARD_REJECT_LABELS",
    "MemoryEvaluation",
    "MemoryPolicy",
    "MemoryPolicyThresholds",
    "PolicyDecision",
    "content_hash",
    "detect_sensitive_labels",
]
