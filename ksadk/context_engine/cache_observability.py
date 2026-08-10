"""Prompt Cache 失效诊断（方案 7.5）。

KsADK 不实现通用 Completion Cache（ADR-013）。本模块只消费 Provider/Runtime 返回的
prompt cache usage，诊断稳定前缀是否意外失效：

- AgentVersion/模型/稳定 section/projection version 变化 → ``expected_invalidation``。
- 稳定前缀未变但 cache_read 大幅下降（cache_creation>0 且 cache_read≈0）→ 疑似
  ``unexpected_break``。
- 无 runtime usage 或无稳定前缀 → ``opaque`` / ``no_cache_info``，不推断命中率。

PR2 只落地诊断原语 + 把 raw 信号记到 span；跨 turn 的"大幅下降"需要历史 hash，本 PR 用
进程内 best-effort 的"上一稳定前缀"记录（见 ``CacheBreakRegistry``），pod 重启后清空，
精度如实标注。完成正式跨 session 历史留后续 PR。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ksadk.context_engine.capabilities import ContextAccuracy

CacheBreakStatus = str
"""``cached`` / ``expected_invalidation`` / ``unexpected_break``
    / ``no_cache_info`` / ``opaque``."""


@dataclass(frozen=True)
class CacheBreakDiagnosis:
    """单次请求的 prompt cache 失效诊断结果。"""

    status: CacheBreakStatus
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    stable_prefix_hash: str = ""
    previous_stable_prefix_hash: str = ""
    unexpected_break: bool = False
    expected_invalidation: bool = False
    break_reason: str = ""
    accounting_accuracy: ContextAccuracy = "opaque"
    metadata: dict[str, Any] = field(default_factory=dict)


def _extract_cache_tokens(usage: Mapping[str, Any] | None) -> tuple[int, int]:
    """从 runtime usage 取 (cache_read_tokens, cache_creation_tokens)。

    兼容 OpenAI（prompt_tokens_details.cached_tokens / cache_read）与 Anthropic
    （cache_read_input_tokens / cache_creation_input_tokens）两种字段命名。
    """
    if not isinstance(usage, Mapping):
        return 0, 0
    cache_read = 0
    cache_creation = 0

    # Anthropic 风格顶层字段
    for read_key in ("cache_read_input_tokens", "cache_read_tokens"):
        value = usage.get(read_key)
        if value is not None:
            try:
                cache_read = max(cache_read, int(value))
            except (TypeError, ValueError):
                pass
    for creation_key in ("cache_creation_input_tokens", "cache_creation_tokens"):
        value = usage.get(creation_key)
        if value is not None:
            try:
                cache_creation = max(cache_creation, int(value))
            except (TypeError, ValueError):
                pass

    # OpenAI / 通用 input_token_details.cached
    input_details = usage.get("input_token_details") or usage.get("input_tokens_details")
    if isinstance(input_details, Mapping):
        cached = (
            input_details.get("cached_tokens")
            or input_details.get("cached")
            or input_details.get("cache_read")
        )
        if cached is not None:
            try:
                cache_read = max(cache_read, int(cached))
            except (TypeError, ValueError):
                pass
    prompt_details = usage.get("prompt_tokens_details")
    if isinstance(prompt_details, Mapping):
        cached = prompt_details.get("cached_tokens")
        if cached is not None:
            try:
                cache_read = max(cache_read, int(cached))
            except (TypeError, ValueError):
                pass
    return cache_read, cache_creation


# 阈值：稳定前缀未变但 cache_read 低于此比例 + 本轮有 cache_creation → 疑似 unexpected break。
_UNEXPECTED_CACHE_READ_RATIO = 0.10


def diagnose_cache_break(
    *,
    stable_prefix_hash: str,
    previous_stable_prefix_hash: str | None,
    usage: Mapping[str, Any] | None,
    accounting_accuracy: ContextAccuracy = "opaque",
    expected_invalidation_signal: bool = False,
) -> CacheBreakDiagnosis:
    """诊断本次请求的 prompt cache 失效情况（方案 7.5）。

    Args:
        stable_prefix_hash: 本轮稳定前缀 hash（来自 ``CompiledPrompt``）。
        previous_stable_prefix_hash: 上一轮稳定前缀 hash（None 表示无历史/首次）。
        usage: Runtime/Provider 返回的 usage mapping（含 cache_read/creation）。
        accounting_accuracy: 该 Runner 的 token 观测精度。
        expected_invalidation_signal: 调用方已知本轮发生了版本/模型/projection 变化。
    """
    cache_read, cache_creation = _extract_cache_tokens(usage)

    # opaque：Runner 不暴露可靠 usage（方案 6.3）。
    if accounting_accuracy == "opaque":
        return CacheBreakDiagnosis(
            status="opaque",
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            stable_prefix_hash=stable_prefix_hash,
            accounting_accuracy=accounting_accuracy,
        )

    # 无稳定前缀 → 无法判断是否意外失效，只记录 raw 信号。
    if not stable_prefix_hash:
        return CacheBreakDiagnosis(
            status="no_cache_info",
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            stable_prefix_hash=stable_prefix_hash,
            accounting_accuracy=accounting_accuracy,
            break_reason="no stable prefix to diagnose",
        )

    # 无 runtime usage → runtime_reported/estimated 都可能拿不到 cache 字段。
    if cache_read == 0 and cache_creation == 0 and not isinstance(usage, Mapping):
        return CacheBreakDiagnosis(
            status="no_cache_info",
            stable_prefix_hash=stable_prefix_hash,
            accounting_accuracy=accounting_accuracy,
            break_reason="no runtime usage reported",
        )

    hash_changed = (
        bool(previous_stable_prefix_hash) and previous_stable_prefix_hash != stable_prefix_hash
    )
    if hash_changed or expected_invalidation_signal:
        return CacheBreakDiagnosis(
            status="expected_invalidation",
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            stable_prefix_hash=stable_prefix_hash,
            previous_stable_prefix_hash=previous_stable_prefix_hash or "",
            expected_invalidation=True,
            accounting_accuracy=accounting_accuracy,
            break_reason="stable prefix changed or explicit invalidation signal",
        )

    if cache_read > 0:
        # 稳定前缀未变且命中 → cached。
        return CacheBreakDiagnosis(
            status="cached",
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            stable_prefix_hash=stable_prefix_hash,
            previous_stable_prefix_hash=previous_stable_prefix_hash or "",
            accounting_accuracy=accounting_accuracy,
        )

    # 稳定前缀未变、无 cache_read、但有 cache_creation → 疑似 unexpected break。
    if cache_creation > 0 and previous_stable_prefix_hash == stable_prefix_hash:
        return CacheBreakDiagnosis(
            status="unexpected_break",
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            stable_prefix_hash=stable_prefix_hash,
            previous_stable_prefix_hash=previous_stable_prefix_hash or "",
            unexpected_break=True,
            accounting_accuracy=accounting_accuracy,
            break_reason="stable prefix unchanged but cache created instead of read",
        )

    return CacheBreakDiagnosis(
        status="no_cache_info",
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        stable_prefix_hash=stable_prefix_hash,
        previous_stable_prefix_hash=previous_stable_prefix_hash or "",
        accounting_accuracy=accounting_accuracy,
        break_reason="no decisive cache signal",
    )


class CacheBreakRegistry:
    """进程内 best-effort 的"上一稳定前缀"记录，按 session 维度。

    用于跨 turn 检测稳定前缀是否变化。仅存活于进程内，pod 重启后清空；精度如实标注为
    ``estimated``/``runtime_reported``（由调用方传入），不伪装成持久事实源。
    """

    def __init__(self, *, limit: int = 1024) -> None:
        self._limit = limit
        self._store: dict[str, str] = {}

    def previous(self, session_id: str) -> str | None:
        return self._store.get(session_id)

    def record(self, session_id: str, stable_prefix_hash: str) -> None:
        if not session_id or not stable_prefix_hash:
            return
        if session_id not in self._store and len(self._store) >= self._limit:
            # 简单淘汰：丢一个最早的（dict 保序）。不做 LRU 复杂度。
            self._store.pop(next(iter(self._store)))
        self._store[session_id] = stable_prefix_hash

    def clear(self) -> None:
        self._store.clear()


_DEFAULT_REGISTRY = CacheBreakRegistry()


def get_default_cache_break_registry() -> CacheBreakRegistry:
    return _DEFAULT_REGISTRY
