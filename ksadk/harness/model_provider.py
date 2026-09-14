"""模型提供方失败分类与可审计执行决策。

分类优先读取结构化 HTTP/异常字段，字符串匹配只作为不同 SDK 间的兼容兜底。
本模块不依赖 LiteLLM，其他 Reasoner 也能遵守同一 Provider Policy。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from ksadk.harness.spec import ModelFailureCategory, ModelProviderPolicy


class ModelFailureKind(str, Enum):
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    TRANSPORT = "transport"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    INVALID_REQUEST = "invalid_request"
    CONTEXT_LENGTH = "context_length"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


class ModelFailureAction(str, Enum):
    RETRY = "retry_same_model"
    FAILOVER = "failover"
    RECOVER_CONTEXT = "recover_context"
    ABORT = "abort"
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True)
class ClassifiedModelFailure:
    kind: ModelFailureKind
    status_code: int | None = None

    @property
    def policy_category(self) -> ModelFailureCategory | None:
        try:
            return ModelFailureCategory(self.kind.value)
        except ValueError:
            return None


_SENSITIVE_ERROR_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE),
    re.compile(r"\bbearer\s+[^\s,;]+", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_-]?key|authorization|token|secret)\s*[:=]\s*[^\s,;]+",
        re.IGNORECASE,
    ),
)


def safe_model_error_message(error: Exception, *, limit: int = 256) -> str:
    """返回可进入事件/异常链的有界脱敏摘要。"""

    message = str(error)
    for pattern in _SENSITIVE_ERROR_PATTERNS:
        message = pattern.sub("***", message)
    return message[:limit]


def classify_model_failure(error: Exception) -> ClassifiedModelFailure:
    """把厂商/SDK 异常归一化为稳定类别，不暴露异常对象或凭证。"""

    status_code = _status_code(error)
    if status_code == 429:
        return ClassifiedModelFailure(ModelFailureKind.RATE_LIMIT, status_code)
    if status_code in {408, 504}:
        return ClassifiedModelFailure(ModelFailureKind.TIMEOUT, status_code)
    if status_code in {500, 502, 503}:
        return ClassifiedModelFailure(ModelFailureKind.UNAVAILABLE, status_code)
    if status_code == 401:
        return ClassifiedModelFailure(ModelFailureKind.AUTHENTICATION, status_code)
    if status_code == 403:
        return ClassifiedModelFailure(ModelFailureKind.PERMISSION, status_code)
    if status_code == 404:
        return ClassifiedModelFailure(ModelFailureKind.NOT_FOUND, status_code)
    name = type(error).__name__.lower()
    message = str(error).lower()
    combined = f"{name} {message}"
    # OpenAI-compatible providers commonly report context overflow as HTTP 400.
    # Inspect the structured/message semantics before the generic 4xx bucket;
    # otherwise the Harness cannot enter its one-shot emergency compaction path.
    if any(
        token in combined
        for token in (
            "context length",
            "maximum context",
            "context window",
            "too many tokens",
            "context_length_exceeded",
            # 真实网关的上下文溢出错误：HTTP 400 "input token limit is N"。
            "input token limit",
            "token limit exceeded",
            # 金山云 OpenAI-compatible 网关实测文案。
            "prompt exceeds max length",
            "prompt exceeds maximum length",
            "prompt is too long",
        )
    ):
        return ClassifiedModelFailure(ModelFailureKind.CONTEXT_LENGTH, status_code)
    if status_code is not None and 400 <= status_code < 500:
        return ClassifiedModelFailure(ModelFailureKind.INVALID_REQUEST, status_code)
    if any(token in combined for token in ("ratelimit", "rate limit", "too many requests")):
        kind = ModelFailureKind.RATE_LIMIT
    elif any(token in combined for token in ("timeout", "timed out", "deadline exceeded")):
        kind = ModelFailureKind.TIMEOUT
    elif any(
        token in combined
        for token in (
            "service unavailable",
            "temporarily unavailable",
            "unavailable",
            "provider 500",
            "overloaded",
        )
    ):
        kind = ModelFailureKind.UNAVAILABLE
    elif any(
        token in combined
        for token in ("connection", "transport", "network", "dns", "socket", "remoteprotocol")
    ):
        kind = ModelFailureKind.TRANSPORT
    elif any(token in combined for token in ("authentication", "unauthorized", "invalid api key")):
        kind = ModelFailureKind.AUTHENTICATION
    elif any(token in combined for token in ("permission", "forbidden")):
        kind = ModelFailureKind.PERMISSION
    elif any(token in combined for token in ("bad request", "invalid request", "invalid json")):
        kind = ModelFailureKind.INVALID_REQUEST
    elif "not found" in combined:
        kind = ModelFailureKind.NOT_FOUND
    else:
        kind = ModelFailureKind.UNKNOWN
    return ClassifiedModelFailure(kind, status_code)


def decide_model_failure_action(
    failure: ClassifiedModelFailure,
    *,
    policy: ModelProviderPolicy,
    model_attempt: int,
    total_attempt: int,
    has_fallback: bool,
) -> ModelFailureAction:
    """根据分类、当前尝试与预算作出唯一动作。"""

    # 相同的超长输入切换 Provider/模型通常仍会失败。Context Engine 必须先
    # 重新规划并紧急压缩；是否重试由默认 Agent Loop 的一次性门禁决定。
    if failure.kind is ModelFailureKind.CONTEXT_LENGTH:
        return ModelFailureAction.RECOVER_CONTEXT
    category = failure.policy_category
    if category is None:
        return ModelFailureAction.ABORT
    retryable = category in policy.retryable_categories
    failover_allowed = category in policy.failover_categories
    if total_attempt >= policy.total_attempt_budget:
        return ModelFailureAction.BUDGET_EXHAUSTED
    if retryable and model_attempt < policy.max_attempts_per_model:
        return ModelFailureAction.RETRY
    if failover_allowed and has_fallback:
        return ModelFailureAction.FAILOVER
    return ModelFailureAction.ABORT


def retry_delay_ms(policy: ModelProviderPolicy, *, model_attempt: int) -> int:
    """确定性指数退避；不加随机数，保证重放与测试可解释。"""

    delay = policy.initial_backoff_ms * (2 ** max(0, model_attempt - 1))
    return min(delay, policy.max_backoff_ms)


def _status_code(error: Exception) -> int | None:
    for value in (
        getattr(error, "status_code", None),
        getattr(error, "http_status", None),
        getattr(getattr(error, "response", None), "status_code", None),
    ):
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


__all__ = [
    "ClassifiedModelFailure",
    "ModelFailureAction",
    "ModelFailureKind",
    "classify_model_failure",
    "decide_model_failure_action",
    "retry_delay_ms",
    "safe_model_error_message",
]
