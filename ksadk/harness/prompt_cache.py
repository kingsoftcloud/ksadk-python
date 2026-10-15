"""Prompt Cache 边界诊断（只记录 Hash，不持久化 Prompt 正文）。"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass


@dataclass(frozen=True)
class PromptCacheDiagnostic:
    """一次稳定 Prompt 前缀观察结果。"""

    stable_prompt_hash: str
    previous_stable_prompt_hash: str
    cache_break: bool
    reason: str

    def to_payload(self) -> dict[str, str | bool]:
        return {
            "stable_prompt_hash": self.stable_prompt_hash,
            "previous_stable_prompt_hash": self.previous_stable_prompt_hash,
            "cache_break": self.cache_break,
            "reason": self.reason,
        }


class PromptCacheTracker:
    """进程内有界诊断器。

    Provider 的真实缓存命中仍以 ``usage.reported.cached_tokens`` 为准；本类只
    判断 KsADK 稳定 Prompt 合同是否发生变化。LRU 有界，重启后首个观察记为
    ``cold_start``，不会把本地缓存状态伪装成云端事实。
    G19：``notify_compaction``/``notify_cache_deletion`` 标记预期 break，
    observe 时跳过告警（reason 标 expected_*）。
    """

    def __init__(self, *, max_scopes: int = 1024) -> None:
        if max_scopes < 1:
            raise ValueError("max_scopes 必须为正整数")
        self._max_scopes = max_scopes
        self._hashes: OrderedDict[str, str] = OrderedDict()
        # G19 预期下降抑制：compaction / cache deletion 期间的 break 不告警
        self._expected_break: str | None = None

    def observe(
        self,
        *,
        scope: str,
        stable_prompt_hash: str,
        changed_section: str | None = None,
    ) -> PromptCacheDiagnostic:
        current = stable_prompt_hash.strip()
        previous = self._hashes.pop(scope, "")
        if current:
            self._hashes[scope] = current
            while len(self._hashes) > self._max_scopes:
                self._hashes.popitem(last=False)

        if not current:
            return PromptCacheDiagnostic(
                stable_prompt_hash="",
                previous_stable_prompt_hash=previous,
                cache_break=False,
                reason="stable_prompt_unavailable",
            )
        if not previous:
            reason, cache_break = "cold_start", False
        elif previous == current:
            reason, cache_break = "stable_prompt_reused", False
        else:
            # G17 强制理由：cache_break=True 须含具体变更 section key；
            # 未传则标 unknown（可见降级，不静默）。
            changed = changed_section or "unknown"
            # G19 预期下降抑制：compaction / cache deletion 期间的 break 不告警
            if self._expected_break is not None:
                reason, cache_break = self._expected_break, False
                self._expected_break = None
            else:
                reason, cache_break = f"stable_prompt_changed:{changed}", True
        return PromptCacheDiagnostic(
            stable_prompt_hash=current,
            previous_stable_prompt_hash=previous,
            cache_break=cache_break,
            reason=reason,
        )

    # G19 预期下降抑制：compaction / cache deletion 期间的 break 标预期
    def notify_compaction(self) -> None:
        self._expected_break = "expected_compaction"

    def notify_cache_deletion(self) -> None:
        self._expected_break = "expected_cache_deletion"


@dataclass(frozen=True)
class CacheBreakSnapshot:
    """G19 缓存失效归因：多维度定容快照（只存 hash，不存正文）。

    维度：instructions / system_sections / tool_set / model_ref。
    ``diff`` 返回发生变化的维度名集合，供 reason 定位。
    """

    instructions: str
    system_sections: str
    tool_set: str
    model_ref: str

    def __post_init__(self) -> None:
        # 定容：把传入正文替换为 hash（不存原文）
        object.__setattr__(self, "instructions", self._hash(self.instructions))
        object.__setattr__(self, "system_sections", self._hash(self.system_sections))
        object.__setattr__(self, "tool_set", self._hash(self.tool_set))

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    def diff(self, other: "CacheBreakSnapshot") -> list[str]:
        changed: list[str] = []
        if self.instructions != other.instructions:
            changed.append("instructions")
        if self.system_sections != other.system_sections:
            changed.append("system_sections")
        if self.tool_set != other.tool_set:
            changed.append("tool_set")
        if self.model_ref != other.model_ref:
            changed.append("model_ref")
        return changed


__all__ = ["CacheBreakSnapshot", "PromptCacheDiagnostic", "PromptCacheTracker"]
