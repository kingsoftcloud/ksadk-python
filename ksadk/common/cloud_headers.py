"""Shared source attribution for KsADK-managed cloud resource requests."""

from __future__ import annotations

import os
from collections.abc import Mapping

DEFAULT_TRAFFIC_SOURCE = "agentkit"
TRAFFIC_SOURCE_HEADER = "X-Traffic-Source"


def resolve_traffic_source(value: str | None = None) -> str:
    """Resolve an explicit snapshot or the process setting, with a stable default."""
    if value is None:
        value = os.getenv("KSADK_TRAFFIC_SOURCE", "")
    # Reject controls before trimming so even trailing CR/LF cannot enter headers.
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) > 126 for char in value):
        raise ValueError("Traffic source must contain printable ASCII characters only")
    return value.strip() or DEFAULT_TRAFFIC_SOURCE


def build_cloud_headers(
    headers: Mapping[str, str] | None = None, *, traffic_source: str | None = None
) -> dict[str, str]:
    """Copy headers and set one canonical source header before request signing.

    Source attribution is owned by KSADK_TRAFFIC_SOURCE (or an explicit worker
    snapshot), including when a caller supplies differently cased extra headers.
    """
    result = {
        name: value
        for name, value in (headers or {}).items()
        if name.lower() != TRAFFIC_SOURCE_HEADER.lower()
    }
    result[TRAFFIC_SOURCE_HEADER] = resolve_traffic_source(traffic_source)
    return result
