"""Prompt Cache 边界诊断（只记录 Hash，不持久化 Prompt 正文）。"""

from __future__ import annotations

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
    """

    def __init__(self, *, max_scopes: int = 1024) -> None:
        if max_scopes < 1:
            raise ValueError("max_scopes 必须为正整数")
        self._max_scopes = max_scopes
        self._hashes: OrderedDict[str, str] = OrderedDict()

    def observe(self, *, scope: str, stable_prompt_hash: str) -> PromptCacheDiagnostic:
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
            reason, cache_break = "stable_prompt_changed", True
        return PromptCacheDiagnostic(
            stable_prompt_hash=current,
            previous_stable_prompt_hash=previous,
            cache_break=cache_break,
            reason=reason,
        )


__all__ = ["PromptCacheDiagnostic", "PromptCacheTracker"]
