"""Canonical streaming events emitted by every model transport.

This is a transport contract, not a UI contract. Credentials and raw chunks
do not cross it. Completed opaque provider content has a separate private
carrier, excluded from as_dict; runtimes validate it before model-only replay.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from ksadk._model_input import ProviderInputObservation


class ModelStreamEventType(str, Enum):
    TURN_STARTED = "turn.started"
    INPUT_TRANSFORMATIONS = "input.transformations"
    TEXT_DELTA = "text.delta"
    REASONING_DELTA = "reasoning.delta"
    PUBLIC_SUMMARY_DELTA = "thinking.summary.delta"
    TOOL_CALL_COMPLETED = "tool_call.completed"
    USAGE_UPDATED = "usage.updated"
    PROVIDER_RETRYING = "provider.retrying"
    PROVIDER_RATE_LIMITED = "provider.rate_limited"
    TURN_COMPLETED = "turn.completed"
    TURN_FAILED = "turn.failed"


@dataclass(frozen=True)
class ModelStreamEvent:
    """One normalized, secret-free model transport event."""

    type: ModelStreamEventType
    model: str
    sequence: int
    text: str = ""
    call_id: str = ""
    tool_name: str = ""
    arguments: str = ""
    usage: Mapping[str, int] | None = None
    failure_category: str = ""
    gateway_error_code: str = ""
    status_code: int | None = None
    provider_request_id: str = ""
    retry_after_ms: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    # Kept private when logging/serializing the public event vocabulary.
    provider_content: tuple[Mapping[str, Any], ...] | None = field(default=None, repr=False)
    provider_input: ProviderInputObservation | None = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": self.type.value,
            "model": self.model,
            "sequence": self.sequence,
        }
        for key in ("text", "call_id", "tool_name", "arguments"):
            value = getattr(self, key)
            if value:
                payload[key] = value
        if self.usage is not None:
            payload["usage"] = dict(self.usage)
        if self.failure_category:
            payload["failure_category"] = self.failure_category
        if self.gateway_error_code:
            payload["gateway_error_code"] = self.gateway_error_code
        if self.status_code is not None:
            payload["status_code"] = self.status_code
        if self.provider_request_id:
            payload["provider_request_id"] = self.provider_request_id
        if self.retry_after_ms is not None:
            payload["retry_after_ms"] = self.retry_after_ms
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


def classify_gateway_error(error: Exception) -> tuple[str, int | None, int | None]:
    """Return stable category/status/retry-after without leaking provider text."""

    from ksadk.harness.model_provider import (
        classify_model_failure,
        retry_after_ms_from_error,
    )

    classified = classify_model_failure(error)
    return classified.kind.value, classified.status_code, retry_after_ms_from_error(error)


def safe_gateway_error_code(error: Exception) -> str:
    """Expose only stable SDK error identifiers, never provider response text."""
    from ksadk.harness.model_provider import model_failure_source

    error = model_failure_source(error)
    code = getattr(error, "code", None)
    if (
        isinstance(code, str)
        and 0 < len(code) <= 64
        and code[0].isupper()
        and all(char.isupper() or char.isdigit() or char == "_" for char in code)
    ):
        return code
    return ""


__all__ = [
    "ModelStreamEvent",
    "ModelStreamEventType",
    "classify_gateway_error",
    "safe_gateway_error_code",
]
