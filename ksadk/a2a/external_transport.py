"""Lease-based external A2A transport boundary owned by the Runtime network guard."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable
from urllib.parse import urlsplit

import httpcore
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


_RUNTIME_LOCAL_POLICY_REVISION = "runtime-local-dns-pinned-v1"
_EXTERNAL_CONNECT_TIMEOUT_SECONDS = 5.0
_EXTERNAL_READ_TIMEOUT_SECONDS = 30.0
_EXTERNAL_OPERATION_TIMEOUT_SECONDS = 300.0
_MAX_EXTERNAL_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_EXTERNAL_JSON_DEPTH = 64
_MAX_EXTERNAL_JSON_STRING_BYTES = 1024 * 1024
ERR_VPC_EGRESS_DIALER_REQUIRED = "A2A_VPC_EGRESS_DIALER_REQUIRED"


async def _resolve_hostname(host: str, port: int) -> Sequence[str]:
    """Resolve one operation hostname without retaining an SDK-level DNS cache."""

    loop = asyncio.get_running_loop()
    results = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = tuple(dict.fromkeys(str(item[4][0]) for item in results))
    if not addresses:
        raise OSError(f"DNS lookup returned no addresses for {host!r}")
    return addresses


class _PinnedDNSNetworkBackend(httpcore.AsyncNetworkBackend):
    """Dials a verified IP while httpcore retains the hostname for TLS/SNI."""

    def __init__(
        self,
        delegate: httpcore.AsyncNetworkBackend,
        *,
        expected_hostname: str,
        expected_port: int,
        pinned_ip: str,
    ) -> None:
        self._delegate = delegate
        self._expected_hostname = expected_hostname
        self._expected_port = expected_port
        self._pinned_ip = pinned_ip

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        if host.lower() != self._expected_hostname or port != self._expected_port:
            raise RuntimeError("external A2A transport attempted to dial an unapproved origin")
        return await self._delegate.connect_tcp(
            self._pinned_ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        return await self._delegate.connect_unix_socket(
            path,
            timeout=timeout,
            socket_options=socket_options,
        )

    async def sleep(self, seconds: float) -> None:
        await self._delegate.sleep(seconds)


class _BoundedResponseStream(httpx.AsyncByteStream):
    """Enforces one operation deadline and a raw response-body limit."""

    def __init__(
        self,
        delegate: httpx.AsyncByteStream,
        *,
        max_response_bytes: int,
        deadline: float,
    ) -> None:
        self._delegate = delegate
        self._max_response_bytes = max_response_bytes
        self._deadline = deadline
        self._received_bytes = 0
        self._json_limits = _JSONPayloadLimits()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        iterator = self._delegate.__aiter__()
        while True:
            remaining = self._deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await self.aclose()
                raise httpx.ReadTimeout(
                    "external A2A operation exceeded its total response deadline"
                )
            try:
                chunk = await asyncio.wait_for(anext(iterator), timeout=remaining)
            except StopAsyncIteration:
                return
            except TimeoutError as exc:
                await self.aclose()
                raise httpx.ReadTimeout(
                    "external A2A operation exceeded its total response deadline"
                ) from exc
            self._received_bytes += len(chunk)
            if self._received_bytes > self._max_response_bytes:
                await self.aclose()
                raise httpx.RemoteProtocolError(
                    "external A2A response exceeds configured size limit"
                )
            self._json_limits.observe(chunk)
            yield chunk

    async def aclose(self) -> None:
        await self._delegate.aclose()


class _JSONPayloadLimits:
    """Lightweight structural guard before the A2A SDK parses external JSON/SSE."""

    def __init__(self) -> None:
        self._depth = 0
        self._in_string = False
        self._escaped = False
        self._string_bytes = 0

    def observe(self, chunk: bytes) -> None:
        for byte in chunk:
            if self._in_string:
                self._string_bytes += 1
                if self._string_bytes > _MAX_EXTERNAL_JSON_STRING_BYTES:
                    raise httpx.RemoteProtocolError(
                        "external A2A JSON string exceeds 1 MiB"
                    )
                if self._escaped:
                    self._escaped = False
                elif byte == ord("\\"):
                    self._escaped = True
                elif byte == ord('"'):
                    self._in_string = False
                    self._string_bytes = 0
                continue
            if byte == ord('"'):
                self._in_string = True
                self._string_bytes = 0
            elif byte in (ord("{"), ord("[")):
                self._depth += 1
                if self._depth > _MAX_EXTERNAL_JSON_DEPTH:
                    raise httpx.RemoteProtocolError("external A2A JSON exceeds depth 64")
            elif byte in (ord("}"), ord("]")):
                self._depth = max(0, self._depth - 1)


class _OriginLockedRedirectRejectingTransport(httpx.AsyncBaseTransport):
    """Restricts a lease client to its resolved origin and rejects all redirects."""

    def __init__(
        self,
        delegate: httpx.AsyncBaseTransport,
        *,
        hostname: str,
        port: int,
        max_response_bytes: int,
        operation_timeout_seconds: float,
    ) -> None:
        self._delegate = delegate
        self._hostname = hostname
        self._port = port
        self._max_response_bytes = max_response_bytes
        self._operation_timeout_seconds = operation_timeout_seconds

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request_port = request.url.port or (443 if request.url.scheme == "https" else 80)
        if (
            request.url.scheme != "https"
            or request.url.host.lower() != self._hostname
            or request_port != self._port
        ):
            raise RuntimeError("external A2A transport lease attempted to use an unapproved origin")
        request.headers["Accept-Encoding"] = "identity"
        deadline = asyncio.get_running_loop().time() + self._operation_timeout_seconds
        try:
            response = await asyncio.wait_for(
                self._delegate.handle_async_request(request),
                timeout=self._operation_timeout_seconds,
            )
        except TimeoutError as exc:
            raise httpx.ReadTimeout(
                "external A2A operation exceeded its total response deadline"
            ) from exc
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise httpx.RemoteProtocolError("A2A external redirect responses are not allowed")
        declared_length = response.headers.get("content-length")
        if declared_length is not None:
            try:
                exceeds_limit = int(declared_length) > self._max_response_bytes
            except ValueError as exc:
                await response.aclose()
                raise httpx.RemoteProtocolError(
                    "external A2A response has an invalid Content-Length"
                ) from exc
            if exceeds_limit:
                await response.aclose()
                raise httpx.RemoteProtocolError(
                    "external A2A response exceeds configured size limit"
                )
        if not isinstance(response.stream, httpx.AsyncByteStream):
            await response.aclose()
            raise RuntimeError("external A2A transport received a non-async response stream")
        response.stream = _BoundedResponseStream(
            response.stream,
            max_response_bytes=self._max_response_bytes,
            deadline=deadline,
        )
        return response

    async def aclose(self) -> None:
        await self._delegate.aclose()


class RuntimeLocalA2AExternalTransport(A2AExternalTransport):
    """Runtime-local guarded HTTPS transport for public external Agents over NAT.

    The Runtime resolves every external operation itself, rejects any non-global
    DNS result, then pins the selected IP in the actual TCP backend.  The HTTP
    origin remains the original hostname so certificate validation, ``Host``,
    and TLS SNI are not weakened by the pinning.
    """

    def __init__(
        self,
        *,
        resolve_hostname: Callable[[str, int], Awaitable[Sequence[str]]] = _resolve_hostname,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
        connect_timeout_seconds: float = _EXTERNAL_CONNECT_TIMEOUT_SECONDS,
        read_timeout_seconds: float = _EXTERNAL_READ_TIMEOUT_SECONDS,
        operation_timeout_seconds: float = _EXTERNAL_OPERATION_TIMEOUT_SECONDS,
        max_response_bytes: int = _MAX_EXTERNAL_RESPONSE_BYTES,
    ) -> None:
        if (
            connect_timeout_seconds <= 0
            or read_timeout_seconds <= 0
            or operation_timeout_seconds <= 0
            or max_response_bytes <= 0
        ):
            raise ValueError("external A2A transport limits must be positive")
        self._resolve_hostname = resolve_hostname
        self._network_backend = network_backend or httpcore.AnyIOBackend()
        self._connect_timeout_seconds = connect_timeout_seconds
        self._read_timeout_seconds = read_timeout_seconds
        self._operation_timeout_seconds = operation_timeout_seconds
        self._max_response_bytes = max_response_bytes

    @asynccontextmanager
    async def open_for_route(
        self,
        route: A2ARouteInterface,
        *,
        route_kind: str,
    ) -> AsyncIterator[A2ATransportLease]:
        hostname, port = self._validated_public_origin(route, route_kind=route_kind)
        try:
            resolved = await asyncio.wait_for(
                self._resolve_hostname(hostname, port),
                timeout=self._connect_timeout_seconds,
            )
        except TimeoutError as exc:
            raise httpx.ConnectTimeout("external A2A DNS resolution timed out") from exc
        pinned_ip = self._select_global_ip(resolved, hostname=hostname)
        client = self._build_client(hostname=hostname, port=port, pinned_ip=pinned_ip)
        try:
            yield A2ATransportLease(
                httpx_client=client,
                effective_interface=route,
                route_kind=route_kind,
                policy_revision=_RUNTIME_LOCAL_POLICY_REVISION,
            )
        finally:
            await client.aclose()

    @staticmethod
    def _validated_public_origin(
        route: A2ARouteInterface,
        *,
        route_kind: str,
    ) -> tuple[str, int]:
        if route_kind == "external_vpc":
            raise RuntimeError(
                f"{ERR_VPC_EGRESS_DIALER_REQUIRED}: external_vpc routes require a "
                "platform-injected VPC dialer"
            )
        if route_kind != "external_public":
            raise ValueError(
                "RuntimeLocalA2AExternalTransport supports only external_public routes"
            )
        parsed = urlsplit(route.url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("external public A2A route has an invalid port") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.netloc.rsplit("@", 1)[-1].endswith(":")
            or port is not None and not 1 <= port <= 65535
        ):
            raise ValueError("external public A2A route must be an absolute HTTPS URL")
        try:
            hostname = parsed.hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("external public A2A route has an invalid hostname") from exc
        return hostname, port or 443

    @staticmethod
    def _select_global_ip(addresses: Sequence[str], *, hostname: str) -> str:
        if not addresses:
            raise PermissionError(f"external public A2A DNS returned no addresses for {hostname!r}")
        normalized: list[str] = []
        for value in addresses:
            try:
                address = ipaddress.ip_address(value)
            except ValueError as exc:
                raise PermissionError(
                    f"external public A2A DNS returned an invalid address for {hostname!r}"
                ) from exc
            if not address.is_global:
                raise PermissionError(
                    "external public A2A DNS results must contain only globally routable IPs"
                )
            normalized.append(str(address))
        return normalized[0]

    def _build_client(self, *, hostname: str, port: int, pinned_ip: str) -> httpx.AsyncClient:
        # httpx has no public network-backend hook. Rebuild its direct httpcore pool
        # to preserve httpx's SSL configuration while replacing only TCP dialing.
        base_transport = httpx.AsyncHTTPTransport(
            trust_env=False,
            http1=True,
            http2=False,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        )
        existing_pool = getattr(base_transport, "_pool", None)
        if not isinstance(existing_pool, httpcore.AsyncConnectionPool):
            raise RuntimeError("httpx direct async transport is unavailable for A2A DNS pinning")
        base_transport._pool = httpcore.AsyncConnectionPool(  # type: ignore[attr-defined]
            ssl_context=existing_pool._ssl_context,
            max_connections=1,
            max_keepalive_connections=0,
            keepalive_expiry=existing_pool._keepalive_expiry,
            http1=True,
            http2=False,
            retries=0,
            network_backend=_PinnedDNSNetworkBackend(
                self._network_backend,
                expected_hostname=hostname,
                expected_port=port,
                pinned_ip=pinned_ip,
            ),
        )
        transport = _OriginLockedRedirectRejectingTransport(
            base_transport,
            hostname=hostname,
            port=port,
            max_response_bytes=self._max_response_bytes,
            operation_timeout_seconds=self._operation_timeout_seconds,
        )
        return httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(
                connect=self._connect_timeout_seconds,
                read=self._read_timeout_seconds,
                write=self._read_timeout_seconds,
                pool=self._connect_timeout_seconds,
            ),
        )


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
    "ERR_VPC_EGRESS_DIALER_REQUIRED",
    "GuardedA2AExternalTransport",
    "RuntimeLocalA2AExternalTransport",
]
