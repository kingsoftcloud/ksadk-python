"""Shared AICP env parsing helpers."""

from __future__ import annotations

import os


DEFAULT_AICP_REGION = "cn-beijing-6"
DEFAULT_AICP_ENDPOINT = "aicp.api.ksyun.com"


def _get_nonempty_env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None

    value = value.strip()
    return value or None


def is_inner_aicp_endpoint(endpoint: str | None) -> bool:
    inner_endpoint = _get_nonempty_env("AICP_INNER_ENDPOINT")
    if not inner_endpoint:
        return False
    if not endpoint:
        return False

    normalized = endpoint.strip().lower()
    if "://" in normalized:
        normalized = normalized.split("://", 1)[1]
    normalized = normalized.split("/", 1)[0]
    return normalized == inner_endpoint.strip().lower()


def resolve_aicp_connection(
    env_prefix: str,
    *,
    default_region: str = DEFAULT_AICP_REGION,
    default_endpoint: str = DEFAULT_AICP_ENDPOINT,
    default_scheme: str = "https",
) -> dict[str, str]:
    endpoint = _get_nonempty_env(f"{env_prefix}_ENDPOINT")
    if endpoint is None:
        endpoint = default_endpoint
    scheme = _get_nonempty_env(f"{env_prefix}_SCHEME")
    if scheme is None:
        scheme = "http" if is_inner_aicp_endpoint(endpoint) else default_scheme

    region = (
        _get_nonempty_env(f"{env_prefix}_REGION")
        or _get_nonempty_env("KSYUN_REGION")
        or default_region
    )

    return {
        "endpoint": endpoint,
        "scheme": scheme,
        "region": region,
    }
