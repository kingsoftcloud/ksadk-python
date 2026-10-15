"""Explicit product Key HTTP configuration for external A2A clients."""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

import httpx

ENV_A2A_API_KEY = "KSADK_A2A_API_KEY"


class A2AProductKeyConfigurationError(ValueError):
    """Invalid configuration; messages never contain credential values."""


class _ProductKeyAuth(httpx.Auth):
    def __init__(self, origin: str, api_key: str | None):
        try:
            parsed = urlsplit(origin)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError
            self._origin = (parsed.scheme, parsed.hostname.lower(), parsed.port or 443)
        except (ValueError, TypeError):
            raise A2AProductKeyConfigurationError(
                "A2A requires a credential-free HTTPS origin"
            ) from None
        # None opts into this single env var; an explicit empty value is an error.
        key = os.environ.get(ENV_A2A_API_KEY) if api_key is None else api_key
        if (
            not isinstance(key, str)
            or not 0 < len(key) <= 4096
            or not re.fullmatch(r"[!-~]+", key)
            or "," in key
        ):
            raise A2AProductKeyConfigurationError("an explicit A2A product API Key is required")
        self._key = key

    def auth_flow(self, request):
        url = request.url
        if (
            (url.scheme, url.host.lower(), url.port or 443) != self._origin
            or url.username
            or url.password
            or url.fragment
            or not url.path.startswith("/a2a/")
        ):
            raise A2AProductKeyConfigurationError("A2A credential destination is not authorized")
        if any(
            name in request.headers
            for name in (
                "authorization",
                "cookie",
                "x-api-key",
                "x-agentengine-a2a-permit",
            )
        ):
            raise A2AProductKeyConfigurationError("conflicting A2A credentials")
        request.headers["Authorization"] = "Bearer " + self._key
        yield request


async def _reject_redirect(response):
    if 300 <= response.status_code < 400:
        await response.aclose()
        raise A2AProductKeyConfigurationError("A2A credential redirects are forbidden")


def product_key_http_client(
    gateway_origin: str,
    *,
    api_key: str | None = None,
    timeout: httpx.Timeout | float = 60,
) -> httpx.AsyncClient:
    """Create an owned client usable with the official A2A ClientConfig.

    Explicit api_key takes precedence over KSADK_A2A_API_KEY. No MCP, AI search,
    resource API Key, proxy, or ambient cookie configuration is inherited.
    The caller owns the returned client and must close it (prefer async with).
    """
    return httpx.AsyncClient(
        auth=_ProductKeyAuth(gateway_origin, api_key),
        trust_env=False,
        follow_redirects=False,
        timeout=timeout,
        event_hooks={"response": [_reject_redirect]},
    )


__all__ = ["A2AProductKeyConfigurationError", "ENV_A2A_API_KEY", "product_key_http_client"]
