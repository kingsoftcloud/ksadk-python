"""TokenCounter 协议与启发式实现。

对齐方案 8.9。实现顺序应是 provider 官方 tokenizer → 兼容 tokenizer → CJK+ASCII
启发式。第一个 PR 只落地启发式实现（复用现有 ``estimate_text_tokens``），并记录所用
tokenizer 名称；只有 heuristic 可用时由调用方自行加安全系数。tiktoken/provider
tokenizer 接入留后续 PR。
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence

HEURISTIC_TOKENIZER_NAME = "heuristic_cjk_ascii"


class TokenCounter(Protocol):
    """token 计数协议。"""

    name: str

    def count_text(self, text: str, *, model: str | None = None) -> int: ...

    def count_messages(self, messages: Sequence[Any], *, model: str | None = None) -> int: ...


class HeuristicTokenCounter:
    """复用 ``ksadk.conversations.model_context.estimate_text_tokens`` 的启发式计数器。

    CJK 字符按 1 token，其他按 4 chars ~= 1 token。不是真实 tokenizer，但比纯英文
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


def get_default_token_counter() -> TokenCounter:
    """返回进程级默认 TokenCounter（启发式）。

    provider 官方 tokenizer / 兼容 tokenizer 的切换留后续 PR；当前第一个 PR
    只用启发式估算做 shadow 可观测基线。
    """
    global _DEFAULT_COUNTER
    if _DEFAULT_COUNTER is None:
        _DEFAULT_COUNTER = HeuristicTokenCounter()
    return _DEFAULT_COUNTER
