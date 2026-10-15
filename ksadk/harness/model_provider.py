"""模型提供方失败分类与可审计执行决策。

分类优先读取结构化 HTTP/异常字段，字符串匹配只作为不同 SDK 间的兼容兜底。
本模块不依赖 LiteLLM，其他 Reasoner 也能遵守同一 Provider Policy。
"""

from __future__ import annotations

import math
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


_NON_RETRYABLE_RESPONSE_CODES = frozenset(
    {
        "MODEL_EMPTY_RESPONSE",
        "MODEL_TOOL_PROTOCOL_INVALID",
        "MODEL_REPETITIVE_OUTPUT",
        "MODEL_RESPONSE_INVALID",
        "MODEL_RESPONSE_TOO_LARGE",
    }
)
_MAX_FAILURE_CHAIN = 8
_STRUCTURED_PROVIDER_CODES = {
    "MODEL_PROVIDER_CONTEXT_LENGTH": ModelFailureKind.CONTEXT_LENGTH,
    "MODEL_PROVIDER_AUTHENTICATION": ModelFailureKind.AUTHENTICATION,
    "MODEL_PROVIDER_PERMISSION": ModelFailureKind.PERMISSION,
    "MODEL_PROVIDER_INVALID_REQUEST": ModelFailureKind.INVALID_REQUEST,
    "MODEL_PROVIDER_NOT_FOUND": ModelFailureKind.NOT_FOUND,
    "MODEL_PROVIDER_RATE_LIMIT": ModelFailureKind.RATE_LIMIT,
    "MODEL_PROVIDER_UNAVAILABLE": ModelFailureKind.UNAVAILABLE,
    "MODEL_PROVIDER_TIMEOUT": ModelFailureKind.TIMEOUT,
    # Keep the existing vocabulary: quota is a terminal client rejection, not
    # congestion. The stable gateway code preserves its more precise cause.
    "MODEL_PROVIDER_QUOTA_EXHAUSTED": ModelFailureKind.INVALID_REQUEST,
    "MODEL_PROVIDER_ERROR": ModelFailureKind.UNKNOWN,
}


def _explicit_failure_chain(error: Exception) -> tuple[Exception, ...]:
    chain: list[Exception] = []
    seen: set[int] = set()
    current = error
    while len(chain) < _MAX_FAILURE_CHAIN and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        cause = current.__cause__
        if not isinstance(cause, Exception):
            break
        current = cause
    return tuple(chain)


def _synthetic_gateway_status(error: Exception) -> bool:
    details = getattr(error, "details", None)
    return (
        getattr(error, "code", None) == "MODEL_REQUEST_FAILED"
        and _status_code(error) == 502
        and (not isinstance(details, dict) or details.get("upstreamStatus") is None)
        and isinstance(error.__cause__, Exception)
    )


def model_failure_source(error: Exception) -> Exception:
    """Select one explicit cause for classification and safe transport metadata.

    Inspired by ZCode's cycle-safe failure inspection. Prefer the nearest
    authoritative SDK rejection or HTTP status; a generic wrapper's text
    cannot hide a structured cause. Only the known MODEL_REQUEST_FAILED
    transport wrapper may yield its synthetic 502 to an explicit cause.
    Implicit __context__ may belong to another operation and is never used.
    This returns a host-local object, not public diagnostics or retry authority.
    """
    chain = _explicit_failure_chain(error)
    for item in chain:
        code = getattr(item, "code", None)
        if isinstance(code, str) and (
            code in _NON_RETRYABLE_RESPONSE_CODES or code in _STRUCTURED_PROVIDER_CODES
        ):
            return item
        status = _status_code(item)
        if status is not None and not _synthetic_gateway_status(item):
            return item
    for item in chain:
        if (not _synthetic_gateway_status(item)
                and _classify_model_failure_source(item).kind != ModelFailureKind.UNKNOWN):
            return item
    return chain[-1]


def model_failure_diagnostic(error: Exception) -> dict[str, str | int]:
    """Bounded cause identity, without messages, arbitrary names or tracebacks."""
    chain = _explicit_failure_chain(error)
    source = model_failure_source(error)
    # Only exact known runtime classes map to diagnostic codes. A plugin can
    # put credentials in its exception name as well as in its message.
    type_code = {
        ("builtins", "TimeoutError"): "timeout_error",
        ("builtins", "ConnectionError"): "connection_error",
        ("builtins", "RuntimeError"): "runtime_error",
        ("builtins", "ValueError"): "value_error",
        ("builtins", "TypeError"): "type_error",
        ("builtins", "AttributeError"): "attribute_error",
        ("builtins", "KeyError"): "key_error",
        ("json.decoder", "JSONDecodeError"): "json_decode_error",
        ("pydantic_core._pydantic_core", "ValidationError"): "validation_error",
        ("ksadk.studio.errors", "StudioError"): "studio_error",
        ("ksadk.studio.model_wire_failure", "ModelResponseError"): "studio_error",
        ("httpx", "HTTPStatusError"): "http_status_error",
        ("httpx", "ReadTimeout"): "timeout_error",
        ("httpx", "ConnectTimeout"): "timeout_error",
        ("httpx", "WriteTimeout"): "timeout_error",
        ("httpx", "PoolTimeout"): "timeout_error",
        ("httpx", "ConnectError"): "connection_error",
        ("httpx", "ReadError"): "connection_error",
        ("httpx", "WriteError"): "connection_error",
        ("httpx", "RemoteProtocolError"): "protocol_error",
    }.get((type(source).__module__, type(source).__name__), "other")
    return {
        "version": "model-failure-source/v1",
        "cause_depth": next(index for index, item in enumerate(chain) if item is source),
        "type_code": type_code,
    }


def classify_model_failure(error: Exception) -> ClassifiedModelFailure:
    return _classify_model_failure_source(model_failure_source(error))


def _classify_model_failure_source(error: Exception) -> ClassifiedModelFailure:
    """把厂商/SDK 异常归一化为稳定类别，不暴露异常对象或凭证。"""

    # A completed response with neither final text nor a tool call is a
    # deterministic semantic failure for the same prompt, not provider
    # congestion. Studio already performs one explicit repair request for
    # this condition; replaying it through the outer ten-attempt 429 policy
    # only leaves sub-agents looking stuck for minutes.
    #
    # 同一条纪律覆盖**全部"我们对响应的拒绝"**（2026-09-18 实测事故）：Studio 用
    # ``status_code=502`` 表示这一类自判失败（EMPTY_RESPONSE / TOOL_PROTOCOL_INVALID /
    # REPETITIVE_OUTPUT / RESPONSE_INVALID / RESPONSE_TOO_LARGE）。分类器只看 502 就
    # 归成 UNAVAILABLE，于是**确定性的校验失败被重试 10 次**：几何退避 1s→60s 合计
    # 约 150 秒纯 sleep，加上每次失败请求自身耗时，把"一个回合"拖成 4~5 分钟
    # （实测两次调用相隔 3m51s / 5m03s，而 TTFB 始终 5.5 秒），全场刷"模型服务繁忙
    # （unavailable）[502]"、15 分钟零产出。重放同一请求不会让响应变合法。
    if str(getattr(error, "code", "")) in _NON_RETRYABLE_RESPONSE_CODES:
        return ClassifiedModelFailure(ModelFailureKind.INVALID_REQUEST, 422)
    status_code = _status_code(error)
    code = getattr(error, "code", None)
    structured = _STRUCTURED_PROVIDER_CODES.get(code) if isinstance(code, str) else None
    if structured is not None:
        return ClassifiedModelFailure(structured, status_code)
    if status_code == 429:
        return ClassifiedModelFailure(ModelFailureKind.RATE_LIMIT, status_code)
    if status_code in {408, 504}:
        return ClassifiedModelFailure(ModelFailureKind.TIMEOUT, status_code)
    if status_code in {500, 502, 503, 529}:
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
    failover_allowed = policy.allow_failover and category in policy.failover_categories
    if total_attempt >= policy.total_attempt_budget:
        return ModelFailureAction.BUDGET_EXHAUSTED
    # The ten-attempt/60-second schedule is intentionally for provider
    # congestion (notably HTTP 429). A full request that has already consumed
    # the useful-progress timeout should not be replayed against the same model
    # when an ordered fallback is available: fail over immediately. Without a
    # fallback, retain one same-model retry so a one-off transport stall can
    # still recover without turning the timeout path into a multi-minute loop.
    if failure.kind in {ModelFailureKind.TIMEOUT, ModelFailureKind.UNAVAILABLE}:
        if failover_allowed and has_fallback:
            return ModelFailureAction.FAILOVER
        if failure.kind is ModelFailureKind.TIMEOUT and model_attempt >= 2:
            return ModelFailureAction.ABORT
    if retryable and model_attempt < policy.max_attempts_per_model:
        return ModelFailureAction.RETRY
    if failover_allowed and has_fallback:
        return ModelFailureAction.FAILOVER
    return ModelFailureAction.ABORT


def retry_delay_ms(policy: ModelProviderPolicy, *, model_attempt: int) -> int:
    """确定性渐进退避；最后一次重试达到上限，不引入随机抖动。

    ``model_attempt`` 是刚失败的本模型尝试序号。默认三次尝试对应两次
    等待，按几何曲线从 1 秒增加到 4 秒。整秒策略会对齐到整秒，
    让 Studio 的公开进度保持易读；毫秒级自定义策略仍保留其精度。
    """

    initial = policy.initial_backoff_ms
    maximum = policy.max_backoff_ms
    if initial <= 0 or maximum <= initial:
        return min(initial, maximum)

    retry_slots = max(1, policy.max_attempts_per_model - 1)
    index = min(max(0, model_attempt - 1), retry_slots - 1)
    if retry_slots == 1:
        delay = initial
    else:
        progress = index / (retry_slots - 1)
        delay = initial * ((maximum / initial) ** progress)

    quantum = 1_000 if initial >= 1_000 and maximum >= 1_000 else 1
    rounded = int(round(delay / quantum) * quantum)
    return min(max(initial, rounded), maximum)


def retry_after_ms_from_error(error: Exception) -> int | None:
    """限流响应的 Retry-After 头解析（G15.2）。

    优先 ``retry-after-ms``（毫秒），其次 ``retry-after``（数值秒）。头信息
    从异常本体或其 ``response`` 上读取（litellm/openai SDK 两种挂载位置）。
    非数值（如 HTTP 日期）或缺失时返回 None，调用方退回确定性退避。
    """

    error = model_failure_source(error)
    headers = getattr(error, "headers", None)
    if not headers:
        headers = getattr(getattr(error, "response", None), "headers", None)
    if headers is None:
        return None

    def _header(name: str) -> str | None:
        try:
            value = headers.get(name)
        except Exception:
            return None
        return str(value).strip() if value is not None else None

    raw_ms = _header("retry-after-ms")
    if raw_ms is not None:
        try:
            ms = float(raw_ms)
        except ValueError:
            ms = -1
        if math.isfinite(ms) and ms >= 0:
            return int(ms)
    raw_seconds = _header("retry-after")
    if raw_seconds is not None:
        try:
            seconds = float(raw_seconds)
        except ValueError:
            return None
        if math.isfinite(seconds) and seconds >= 0 and math.isfinite(seconds * 1000):
            return int(seconds * 1000)
    return None


def _status_code(error: Exception) -> int | None:
    """取用于分类的 HTTP 状态码。

    ⚠️ 优先取**真实的上游状态**（``details.upstreamStatus``），而不是我们包装后的
    状态码（2026-09-18 实测事故）：Studio 客户端把上游任何 >=400 统一包成
    ``MODEL_REQUEST_FAILED(status_code=502)``，只带
    ``details={"upstreamStatus": <真实值>}``。分类器若只看包装后的 502，就会把
    **上游 400（请求无效，本不该重试）判成 UNAVAILABLE 并重试 10 次**，界面表现为
    "模型服务繁忙"刷屏、全程零产出。用户据此质疑"服务器没那么容易繁忙吧"——而
    真实原因是**我们自己把不可重试的错误包装成了可重试的错误**。
    """

    details = getattr(error, "details", None)
    if isinstance(details, dict):
        try:
            upstream = details.get("upstreamStatus")
            if upstream is not None:
                return int(upstream)
        except (TypeError, ValueError):
            pass
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
    "model_failure_source",
    "model_failure_diagnostic",
    "decide_model_failure_action",
    "retry_after_ms_from_error",
    "retry_delay_ms",
    "safe_model_error_message",
]
