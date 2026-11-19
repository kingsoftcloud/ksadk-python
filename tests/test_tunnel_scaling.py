"""Elastic replica discovery through real internal listeners and connector sockets."""

import asyncio
import socket
from dataclasses import replace

import pytest
from fastapi import FastAPI
from websockets.exceptions import InvalidHandshake

from ksadk.tunnel.discovery import discover_tools
from ksadk.tunnel.peers import PeerConfig, PeerNetwork
from ksadk.tunnel.protocol import TunnelError, TunnelLimits
from ksadk.tunnel.registry import TunnelRegistry
from tests.test_mcp_tunnel import cloud, connected, connector, eventually, local_mcp, serve
from tests.test_tunnel_peers import peers


async def echo_tool(registry):
    return next(
        tool
        for tool in await discover_tools("deployment-17", registry)
        if tool.descriptor.name == "echo"
    )


async def test_cache_coalesces_discovery_and_local_calls_never_use_internal_port(monkeypatch):
    async with local_mcp() as (endpoint, calls, _, _), cloud() as (_, a, url, _):
        async with peers(a, TunnelRegistry()) as networks:
            async with connected(connector(url, endpoint)):
                local = await echo_tool(a)
                opens = 0
                original = networks[0]._open

                async def count(*args, **kwargs):
                    nonlocal opens
                    opens += 1
                    return await original(*args, **kwargs)

                monkeypatch.setattr(networks[0], "_open", count)
                assert (await local.call({"text": "local"}))["structuredContent"]["text"] == "local"
                assert opens == 0
                b = networks[1]
                probes = 0
                original_probe = b._discover_at

                async def probe(*args, **kwargs):
                    nonlocal probes
                    probes += 1
                    return await original_probe(*args, **kwargs)

                monkeypatch.setattr(b, "_discover_at", probe)
                results = await asyncio.gather(*(b.discover("deployment-17") for _ in range(12)))
                assert all(result[0][0] == "erp" for result in results)
                assert probes == 1
                owner = results[0][0][1].owner
                generation = results[0][0][1].connector_id
                for _ in range(3):
                    await b.resolve("deployment-17", "erp", generation, owner)
                assert probes == 1
                b._catalogs["deployment-17"].expires_at = 0
                await b.discover("deployment-17")
                assert probes == 2
                assert calls == ["local"]


async def test_partial_discovery_survives_starting_pods_but_reports_unknown_if_no_owner():
    async with (
        local_mcp() as (endpoint, _, _, _),
        cloud() as (_, a, url, _),
        cloud() as (_, b, _, _),
    ):
        async with peers(a, b) as networks:
            # A bound but non-listening port models an endpoint not ready to accept peers.
            with socket.socket() as offline:
                offline.bind(("127.0.0.1", 0))
                dead = "127.0.0.1:" + str(offline.getsockname()[1])

                async def resolve():
                    return [network.owner["address"] for network in networks] + [dead]

                for network in networks:
                    network._resolver = resolve
                async with connected(connector(url, endpoint)):
                    remote = await echo_tool(b)
                    assert (await remote.call({"text": "online"}))["structuredContent"][
                        "text"
                    ] == "online"
                    assert networks[1]._catalogs["deployment-17"].unavailable
                    # A local route remains usable even if every remote peer is unreachable.
                    await networks[1].close()
                    assert (await (await echo_tool(a)).call({"text": "local"}))[
                        "structuredContent"
                    ]["text"] == "local"
            # The successful owner is now gone too. A negative, incomplete result is explicit.
            networks[0]._catalogs.clear()
            with pytest.raises(TunnelError, match="peer_discovery_unavailable"):
                await networks[0].discover("unregistered")
            # Framework tool enumeration must still let the model run.
            assert await discover_tools("unregistered", a) == []


async def test_occupied_peer_port_preserves_local_tools_and_recovers_on_heartbeat():
    async with (
        local_mcp() as (endpoint, calls, _, _),
        cloud() as (_, a, url, _),
        cloud() as (_, b, _, _),
        peers(b) as (peer_b,),
    ):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            port = occupied.getsockname()[1]

            async def resolve():
                return [f"127.0.0.1:{port}", peer_b.owner["address"]]

            peer_b._resolver = resolve
            a.peers = PeerNetwork(
                replace(peer_b.config, port=port, topology_cache_ttl=0.05),
                lambda: a,
                resolver=resolve,
            )
            assert await discover_tools("deployment-17", a) == []
            client = connector(url, endpoint, limits=TunnelLimits(heartbeat_interval=0.1))
            async with connected(client):
                local = await echo_tool(a)
                assert local.owner is None
                await local.call({"text": "local"})
                occupied.close()
                # With no new requests on A, its existing Connector heartbeat retries the bind.
                await eventually(lambda: a.peers.owner is not None)
                remote = await echo_tool(b)
                assert remote.owner == a.peers.owner
                assert remote.connector_id == local.connector_id
                await remote.call({"text": "forwarded"})
                # A restored snapshot may land on A before A discovers its own new peer address.
                await a.call_tool(
                    "deployment-17",
                    "erp",
                    "echo",
                    {"text": "restored"},
                    connector_id=remote.connector_id,
                    owner=remote.owner,
                )
                assert calls == ["local", "forwarded", "restored"]


@pytest.mark.parametrize("failure", ["empty", "unresolved", "invalid_address", "timeout"])
async def test_peer_listener_degrades_on_unusable_local_dns(monkeypatch, failure):
    async def resolve(host, port, **kwargs):
        if failure == "timeout":
            await asyncio.Event().wait()
        if failure == "unresolved":
            raise socket.gaierror("fixture")
        if failure == "empty":
            return []
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("invalid", 0))]

    monkeypatch.setattr(socket, "getfqdn", lambda: "fixture.invalid")
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    registry = TunnelRegistry()
    registry.peers = PeerNetwork(
        PeerConfig("runtime", "fixture", "f" * 32, port=0, discovery_timeout=0.1), lambda: registry
    )
    try:
        assert await discover_tools("deployment-17", registry) == []
        assert registry.peers.server is None
    finally:
        await registry.close()


async def test_dns_topology_cache_refreshes_and_deduplicates(monkeypatch):
    async with peers(TunnelRegistry()) as networks:
        network = networks[0]
        network._resolver = None
        records = ["127.0.0.2", "127.0.0.2"]
        lookups = []

        async def getaddrinfo(host, port, **kwargs):
            lookups.append((host, port))
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in records]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)
        first = await network.addresses()
        assert first == {"127.0.0.2:0"}
        assert await network.addresses() == first
        assert len(lookups) == 1
        records[:] = ["127.0.0.3"]
        network._topology_expires_at = 0
        assert await network.addresses() == {"127.0.0.3:0"}
        assert len(lookups) == 2


async def test_one_to_ten_to_one_and_connector_reconnects_to_surviving_replica():
    registries = [TunnelRegistry() for _ in range(10)]
    async with (
        local_mcp() as (endpoint, calls, started, _),
        cloud() as (app_a, a, _, _),
        cloud() as (app_b, b, _, _),
    ):
        registries[0], registries[1] = a, b
        target = app_a
        fallback = FastAPI()

        class Gateway:
            async def __call__(self, scope, receive, send):
                app = target if scope["type"] == "websocket" else fallback
                await app(scope, receive, send)

        async with peers(*registries) as networks, serve(Gateway()) as url:
            visible = [networks[0]]

            async def resolve():
                return [network.owner["address"] for network in visible]

            for network in networks:
                network._resolver = resolve
                network.config = replace(
                    network.config, ownership_cache_ttl=0.05, topology_cache_ttl=0.05
                )
            client = connector(url, endpoint)
            async with connected(client):
                assert (await (await echo_tool(a)).call({"text": "one"}))["structuredContent"][
                    "text"
                ] == "one"
                # Scale up: every new replica finds the original, unchanged connector.
                original = a.connections("deployment-17")[0][1]
                visible[:] = networks
                for registry in registries[1:]:
                    await (await echo_tool(registry)).call({"text": "ten"})
                assert a.connections("deployment-17")[0][1] is original
                stale = await echo_tool(b)
                wait = next(
                    tool
                    for tool in await discover_tools("deployment-17", b)
                    if tool.descriptor.name == "wait_forever"
                )
                pending = asyncio.create_task(wait.call({}))
                try:
                    await asyncio.wait_for(started.wait(), 3)
                    # Scale down deletes the owner. The same external gateway now routes
                    # reconnect handshakes to B. No request is replayed on the new socket.
                    target = app_b
                    visible[:] = [networks[1]]
                    await networks[0].close()
                    for network in networks[2:]:
                        await network.close()
                    await original.websocket.close(code=1012)
                    with pytest.raises(TunnelError) as interrupted:
                        await asyncio.wait_for(pending, 3)
                    assert interrupted.value.indeterminate
                    await eventually(lambda: bool(b.connections("deployment-17")))
                    with pytest.raises(TunnelError):
                        await stale.call({"text": "must-not-replay"})
                    recovered = await echo_tool(b)
                    await recovered.call({"text": "recovered"})
                    assert calls == ["one", *(["ten"] * 9), "wait", "recovered"]
                    assert not a.connections("deployment-17")
                finally:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("unavailable", ["timeout", "handshake"])
async def test_probe_has_a_deadline_and_does_not_block_other_owners(monkeypatch, unavailable):
    async with local_mcp() as (endpoint, _, _, _), cloud() as (_, a, url, _):
        async with peers(a, TunnelRegistry(), TunnelRegistry()) as networks:
            b = networks[1]
            b.config = replace(b.config, discovery_timeout=0.3)
            original = b._discover_at
            cancelled = asyncio.Event()

            async def probe(address, *args, **kwargs):
                if address == networks[2].owner["address"]:
                    if unavailable == "handshake":
                        raise InvalidHandshake("peer not ready")
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()
                return await original(address, *args, **kwargs)

            monkeypatch.setattr(b, "_discover_at", probe)
            async with connected(connector(url, endpoint)):
                found = await asyncio.wait_for(b.discover("deployment-17"), 2)
                assert found[0][0] == "erp"
                assert cancelled.is_set() == (unavailable == "timeout")


@pytest.mark.parametrize(
    "field", ["ownership_cache_ttl", "topology_cache_ttl", "discovery_timeout"]
)
@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_cache_and_probe_durations_must_be_positive_and_finite(field, value):
    with pytest.raises(ValueError):
        PeerConfig("runtime", "runtime-headless", "f" * 32, **{field: value})


async def test_dns_failure_keeps_local_service_callable(monkeypatch):
    async with local_mcp() as (endpoint, _, _, _), cloud() as (_, a, url, _):
        async with peers(a) as networks:

            async def unavailable():
                raise socket.gaierror("temporary DNS failure")

            networks[0]._resolver = unavailable
            async with connected(connector(url, endpoint)):
                tool = await echo_tool(a)
                assert (await tool.call({"text": "local-dns-down"}))["structuredContent"][
                    "text"
                ] == "local-dns-down"


async def test_offline_owner_is_invalidated_and_new_owner_discovered_without_replaying():
    async with (
        local_mcp() as (endpoint, calls, _, _),
        cloud() as (_, a, url_a, _),
        cloud() as (_, b, _, _),
        cloud() as (_, c, url_c, _),
    ):
        async with peers(a, b, c) as networks:
            async with connected(connector(url_a, endpoint)):
                stale = await echo_tool(b)
            async with connected(connector(url_c, endpoint)):
                with pytest.raises(TunnelError):
                    await stale.call({"text": "must-not-run"})
                assert networks[1]._catalogs["deployment-17"].expires_at == 0
                fresh = await echo_tool(b)
                assert fresh.owner == networks[2].owner
                await fresh.call({"text": "new-owner"})
                assert calls == ["new-owner"]


async def test_new_conflicting_alias_is_rejected_after_cache_refresh():
    async with (
        local_mcp() as (endpoint, _, _, _),
        cloud() as (_, a, url_a, _),
        cloud() as (_, b, _, _),
        cloud() as (_, c, url_c, _),
    ):
        async with peers(a, b, c) as networks:
            async with connected(connector(url_a, endpoint)):
                await echo_tool(b)
                async with connected(connector(url_c, endpoint)):
                    networks[1]._catalogs["deployment-17"].expires_at = 0
                    with pytest.raises(TunnelError, match="ambiguous_service"):
                        await b.discover_connections("deployment-17")


async def test_caller_shutdown_cancels_outgoing_mcp_and_releases_owner_pending():
    async with (
        local_mcp() as (endpoint, calls, started, _),
        cloud() as (_, a, url, _),
        cloud() as (_, b, _, _),
    ):
        async with peers(a, b) as networks:
            async with connected(connector(url, endpoint)):
                wait = next(
                    tool
                    for tool in await discover_tools("deployment-17", b)
                    if tool.descriptor.name == "wait_forever"
                )
                request = asyncio.create_task(wait.call({}))
                try:
                    await asyncio.wait_for(started.wait(), 3)
                    await networks[1].close()
                    with pytest.raises(TunnelError) as failure:
                        await asyncio.wait_for(request, 3)
                    assert failure.value.indeterminate
                    owner = a.connections("deployment-17")[0][1]
                    await eventually(lambda: not owner.pending)
                    assert not networks[1]._channels
                    assert calls == ["wait"]
                finally:
                    request.cancel()
                    await asyncio.gather(request, return_exceptions=True)


async def test_negative_catalog_expires_and_not_ready_peer_can_join(monkeypatch):
    async with (
        local_mcp() as (endpoint, _, _, _),
        cloud() as (_, a, url, _),
        cloud() as (_, b, _, _),
    ):
        async with peers(a, b) as networks:
            b_peer = networks[1]
            original = b_peer._discover_at
            ready = False

            async def probe(*args, **kwargs):
                if not ready:
                    raise OSError("not listening yet")
                return await original(*args, **kwargs)

            monkeypatch.setattr(b_peer, "_discover_at", probe)
            async with connected(connector(url, endpoint)):
                with pytest.raises(TunnelError, match="peer_discovery_unavailable"):
                    await b_peer.discover("deployment-17")
                ready = True
                # Negative catalogs have a short TTL, so recovery needs no restart.
                await asyncio.sleep(0.3)
                assert (await b_peer.discover("deployment-17"))[0][0] == "erp"


async def test_same_address_new_process_instance_rejects_cached_handle():
    from ksadk.tunnel.peers import PeerNetwork

    async with (
        local_mcp() as (endpoint, calls, _, _),
        cloud() as (_, a, url, _),
        cloud() as (_, b, _, _),
    ):
        async with peers(a, b) as networks:
            async with connected(connector(url, endpoint)):
                stale = await echo_tool(b)
            old = networks[0]
            port = old.server.sockets[0].getsockname()[1]
            await old.close()

            async def resolve():
                return [network.owner["address"] for network in networks]

            replacement = PeerNetwork(replace(old.config, port=port), lambda: a, resolver=resolve)
            a.peers = replacement
            networks[0] = replacement
            await replacement.start()
            assert replacement.owner["address"] == stale.owner["address"]
            assert replacement.instance_id != stale.owner["instance_id"]
            async with connected(connector(url, endpoint)):
                with pytest.raises(TunnelError, match="peer_auth_failed"):
                    await stale.call({"text": "old-process"})
                fresh = await echo_tool(b)
                await fresh.call({"text": "new-process"})
                assert calls == ["new-process"]


@pytest.mark.parametrize("code", ["peer_auth_failed", "invalid_result"])
async def test_invalid_authenticated_peer_data_is_not_cached(monkeypatch, code):
    async with peers(TunnelRegistry(), TunnelRegistry()) as networks:

        async def invalid(*args, **kwargs):
            raise TunnelError(code)

        monkeypatch.setattr(networks[1], "_discover_at", invalid)
        with pytest.raises(TunnelError, match="peer_discovery_unavailable"):
            await networks[1].discover("deployment-17")
        assert not networks[1]._catalogs


async def test_expired_handle_reports_offline_owner_before_any_local_tool_call():
    async with (
        local_mcp() as (endpoint, calls, _, _),
        cloud() as (_, a, url, _),
        cloud() as (_, b, _, _),
        peers(a, b) as networks,
        connected(connector(url, endpoint)),
    ):
        handle = await echo_tool(b)
        networks[1]._catalogs["deployment-17"].expires_at = 0
        await networks[0].close()
        with pytest.raises(TunnelError, match="disconnected") as error:
            await handle.call({"text": "stale"})
        assert not error.value.indeterminate
        assert calls == []


async def test_refresh_keeps_inflight_budget_for_confirmed_connector_generation():
    async with (
        local_mcp() as (endpoint, calls, started, _),
        cloud() as (_, a, url, _),
        cloud() as (_, b, _, _),
        peers(a, b) as networks,
        connected(connector(url, endpoint)),
    ):
        peer = networks[1]
        peer.limits = replace(peer.limits, max_inflight=1)
        handles = await discover_tools("deployment-17", b)
        slow = next(tool for tool in handles if tool.descriptor.name == "wait_forever")
        echo = next(tool for tool in handles if tool.descriptor.name == "echo")
        pending = asyncio.create_task(slow.call({}))
        try:
            await asyncio.wait_for(started.wait(), 3)
            peer.invalidate(echo.owner)
            with pytest.raises(TunnelError, match="busy"):
                await echo.call({"text": "over budget"})
            assert calls == ["wait"]
            assert not pending.done()
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
