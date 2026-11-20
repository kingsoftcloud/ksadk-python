"""Shared source attribution for KsADK-managed cloud resource requests."""

from __future__ import annotations

import os
from collections.abc import Mapping

DEFAULT_TRAFFIC_SOURCE = "agentkit"
TRAFFIC_SOURCE_HEADER = "X-Traffic-Source"
COMPONENT_ENV_VAR = "KSADK_COMPONENT_ENV"


def resolve_traffic_source(value: str | None = None) -> str:
    """Resolve an explicit snapshot or the process setting, with a stable default."""
    if value is None:
        value = os.getenv("KSADK_TRAFFIC_SOURCE", "")
    # Reject controls before trimming so even trailing CR/LF cannot enter headers.
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) > 126 for char in value):
        raise ValueError("Traffic source must contain printable ASCII characters only")
    return value.strip() or DEFAULT_TRAFFIC_SOURCE


def resolve_component_env() -> str:
    """Resolve KSADK_COMPONENT_ENV for knowledge and memory routing."""
    return str(os.getenv(COMPONENT_ENV_VAR, "") or "").strip()


def build_cloud_headers(
    headers: Mapping[str, str] | None = None, *, traffic_source: str | None = None
) -> dict[str, str]:
    """Copy headers and set canonical source attribution before request signing.

    Source attribution is owned by KSADK_TRAFFIC_SOURCE (or an explicit worker
    snapshot), including when a caller supplies differently cased extra headers.
    """
    result: dict[str, str] = {}
    for name, value in (headers or {}).items():
        lower = name.lower()
        if lower == TRAFFIC_SOURCE_HEADER.lower():
            continue
        result[name] = value

    result[TRAFFIC_SOURCE_HEADER] = resolve_traffic_source(traffic_source)

    return result


def build_component_headers(
    headers: Mapping[str, str] | None = None,
    *,
    component_env: str,
    traffic_source: str | None = None,
) -> dict[str, str]:
    """Add the preproduction route only for knowledge and memory clients."""
    result = build_cloud_headers(headers, traffic_source=traffic_source)
    if component_env.strip().lower() == "pre":
        result = {
            name: value
            for name, value in result.items()
            if name.lower() != "x-ksc-custom-source"
        }
        result["X-KSC-CUSTOM-SOURCE"] = "pre"
    return result
