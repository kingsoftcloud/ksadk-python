from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest

from ksadk.a2a.control_plane import A2ARouteInterface
from ksadk.a2a.external_transport import (
    A2ATransportLease,
    CallableA2ARouteOpener,
    GuardedA2AExternalTransport,
)


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
