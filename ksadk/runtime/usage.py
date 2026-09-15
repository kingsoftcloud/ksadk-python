"""Canonical runtime usage projection helpers."""

from __future__ import annotations

from typing import Any, Mapping


def _usage_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _details(usage: Mapping[str, Any], *keys: str) -> Mapping[str, Any]:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def _first_present_int(*values: Any) -> int:
    for value in values:
        if value is not None:
            return _usage_int(value)
    return 0


def canonical_usage_payload(usage: Mapping[str, Any], *, runtime_type: str) -> dict[str, Any]:
    """Normalize provider usage, including nested cache/reasoning details."""
    input_details = _details(
        usage,
        "input_token_details",
        "input_tokens_details",
        "prompt_tokens_details",
    )
    output_details = _details(
        usage,
        "output_token_details",
        "output_tokens_details",
        "completion_tokens_details",
    )
    cached = _first_present_int(
        input_details.get("cached_tokens"),
        input_details.get("cached"),
        input_details.get("cache_read"),
        usage.get("cached_tokens"),
    )
    reasoning = _first_present_int(
        output_details.get("reasoning_tokens"),
        output_details.get("reasoning"),
        usage.get("reasoning_tokens"),
    )
    input_tokens = _first_present_int(usage.get("input_tokens"), usage.get("prompt_tokens"))
    output_tokens = _first_present_int(usage.get("output_tokens"), usage.get("completion_tokens"))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": _first_present_int(usage.get("total_tokens"), input_tokens + output_tokens),
        "cached_tokens": cached,
        "reasoning_tokens": reasoning,
        "source": str(usage.get("source") or runtime_type),
    }
