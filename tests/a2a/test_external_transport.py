from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from ksadk.a2a.control_plane import A2ARouteInterface
from ksadk.a2a.external_transport import (
    ERR_VPC_EGRESS_DIALER_REQUIRED,
    A2ATransportLease,
    CallableA2ARouteOpener,
    GuardedA2AExternalTransport,
    RuntimeLocalA2AExternalTransport,
)


class _ResponseStream:
    def __init__(
        self,
        response: bytes,
        tls_server_names: list[str | None],
        *,
        delay_seconds: float = 0,
    ) -> None:
        self._response = response
        self._tls_server_names = tls_server_names
        self._delay_seconds = delay_seconds
        self._sent = False

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ARG002
        if self._sent:
            return b""
        self._sent = True
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        return self._response

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ARG002
        return None

    async def aclose(self) -> None:
        return None

    async def start_tls(
        self,
        ssl_context,  # noqa: ANN001
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ARG002
    ) -> "_ResponseStream":
        self._tls_server_names.append(server_hostname)
        return self

    def get_extra_info(self, info: str):  # noqa: ANN201
        return None


class _RecordingNetworkBackend:
    def __init__(self, response: bytes, *, delay_seconds: float = 0) -> None:
        self._response = response
        self._delay_seconds = delay_seconds
        self.connects: list[tuple[str, int]] = []
        self.tls_server_names: list[str | None] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ARG002
        local_address: str | None = None,  # noqa: ARG002
        socket_options=None,  # noqa: ANN001
    ) -> _ResponseStream:
        self.connects.append((host, port))
        return _ResponseStream(
            self._response,
            self.tls_server_names,
            delay_seconds=self._delay_seconds,
        )

    async def connect_unix_socket(self, path: str, timeout=None, socket_options=None):  # noqa: ANN001, ANN201
        raise AssertionError(f"unexpected Unix socket dial: {path}")

    async def sleep(self, seconds: float) -> None:  # noqa: ARG002
        return None


@pytest.mark.asyncio
async def test_guarded_external_transport_uses_validated_route_lease() -> None:
    client = httpx.AsyncClient(follow_redirects=False, trust_env=False)

    @asynccontextmanager
    async def open_route(route, route_kind):  # noqa: ANN001, ANN202
        assert route.url == "https://vendor.example/a2a/jsonrpc"
        assert route_kind == "external_public"
        yield A2ATransportLease(
            httpx_client=client,
            effective_interface=route,
            route_kind=route_kind,
            policy_revision="policy-7",
        )

    transport = GuardedA2AExternalTransport(CallableA2ARouteOpener(open_route))
    route = A2ARouteInterface(
        url="https://vendor.example/a2a/jsonrpc",
        protocol_binding="JSONRPC",
        protocol_version="1.0",
    )
    try:
        async with transport.open_for_route(route, route_kind="external_public") as lease:
            assert lease.httpx_client is client
            assert lease.policy_revision == "policy-7"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_guarded_external_transport_rejects_redirect_following_client() -> None:
    client = httpx.AsyncClient(follow_redirects=True, trust_env=False)

    @asynccontextmanager
    async def open_route(route, route_kind):  # noqa: ANN001, ANN202
        yield A2ATransportLease(
            httpx_client=client,
            effective_interface=route,
            route_kind=route_kind,
            policy_revision="policy-7",
        )

    transport = GuardedA2AExternalTransport(CallableA2ARouteOpener(open_route))
    route = A2ARouteInterface(
        url="https://vendor.example/a2a/jsonrpc",
        protocol_binding="JSONRPC",
        protocol_version="1.0",
    )
    try:
        with pytest.raises(ValueError, match="follow redirects"):
            async with transport.open_for_route(route, route_kind="external_public"):
                pass
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_runtime_local_transport_pins_verified_public_ip_and_keeps_tls_hostname() -> None:
    resolver_calls: list[tuple[str, int]] = []
    backend = _RecordingNetworkBackend(
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
    )

    async def resolve(host: str, port: int) -> list[str]:
        resolver_calls.append((host, port))
        return ["93.184.216.34"]

    transport = RuntimeLocalA2AExternalTransport(
        resolve_hostname=resolve,
        network_backend=backend,
    )
    route = A2ARouteInterface(
        url="https://vendor.example/a2a/jsonrpc",
        protocol_binding="JSONRPC",
        protocol_version="1.0",
    )

    async with transport.open_for_route(route, route_kind="external_public") as lease:
        response = await lease.httpx_client.get(route.url)

    assert response.status_code == 200
    assert resolver_calls == [("vendor.example", 443)]
    assert backend.connects == [("93.184.216.34", 443)]
    assert backend.tls_server_names == ["vendor.example"]


@pytest.mark.asyncio
async def test_runtime_local_transport_rejects_private_dns_results() -> None:
    async def resolve(host: str, port: int) -> list[str]:  # noqa: ARG001
        return ["10.1.2.3"]

    transport = RuntimeLocalA2AExternalTransport(resolve_hostname=resolve)
    route = A2ARouteInterface(
        url="https://vendor.example/a2a/jsonrpc",
        protocol_binding="JSONRPC",
        protocol_version="1.0",
    )

    with pytest.raises(PermissionError, match="globally routable"):
        async with transport.open_for_route(route, route_kind="external_public"):
            pass


@pytest.mark.asyncio
async def test_runtime_local_transport_rejects_external_vpc_without_a_vpc_dialer() -> None:
    transport = RuntimeLocalA2AExternalTransport()
    route = A2ARouteInterface(
        url="https://vendor.example/a2a/jsonrpc",
        protocol_binding="JSONRPC",
        protocol_version="1.0",
    )

    with pytest.raises(RuntimeError, match=ERR_VPC_EGRESS_DIALER_REQUIRED):
        async with transport.open_for_route(route, route_kind="external_vpc"):
            pass


@pytest.mark.asyncio
async def test_runtime_local_transport_rejects_redirect_responses() -> None:
    backend = _RecordingNetworkBackend(
        b"HTTP/1.1 302 Found\r\n"
        b"Location: https://other.example/\r\n"
        b"Content-Length: 0\r\nConnection: close\r\n\r\n"
    )

    async def resolve(host: str, port: int) -> list[str]:  # noqa: ARG001
        return ["93.184.216.34"]

    transport = RuntimeLocalA2AExternalTransport(
        resolve_hostname=resolve,
        network_backend=backend,
    )
    route = A2ARouteInterface(
        url="https://vendor.example/a2a/jsonrpc",
        protocol_binding="JSONRPC",
        protocol_version="1.0",
    )

    async with transport.open_for_route(route, route_kind="external_public") as lease:
        with pytest.raises(httpx.RemoteProtocolError, match="redirect responses are not allowed"):
            await lease.httpx_client.get(route.url)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    ["https://vendor.example:/a2a/jsonrpc", "https://vendor.example:0/a2a/jsonrpc"],
)
async def test_runtime_local_transport_rejects_invalid_explicit_ports(url: str) -> None:
    transport = RuntimeLocalA2AExternalTransport()
    route = A2ARouteInterface(
        url=url,
        protocol_binding="JSONRPC",
        protocol_version="1.0",
    )

    with pytest.raises(ValueError, match="invalid port|absolute HTTPS URL"):
        async with transport.open_for_route(route, route_kind="external_public"):
            pass


@pytest.mark.asyncio
async def test_runtime_local_transport_rejects_oversized_response_before_reading_body() -> None:
    backend = _RecordingNetworkBackend(
        b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nConnection: close\r\n\r\nxyz"
    )

    async def resolve(host: str, port: int) -> list[str]:  # noqa: ARG001
        return ["93.184.216.34"]

    transport = RuntimeLocalA2AExternalTransport(
        resolve_hostname=resolve,
        network_backend=backend,
        max_response_bytes=2,
    )
    route = A2ARouteInterface(
        url="https://vendor.example/a2a/jsonrpc",
        protocol_binding="JSONRPC",
        protocol_version="1.0",
    )

    async with transport.open_for_route(route, route_kind="external_public") as lease:
        with pytest.raises(httpx.RemoteProtocolError, match="configured size limit"):
            await lease.httpx_client.get(route.url)


@pytest.mark.asyncio
async def test_runtime_local_transport_enforces_total_response_deadline() -> None:
    backend = _RecordingNetworkBackend(
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}",
        delay_seconds=0.05,
    )

    async def resolve(host: str, port: int) -> list[str]:  # noqa: ARG001
        return ["93.184.216.34"]

    transport = RuntimeLocalA2AExternalTransport(
        resolve_hostname=resolve,
        network_backend=backend,
        operation_timeout_seconds=0.01,
    )
    route = A2ARouteInterface(
        url="https://vendor.example/a2a/jsonrpc",
        protocol_binding="JSONRPC",
        protocol_version="1.0",
    )

    async with transport.open_for_route(route, route_kind="external_public") as lease:
        with pytest.raises(httpx.ReadTimeout, match="total response deadline"):
            await lease.httpx_client.get(route.url)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload, error",
    [
        (b"{" * 65 + b"}" * 65, "JSON exceeds depth 64"),
        (b'{"field":"' + b"a" * (1024 * 1024 + 1) + b'"}', "JSON string exceeds 1 MiB"),
    ],
)
async def test_runtime_local_transport_rejects_unbounded_json_payloads(
    payload: bytes, error: str
) -> None:
    backend = _RecordingNetworkBackend(
        b"HTTP/1.1 200 OK\r\nContent-Length: "
        + str(len(payload)).encode()
        + b"\r\nConnection: close\r\n\r\n"
        + payload
    )

    async def resolve(host: str, port: int) -> list[str]:  # noqa: ARG001
        return ["93.184.216.34"]

    transport = RuntimeLocalA2AExternalTransport(
        resolve_hostname=resolve,
        network_backend=backend,
        max_response_bytes=len(payload) + 1,
    )
    route = A2ARouteInterface(
        url="https://vendor.example/a2a/jsonrpc",
        protocol_binding="JSONRPC",
        protocol_version="1.0",
    )

    async with transport.open_for_route(route, route_kind="external_public") as lease:
        with pytest.raises(httpx.RemoteProtocolError, match=error):
            await lease.httpx_client.get(route.url)
