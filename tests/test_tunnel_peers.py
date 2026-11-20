"""Real dedicated peer listeners; no global directory or public proxy routes."""

import asyncio
import json
import time
import uuid
import zlib
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from websockets.exceptions import ConnectionClosed
from websockets.legacy.client import connect

from ksadk.tunnel import APIOperation, LocalAPIService, TunnelConnector, TunnelLimits
from ksadk.tunnel.discovery import discover_tools
from ksadk.tunnel.langgraph import TunnelTools
from ksadk.tunnel.peers import (
    _CHALLENGE_HEADER,
    _MAX_LEGACY_NONCES,
    PeerConfig,
    PeerNetwork,
    _json,
    _mac,
)
from ksadk.tunnel.protocol import TunnelError
from ksadk.tunnel.registry import ConnectorConnection, TunnelRegistry
from tests.test_mcp_tunnel import cloud, connected, connector, eventually, local_mcp, serve


@asynccontextmanager
async def peers(*registries, secrets=None, runtime_ids=None):
    networks = []

    async def resolve():
        return [network.owner["address"] for network in networks]

    try:
        for index, registry in enumerate(registries):
            network = PeerNetwork(
                PeerConfig(
                    (runtime_ids or ["runtime-test"] * len(registries))[index],
                    "runtime-test-headless",
                    (secrets or ["f" * 32] * len(registries))[index],
                    port=0,
                    host="127.0.0.1",
                    advertised_host="127.0.0.1",
                ),
                lambda reg=registry: reg,
                resolver=resolve,
            )
            if registry is not None:
                registry.peers = network
            await network.start()
            networks.append(network)
        yield networks
    finally:
        await asyncio.gather(*(network.close() for network in networks))


async def test_two_listeners_discover_forward_mcp_and_restore_owner_snapshot():
    async with (
        local_mcp() as (endpoint, calls, _, _),
        cloud() as (_, a, url, _),
        cloud() as (_, b, _, _),
    ):
        # Third ready pod has never loaded an agent or accepted a connector.
        async with peers(a, b, None) as networks:
            async with connected(connector(url, endpoint)):
                tools = await discover_tools("deployment-17", b)
                echo = next(tool for tool in tools if tool.descriptor.name == "echo")
                assert echo.owner == networks[0].owner
                result = await echo.call({"text": "你好🌏"})
                assert result["structuredContent"] == {"text": "你好🌏"}
                assert calls == ["你好🌏"]
                snapshot = json.loads(json.dumps(echo.snapshot()))
                # Reconstructed graph state must route with its saved owner,
                # even after the receiving pod's discovery cache is empty.
                networks[1]._remotes.clear()
                restored = (await TunnelTools("deployment-17", b).get_tools([snapshot]))[0]
                message = await restored.ainvoke(
                    {
                        "type": "tool_call",
                        "id": "restored",
                        "name": restored.name,
                        "args": {"text": "restored"},
                    }
                )
                assert message.artifact["structuredContent"] == {"text": "restored"}
                assert calls == ["你好🌏", "restored"]


async def test_peer_http_stream_is_incremental_and_close_cancels_owner():
    local = FastAPI()
    release = asyncio.Event()

    @local.get("/mcp")
    async def stream():
        async def chunks():
            yield "data: 你好\n\n"
            await release.wait()
            yield "data: end\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    async with serve(local) as endpoint, cloud() as (_, a, url, _), cloud() as (_, b, _, _):
        async with peers(a, b):
            async with connected(connector(url, endpoint + "/mcp")):
                connections = await b.discover_connections("deployment-17")
                remote = connections[0][1]
                response = await remote.forward("erp", httpx.Request("GET", "http://tunnel/"))
                iterator = response.aiter_bytes()
                first = await asyncio.wait_for(anext(iterator), 2)
                assert "你好" in first.decode()
                assert not release.is_set()
                await response.aclose()
                owner = a.connections("deployment-17")[0][1]
                await eventually(lambda: not owner.pending)


@pytest.mark.parametrize(
    "runtime_ids,secrets",
    [
        (["runtime-test", "other-runtime"], None),
        (None, ["f" * 32, "x" * 32]),
    ],
)
async def test_wrong_runtime_or_secret_cannot_discover(runtime_ids, secrets):
    async with peers(
        TunnelRegistry(), TunnelRegistry(), runtime_ids=runtime_ids, secrets=secrets
    ) as networks:
        with pytest.raises(TunnelError, match="peer_discovery_unavailable"):
            await networks[1].discover("deployment-17")


@pytest.mark.parametrize("legacy", [False, True])
async def test_signed_handshake_is_one_use_and_frames_cannot_be_modified(legacy):
    async with peers(TunnelRegistry()) as networks:
        network = networks[0]
        hello = {
            "type": "hello",
            "runtime": "runtime-test",
            "timestamp": int(time.time()),
            "nonce": uuid.uuid4().hex,
        }
        assert "f" * 32 not in json.dumps(hello)
        url = f"ws://{network.owner['address']}/ksadk-tunnel-peer"
        async with connect(url) as ws:
            if not legacy:
                hello["challenge"] = ws.response_headers[_CHALLENGE_HEADER]
            hello["signature"] = _mac(network.key, _json(hello))
            await ws.send(json.dumps(hello))
            assert json.loads(await ws.recv())["type"] == "hello_ack"
        async with connect(url) as ws:
            await ws.send(json.dumps(hello))
            with pytest.raises(ConnectionClosed):
                await ws.recv()
        channel, _ = await network._open(network.owner["address"])
        try:
            await channel.ws.send(
                json.dumps(
                    {
                        "seq": 0,
                        "payload": {"type": "discover", "agent_id": "deployment-17"},
                        "mac": "0" * 64,
                    }
                )
            )
            rejected = await channel.receive()
            assert rejected == {"type": "http_error", "error": "local_service_error"}
            assert "services" not in rejected
            with pytest.raises(ConnectionClosed):
                await channel.receive()
        finally:
            await channel.close()


async def test_duplicate_alias_across_pods_fails_closed():
    async with (
        local_mcp() as (endpoint, _, _, _),
        cloud() as (_, a, url, _),
        cloud() as (_, b, url2, _),
    ):
        async with peers(a, b):
            async with connected(connector(url, endpoint)), connected(connector(url2, endpoint)):
                with pytest.raises(TunnelError, match="ambiguous_service"):
                    await b.discover_connections("deployment-17")


async def test_stale_remote_generation_never_rebinds_after_connector_reconnect():
    async with (
        local_mcp() as (endpoint, calls, _, _),
        cloud() as (_, a, url, _),
        cloud() as (_, b, _, _),
    ):
        async with peers(a, b):
            async with connected(connector(url, endpoint)):
                echo = next(
                    tool
                    for tool in await discover_tools("deployment-17", b)
                    if tool.descriptor.name == "echo"
                )
                owner = a.connections("deployment-17")[0][1]
                await owner.websocket.close(code=1012)
                await eventually(
                    lambda: bool(a.connections("deployment-17"))
                    and a.connections("deployment-17")[0][1] is not owner
                )
                with pytest.raises(TunnelError, match="disconnected"):
                    await echo.call({"text": "stale"})
                fresh = next(
                    tool
                    for tool in await discover_tools("deployment-17", b)
                    if tool.descriptor.name == "echo"
                )
                assert (await fresh.call({"text": "fresh"}))["structuredContent"] == {
                    "text": "fresh"
                }
                assert calls == ["fresh"]


async def test_remote_owner_rejects_foreign_instance_from_saved_snapshot():
    async with (
        local_mcp() as (endpoint, calls, _, _),
        cloud() as (_, a, url, _),
        cloud() as (_, b, _, _),
    ):
        async with peers(a, b):
            async with connected(connector(url, endpoint)):
                echo = next(
                    tool
                    for tool in await discover_tools("deployment-17", b)
                    if tool.descriptor.name == "echo"
                )
                altered = {**echo.owner, "instance_id": uuid.uuid4().hex}
                with pytest.raises(TunnelError, match="peer_auth_failed"):
                    await b.call_tool(
                        "deployment-17",
                        "erp",
                        "echo",
                        {"text": "forbidden"},
                        connector_id=echo.connector_id,
                        owner=altered,
                    )
                assert calls == []


@pytest.mark.parametrize("cancel", [True, False])
async def test_remote_cancel_or_disconnect_cleans_owner_without_replay(cancel):
    async with (
        local_mcp() as (endpoint, calls, started, _),
        cloud() as (_, a, url, _),
        cloud() as (_, b, _, _),
    ):
        async with peers(a, b):
            async with connected(connector(url, endpoint)):
                wait = next(
                    tool
                    for tool in await discover_tools("deployment-17", b)
                    if tool.descriptor.name == "wait_forever"
                )
                owner = a.connections("deployment-17")[0][1]
                task = asyncio.create_task(wait.call({}))
                try:
                    await asyncio.wait_for(started.wait(), 3)
                    if cancel:
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task
                    else:
                        await owner.websocket.close(code=1012)
                        with pytest.raises(TunnelError) as caught:
                            await asyncio.wait_for(task, 3)
                        assert caught.value.indeterminate
                    await eventually(lambda: not owner.pending)
                    assert calls == ["wait"]
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("head", [True, False])
async def test_one_peer_http_error_does_not_cancel_other_call(head):
    async with (
        local_mcp() as (endpoint, calls, started, _),
        cloud() as (_, a, url, _),
        cloud() as (_, b, _, _),
    ):
        async with peers(a, b) as networks:
            original = networks[0]._relay

            async def reject_echo(channel, connection, service, request):
                message = json.loads(request.content) if request.content else {}
                if message.get("method") == "tools/call" and message["params"]["name"] == "echo":
                    if head:
                        await channel.send(
                            {
                                "type": "http_response",
                                "status": 200,
                                "headers": {"content-type": "text/event-stream"},
                            }
                        )
                    await channel.send({"type": "http_error", "error": "busy"})
                else:
                    await original(channel, connection, service, request)

            networks[0]._relay = reject_echo
            async with connected(connector(url, endpoint)):
                tools = await discover_tools("deployment-17", b)
                wait = next(tool for tool in tools if tool.descriptor.name == "wait_forever")
                echo = next(tool for tool in tools if tool.descriptor.name == "echo")
                waiting = asyncio.create_task(wait.call({}))
                try:
                    await asyncio.wait_for(started.wait(), 3)
                    with pytest.raises(TunnelError) as caught:
                        await asyncio.wait_for(echo.call({"text": "rejected"}), 3)
                    assert caught.value.code == "busy"
                    assert not waiting.done()
                    assert calls == ["wait"]
                finally:
                    waiting.cancel()
                    await asyncio.gather(waiting, return_exceptions=True)


async def test_busy_owner_serves_remote_tools_with_full_legacy_replay_window():
    async with local_mcp() as (endpoint, calls, _, _), cloud() as (_, owner, url, _):
        caller = TunnelRegistry()
        async with peers(owner, caller) as (a, _), connected(connector(url, endpoint)):
            now = time.time()
            a._legacy_nonces = {uuid.uuid4().hex: now for _ in range(_MAX_LEGACY_NONCES)}
            legacy_nonces = a._legacy_nonces.copy()
            tools = await discover_tools("deployment-17", caller)
            echo = next(t for t in tools if t.descriptor.name == "echo")
            assert echo.owner == a.owner
            inputs = [f"parallel-{i}" for i in range(8)]
            results = await asyncio.gather(*(echo.call({"text": value}) for value in inputs))
            assert [r["structuredContent"]["text"] for r in results] == inputs
            assert sorted(calls) == sorted(inputs)
            assert a._legacy_nonces == legacy_nonces


async def test_peer_discovers_and_calls_17_services_on_two_connectors():
    from ksadk.tunnel import APIOperation, LocalAPIService, TunnelConnector
    from ksadk.tunnel.server import TunnelAgentPolicy, mount_tunnel

    owner, caller = TunnelRegistry(), TunnelRegistry()
    ingress, local = FastAPI(), FastAPI()
    mount_tunnel(ingress, policies=[TunnelAgentPolicy("example", "fixture", None)], registry=owner)
    calls = []

    @local.get("/{service}")
    async def lookup(service: str):
        calls.append(service)
        return {"service": service}

    async with peers(owner, caller), serve(ingress) as url, serve(local) as endpoint:
        services = [
            LocalAPIService(
                f"s{i}",
                endpoint,
                operations=(APIOperation("lookup", "GET", f"/s{i}", {"type": "object"}),),
            )
            for i in range(17)
        ]
        async with (
            connected(TunnelConnector(url, token="fixture", services=services[:16])),
            connected(TunnelConnector(url, token="fixture", services=services[16:])),
        ):
            tools = await discover_tools("example", caller)
            assert {tool.service for tool in tools} == {service.name for service in services}
            for tool in tools:
                assert (await tool.call({}))["structuredContent"] == {"service": tool.service}
            assert sorted(calls) == sorted(service.name for service in services)


async def test_peer_rejects_oversize_connector_without_remembering_partial_catalog():
    a, b = TunnelRegistry(), TunnelRegistry()
    limits = TunnelLimits()
    services = {
        f"s{i}": {"protocol": "mcp", "name": f"s{i}"} for i in range(limits.max_services + 1)
    }
    a.add(ConnectorConnection("example", services, None, limits))
    async with peers(a, b) as (_, caller):
        with pytest.raises(TunnelError, match="peer_discovery_unavailable"):
            await caller.discover("example")
        assert not caller._remotes


@pytest.mark.parametrize("compressed", [False, True], ids=["plain", "gzip"])
async def test_local_and_peer_api_preserve_multibyte_chunks(compressed):
    expected = "a" * 65535 + "你🌏好" * 20000
    payload = expected.encode()
    # The first byte of 你 is the last byte of the first upstream chunk.
    first, second = payload[:65536], payload[65536:]
    if compressed:
        encoder = zlib.compressobj(wbits=31)
        first = encoder.compress(first) + encoder.flush(zlib.Z_SYNC_FLUSH)
        second = encoder.compress(second) + encoder.flush()
    local = FastAPI()
    calls = []

    @local.get("/report")
    async def report():
        calls.append("report")

        async def chunks():
            yield first
            await asyncio.sleep(0)
            yield second

        return StreamingResponse(
            chunks(),
            media_type="text/plain",
            headers={"Content-Encoding": "gzip"} if compressed else {},
        )

    async with (
        serve(local) as endpoint,
        cloud() as (_, a, url, _),
        cloud() as (_, b, _, _),
        peers(a, b),
        connected(
            TunnelConnector(
                url,
                token="fake-tunnel-token",
                services=[
                    LocalAPIService(
                        "erp",
                        endpoint,
                        operations=(APIOperation("report", "GET", "/report", {"type": "object"}),),
                    ),
                ],
            )
        ),
    ):
        for registry in (a, b):
            result = await registry.call_tool("deployment-17", "erp", "report", {})
            assert result["content"][0]["text"] == expected
        assert calls == ["report", "report"]


@pytest.mark.parametrize(
    "descriptor", [None, {"name": "erp"}, {"name": "erp", "protocol": "unknown"}]
)
async def test_invalid_peer_descriptor_rejects_discovery_and_owner_lookup(descriptor):
    a, b = TunnelRegistry(), TunnelRegistry()
    connection = ConnectorConnection("agent", {"erp": descriptor}, None, TunnelLimits())
    a.add(connection)
    healthy = TunnelRegistry()
    healthy.add(
        ConnectorConnection(
            "agent", {"stock": {"name": "stock", "protocol": "mcp"}}, None, TunnelLimits()
        )
    )
    async with peers(a, b, healthy) as (owner, caller, _):
        with pytest.raises(TunnelError, match="peer_discovery_unavailable"):
            await discover_tools("agent", b)
        with pytest.raises(TunnelError, match="invalid_result"):
            await caller.resolve("agent", "erp", connection.connector_id, owner.owner)
        assert not caller._catalogs and not caller._remotes
        connection.services["erp"] = {"name": "erp", "protocol": "mcp"}
        routes = await caller.discover("agent")
        assert {name for name, _ in routes} == {"erp", "stock"}


async def test_binary_request_is_rejected_before_forwarding_locally_and_remotely():
    async with (
        local_mcp() as (endpoint, calls, _, _),
        cloud() as (_, a, url, _),
        cloud() as (_, b, _, _),
        peers(a, b),
        connected(connector(url, endpoint)),
    ):
        for registry in (a, b):
            [(service, connection)] = await registry.discover_connections("deployment-17")
            request = httpx.Request("POST", "http://tunnel/", content=bytes([255]))
            with pytest.raises(TunnelError, match="invalid_request") as caught:
                await connection.forward(service, request)
            assert not caught.value.indeterminate
        assert not calls
