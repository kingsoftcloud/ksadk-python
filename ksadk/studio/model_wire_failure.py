"""Host-owned failure facts, without provider messages or hidden retry policy."""

from collections.abc import Mapping

from ksadk._model_input import ProviderInputObservation
from ksadk.studio.contracts import Usage
from ksadk.studio.errors import StudioError
from ksadk.studio.model_wire_identity import invalid_model_frame


class ModelResponseError(StudioError):
    """Private response accounting survives a public, safe decoder rejection."""

    def __init__(self, error: StudioError, usage: Usage | None = None) -> None:
        super().__init__(error.code, error.message, status_code=error.status_code,
                         field=error.field, details=dict(error.details))
        self._response_usage = usage.model_copy(deep=True) if usage is not None else None
        self.headers = getattr(error, "headers", None)
        self.provider_input: ProviderInputObservation | None = None


def response_error_usage(error: Exception) -> Usage | None:
    # Neither public details nor an arbitrary plugin's attribute grants billing.
    if type(error) is ModelResponseError and error._response_usage is not None:
        return error._response_usage.model_copy(deep=True)
    return None


_CODES = {
    "context_length_exceeded": "MODEL_PROVIDER_CONTEXT_LENGTH",
    "context_window_exceeded": "MODEL_PROVIDER_CONTEXT_LENGTH",
    "model_context_exceeded": "MODEL_PROVIDER_CONTEXT_LENGTH",
    "model_context_window_exceeded": "MODEL_PROVIDER_CONTEXT_LENGTH",
    "invalid_api_key": "MODEL_PROVIDER_AUTHENTICATION",
    "authentication_error": "MODEL_PROVIDER_AUTHENTICATION",
    "permission_error": "MODEL_PROVIDER_PERMISSION",
    "invalid_request_error": "MODEL_PROVIDER_INVALID_REQUEST",
    "not_found_error": "MODEL_PROVIDER_NOT_FOUND",
    "rate_limit_exceeded": "MODEL_PROVIDER_RATE_LIMIT",
    "rate_limit_error": "MODEL_PROVIDER_RATE_LIMIT",
    "overloaded_error": "MODEL_PROVIDER_UNAVAILABLE",
    "server_error": "MODEL_PROVIDER_UNAVAILABLE",
    "api_error": "MODEL_PROVIDER_UNAVAILABLE",
    "service_unavailable_error": "MODEL_PROVIDER_UNAVAILABLE",
    "server_is_overloaded": "MODEL_PROVIDER_UNAVAILABLE",
    "timeout_error": "MODEL_PROVIDER_TIMEOUT",
    "request_too_large": "MODEL_PROVIDER_INVALID_REQUEST",
    "billing_error": "MODEL_PROVIDER_QUOTA_EXHAUSTED",
    "insufficient_quota": "MODEL_PROVIDER_QUOTA_EXHAUSTED",
    "credit_balance_exhausted": "MODEL_PROVIDER_QUOTA_EXHAUSTED",
    "organization_spend_limit_exceeded": "MODEL_PROVIDER_QUOTA_EXHAUSTED",
    "project_spend_limit_exceeded": "MODEL_PROVIDER_QUOTA_EXHAUSTED",
    "organization_usage_limit_exceeded": "MODEL_PROVIDER_QUOTA_EXHAUSTED",
}


def provider_error(
    payload: dict, *, wire_api: str, status: int = 200,
    usage: Usage | None = None, headers: Mapping[str, str] | None = None,
    provider_request_id: str = "",
) -> ModelResponseError | None:
    """Known structured codes precede HTTP; unknown 2xx errors remain terminal.

    An error alongside a purported successful answer is a protocol conflict,
    not a provider rejection that can authorize context recovery or retries.
    """
    error = payload.get("error")
    failed = wire_api == "responses" and payload.get("status") == "failed"
    if error is None and not failed and status < 400:
        return None
    if status < 400 and (
        (wire_api == "chat" and (payload.get("choices") or payload.get("message")))
        or (wire_api == "responses" and payload.get("status") == "completed")
        or (wire_api == "anthropic" and (
            payload.get("type") == "message" or "content" in payload or "stop_reason" in payload
        ))
    ):
        raise invalid_model_frame()
    if error is not None and not isinstance(error, dict):
        if status < 400:
            raise invalid_model_frame()
        error = None
    fields = error or {}
    for field in ("code", "type"):
        if fields.get(field) is not None and type(fields[field]) is not str:
            if status < 400:
                raise invalid_model_frame()
            fields = {}
            break
    # Only mapped codes become public facts. Text, arbitrary keys and vendor
    # numeric codes cannot select a policy or escape into diagnostics.
    code = next((_CODES[fields[field]] for field in ("code", "type")
                 if fields.get(field) in _CODES), None)
    if code is None:
        code = ("MODEL_RATE_LIMITED" if status == 429 else "MODEL_REQUEST_FAILED") \
            if status >= 400 else "MODEL_PROVIDER_ERROR"
    details = {"upstreamStatus": status}
    if provider_request_id:
        details["providerRequestId"] = provider_request_id
    message = (
        "所选生成模型当前限流，请稍后重试或切换模型 Profile"
        if code == "MODEL_RATE_LIMITED" else "模型提供方拒绝本次请求"
    )
    error = StudioError(
        code, message, status_code=429 if status == 429 else 502,
        details=details,
    )
    error.headers = {name: headers[name] for name in ("retry-after", "retry-after-ms")
                     if headers is not None and name in headers}
    return ModelResponseError(error, usage)
