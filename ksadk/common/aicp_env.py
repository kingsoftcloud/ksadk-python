"""Shared AICP env parsing helpers."""

from __future__ import annotations

import os
import socket


DEFAULT_AICP_REGION = "cn-beijing-6"
DEFAULT_AICP_ENDPOINT = "aicp.api.ksyun.com"
INTERNAL_AICP_ENDPOINT = "aicp.internal.api.ksyun.com"
INNER_AICP_ENDPOINT = "aicp.inner.api.ksyun.com"
PUBLIC_AICP_ENDPOINT = DEFAULT_AICP_ENDPOINT

_AICP_ENDPOINTS_BY_MODE = {
    "internal": INTERNAL_AICP_ENDPOINT,
    "inner": INNER_AICP_ENDPOINT,
    "public": PUBLIC_AICP_ENDPOINT,
}
_AUTO_AICP_ENDPOINTS = (
    INTERNAL_AICP_ENDPOINT,
    INNER_AICP_ENDPOINT,
    PUBLIC_AICP_ENDPOINT,
)


def _get_nonempty_env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None

    value = value.strip()
    return value or None


def _endpoint_host(endpoint: str | None) -> str:
    if not endpoint:
        return ""

    normalized = endpoint.strip().lower()
    if "://" in normalized:
        normalized = normalized.split("://", 1)[1]
    return normalized.split("/", 1)[0]


def is_inner_aicp_endpoint(endpoint: str | None) -> bool:
    return _endpoint_host(endpoint).endswith(".inner.api.ksyun.com")


def is_internal_aicp_endpoint(endpoint: str | None) -> bool:
    return _endpoint_host(endpoint).endswith(".internal.api.ksyun.com")


def is_aicp_kop_endpoint(endpoint: str | None) -> bool:
    host = _endpoint_host(endpoint)
    return (
        host.endswith(".internal.api.ksyun.com")
        or host.endswith(".inner.api.ksyun.com")
        or host.endswith(".api.ksyun.com")
    )


def _is_connectable(endpoint: str, *, timeout: float = 1.0) -> bool:
    normalized = endpoint.strip()
    if "://" in normalized:
        scheme, normalized = normalized.split("://", 1)
    else:
        scheme = "http"
    host = normalized.split("/", 1)[0]
    port = 443 if scheme == "https" else 80
    if ":" in host:
        host, raw_port = host.rsplit(":", 1)
        try:
            port = int(raw_port)
        except ValueError:
            return False

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def resolve_aicp_connection(
    env_prefix: str,
    *,
    default_region: str = DEFAULT_AICP_REGION,
    default_endpoint: str = DEFAULT_AICP_ENDPOINT,
    default_scheme: str = "https",
) -> dict[str, str]:
    endpoint = _get_nonempty_env(f"{env_prefix}_ENDPOINT")
    if endpoint is None:
        endpoint = _resolve_auto_endpoint(default_endpoint=default_endpoint)
    scheme = _get_nonempty_env(f"{env_prefix}_SCHEME")
    if scheme is None:
        scheme = _default_scheme_for_endpoint(endpoint, default_scheme=default_scheme)

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


def _resolve_auto_endpoint(*, default_endpoint: str = DEFAULT_AICP_ENDPOINT) -> str:
    mode = (_get_nonempty_env("KSADK_AICP_ENDPOINT_MODE") or "auto").lower()
    if mode in _AICP_ENDPOINTS_BY_MODE:
        return _AICP_ENDPOINTS_BY_MODE[mode]
    if mode not in {"auto", "detect"}:
        return default_endpoint

    for endpoint in _AUTO_AICP_ENDPOINTS:
        if _is_connectable(endpoint):
            return endpoint
    return default_endpoint


def _default_scheme_for_endpoint(endpoint: str, *, default_scheme: str) -> str:
    if is_internal_aicp_endpoint(endpoint) or is_inner_aicp_endpoint(endpoint):
        return "http"
    return default_scheme
