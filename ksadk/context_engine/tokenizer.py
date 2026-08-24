"""TokenCounter 协议与启发式实现。

对齐方案 8.9。实现顺序应是 provider 官方 tokenizer → 兼容 tokenizer → CJK+ASCII
启发式。第一个 PR 只落地启发式实现（复用现有 ``estimate_text_tokens``），并记录所用
tokenizer 名称；只有 heuristic 可用时由调用方自行加安全系数。tiktoken/provider
tokenizer 接入留后续 PR。
"""

from __future__ import annotations

import os
from typing import Any, Protocol, Sequence

HEURISTIC_TOKENIZER_NAME = "heuristic_cjk_ascii"


class TokenCounter(Protocol):
    """token 计数协议。"""

    name: str

    def count_text(self, text: str, *, model: str | None = None) -> int: ...

    def count_messages(self, messages: Sequence[Any], *, model: str | None = None) -> int: ...


class HeuristicTokenCounter:
    """复用 ``ksadk.conversations.model_context.estimate_text_tokens`` 的启发式计数器。

    CJK 字符按约 1.5 token，其他按 4 chars ~= 1 token。不是真实 tokenizer，但比纯英文
    口径更接近本地中文使用体验。第一个 PR 的 shadow ContextPlan 只用它做可观测估算，
    不进任何决策路径。
    """

    name = HEURISTIC_TOKENIZER_NAME

    def count_text(self, text: str, *, model: str | None = None) -> int:
        from ksadk.conversations.model_context import estimate_text_tokens

        return estimate_text_tokens(text)

    def count_messages(self, messages: Sequence[Any], *, model: str | None = None) -> int:
        from ksadk.conversations.model_context import estimate_text_tokens

        total = 0
        for message in messages:
            total += self._count_message(message, estimate_text_tokens)
        return total

    @staticmethod
    def _count_message(message: Any, estimator: Any) -> int:
        if isinstance(message, str):
            return estimator(message)
        if isinstance(message, dict):
            total = 0
            for key in ("content", "text", "output"):
                value = message.get(key)
                if isinstance(value, str):
                    total += estimator(value)
                elif isinstance(value, list):
                    for part in value:
                        if isinstance(part, dict):
                            text = part.get("text") or part.get("content")
                            if isinstance(text, str):
                                total += estimator(text)
                        elif isinstance(part, str):
                            total += estimator(part)
            role = message.get("role")
            if isinstance(role, str):
                total += estimator(role)
            return total
        # LangChain/BaseMessage 风格对象：尽量取 content。
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return estimator(content)
        if isinstance(content, list):
            total = 0
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text") or part.get("content")
                    if isinstance(text, str):
                        total += estimator(text)
                elif isinstance(part, str):
                    total += estimator(part)
            return total
        return estimator(str(message))


_DEFAULT_COUNTER: HeuristicTokenCounter | None = None
_PROVIDER_COUNTER: "TokenCounter | None" = None


class _TiktokenTokenCounter:
    """tiktoken 兼容 tokenizer（方案 §8.9 实现顺序 2）。

    用于 OpenAI cl100k_base/o200k 系模型；非该系模型回退到 heuristic。``name`` 记录实际
    tokenizer，供 ContextPlan ``tokenizer`` 字段如实标注。
    """

    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        try:
            import tiktoken  # type: ignore

            self._enc = tiktoken.get_encoding(encoding_name)
            self._encoding_name = encoding_name
        except Exception:  # noqa: BLE001
            self._enc = None
            self._encoding_name = encoding_name

    @property
    def name(self) -> str:
        if self._enc is None:
            return HEURISTIC_TOKENIZER_NAME
        return f"tiktoken:{self._encoding_name}"

    def count_text(self, text: str, *, model: str | None = None) -> int:
        if self._enc is None:
            return HeuristicTokenCounter().count_text(text)
        try:
            return len(self._enc.encode(str(text or "")))
        except Exception:  # noqa: BLE001
            return HeuristicTokenCounter().count_text(text)

    def count_messages(self, messages: Sequence[Any], *, model: str | None = None) -> int:
        total = 0
        for message in messages:
            total += HeuristicTokenCounter._count_message(message, self.count_text)
        return total


def _provider_counter_enabled() -> bool:
    """是否启用 provider/兼容 tokenizer（方案 §8.9）。

    默认 **关闭**（保持 heuristic baseline，不静默改变既有计数口径——方案 §8.9 "先观测后接管"）；
    显式 ``KSADK_TOKENIZER_PROVIDER=auto|tiktoken`` 才尝试 tiktoken，不可用时回退 heuristic。
    """
    raw = str(os.environ.get("KSADK_TOKENIZER_PROVIDER", "") or "").strip().lower()
    return raw in ("auto", "tiktoken")


def get_default_token_counter() -> TokenCounter:
    """返回进程级默认 TokenCounter（方案 §8.9）。

    优先 provider/兼容 tokenizer（``KSADK_TOKENIZER_PROVIDER=auto`` 时尝试 tiktoken，不可用
    回退 heuristic）；``auto`` 之外显式 ``heuristic`` 则只用启发式。名称如实记录，偏差监控由
    调用方按 model 维度做（方案 §8.9 末）。
    """
    global _PROVIDER_COUNTER
    if _provider_counter_enabled() and _PROVIDER_COUNTER is None:
        _PROVIDER_COUNTER = _TiktokenTokenCounter()
    if _PROVIDER_COUNTER is not None and _provider_counter_enabled():
        return _PROVIDER_COUNTER
    global _DEFAULT_COUNTER
    if _DEFAULT_COUNTER is None:
        _DEFAULT_COUNTER = HeuristicTokenCounter()
    return _DEFAULT_COUNTER


def set_default_token_counter(counter: TokenCounter | None) -> None:
    """测试/注入用：覆盖默认 counter。``None`` 恢复自动解析。"""
    global _PROVIDER_COUNTER, _DEFAULT_COUNTER
    _PROVIDER_COUNTER = counter
    if counter is None:
        _DEFAULT_COUNTER = None
