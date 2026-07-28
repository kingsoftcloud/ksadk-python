"""Lease-based external A2A transport boundary owned by the Runtime network guard."""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Callable
from urllib.parse import urlsplit

import httpx

from ksadk.a2a.control_plane import A2ARouteInterface


@dataclass(frozen=True)
class A2ATransportLease:
    """A single validated external route operation."""

    httpx_client: httpx.AsyncClient
    effective_interface: A2ARouteInterface
    route_kind: str
    policy_revision: str


class A2AExternalTransport(ABC):
    """SSRF-safe external transport supplied by the Runtime network guard."""

    @abstractmethod
    def open_for_route(
        self,
        route: A2ARouteInterface,
        *,
        route_kind: str,
    ) -> AbstractAsyncContextManager[A2ATransportLease]:
        raise NotImplementedError


class A2ARouteOpener(ABC):
    """Platform network guard operation that has completed DNS/VPC policy validation."""

    @abstractmethod
    def open_validated_route(
        self,
        route: A2ARouteInterface,
        *,
        route_kind: str,
    ) -> AbstractAsyncContextManager[A2ATransportLease]:
        raise NotImplementedError


class CallableA2ARouteOpener(A2ARouteOpener):
    """Adapter for an injected Runtime network guard operation."""

    def __init__(
        self,
        open_route: Callable[..., AbstractAsyncContextManager[A2ATransportLease]],
    ) -> None:
        self._open_route = open_route

    def open_validated_route(
        self,
        route: A2ARouteInterface,
        *,
        route_kind: str,
    ) -> AbstractAsyncContextManager[A2ATransportLease]:
        return self._open_route(route, route_kind=route_kind)


class GuardedA2AExternalTransport(A2AExternalTransport):
    """Validates a Runtime network-guard lease before exposing its HTTP client."""

    def __init__(self, route_opener: A2ARouteOpener) -> None:
        self._route_opener = route_opener

    @asynccontextmanager
    async def open_for_route(
        self,
        route: A2ARouteInterface,
        *,
        route_kind: str,
    ) -> AsyncIterator[A2ATransportLease]:
        if route_kind not in {"external_public", "external_vpc"}:
            raise ValueError(f"external transport does not support route kind {route_kind!r}")
        async with self._route_opener.open_validated_route(
            route,
            route_kind=route_kind,
        ) as lease:
            self._validate_lease(route, route_kind, lease)
            yield lease

    @staticmethod
    def _validate_lease(
        requested: A2ARouteInterface,
        route_kind: str,
        lease: A2ATransportLease,
    ) -> None:
        if lease.route_kind != route_kind:
            raise ValueError("network guard lease route kind does not match requested route")
        if not lease.policy_revision.strip():
            raise ValueError("network guard lease is missing policy revision")
        if _canonical_url(lease.effective_interface.url) != _canonical_url(requested.url):
            raise ValueError("network guard lease route does not match requested route")
        if lease.httpx_client.follow_redirects:
            raise ValueError("external A2A transport must not follow redirects automatically")
        if getattr(lease.httpx_client, "_trust_env", None) is not False:
            raise ValueError("external A2A transport must disable environment proxies")


def _canonical_url(value: str) -> tuple[str, str, int | None, str, str]:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("external A2A route must use an absolute HTTP(S) URL")
    default_port = 443 if parsed.scheme == "https" else 80
    port = parsed.port or default_port
    return parsed.scheme, parsed.hostname.lower(), port, parsed.path or "/", parsed.query


__all__ = [
    "A2AExternalTransport",
    "A2ARouteOpener",
    "A2ATransportLease",
    "CallableA2ARouteOpener",
    "GuardedA2AExternalTransport",
]
