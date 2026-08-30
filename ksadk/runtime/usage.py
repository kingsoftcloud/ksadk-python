"""Canonical runtime usage projection helpers."""

from __future__ import annotations

from typing import Any, Mapping


def canonical_usage_payload(
    usage: Mapping[str, Any], *, runtime_type: str
) -> dict[str, Any]:
    """Normalize provider usage, including nested cache/reasoning details."""
    input_details = usage.get("input_token_details")
    output_details = usage.get("output_token_details")
    cached = (
        input_details.get("cached")
        if isinstance(input_details, Mapping)
        else usage.get("cached_tokens")
    )
    reasoning = (
        output_details.get("reasoning")
        if isinstance(output_details, Mapping)
        else usage.get("reasoning_tokens")
    )
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": int(usage.get("total_tokens") or input_tokens + output_tokens),
        "cached_tokens": int(cached or 0),
        "reasoning_tokens": int(reasoning or 0),
        "source": str(usage.get("source") or runtime_type),
    }
