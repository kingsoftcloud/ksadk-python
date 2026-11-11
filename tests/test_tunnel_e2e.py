from __future__ import annotations

import asyncio
from contextlib import suppress

import httpx
import pytest
from fastapi import FastAPI

from ksadk.tunnel import (
    LocalServer,
    TunnelConnector,
    TunnelRegistry,
    TunnelToolset,
    mount_tunnel,
)
from tests.harness.fixtures.mcp_server import run_fixture_mcp_server, run_http_app

_FAKE_TUNNEL_TOKEN = "fixture-tunnel-token"
_FAKE_LOCAL_MCP_TOKEN = "fixture-local-mcp-token"


async def _wait_for_server_count(
    registry: TunnelRegistry,
    expected: int,
    connector_task: asyncio.Task,
) -> None:
    deadline = asyncio.get_running_loop().time() + 15
    while len(registry.list_servers("demo_agent")) != expected:
        if connector_task.done():
            await connector_task
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"Tunnel server count did not become {expected}")
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
@pytest.mark.local_process_heavy
async def test_local_mcp_tunnel_list_call_and_disconnect():
    registry = TunnelRegistry()
    app = FastAPI()
    mount_tunnel(
        app,
        token=_FAKE_TUNNEL_TOKEN,
        allowed_agents=["demo_agent"],
        registry=registry,
    )

    with (
        run_fixture_mcp_server(
            label="tunnel_fixture",
            required_token=_FAKE_LOCAL_MCP_TOKEN,
        ) as local_mcp,
        run_http_app(app) as cloud_url,
    ):
        connector = TunnelConnector(
            cloud_url=cloud_url,
            agent="demo_agent",
            token=_FAKE_TUNNEL_TOKEN,
            servers=[
                LocalServer(
                    name="fixture",
                    address=local_mcp.url,
                    tool_filter=["lookup"],
                    headers={"Authorization": f"Bearer {_FAKE_LOCAL_MCP_TOKEN}"},
                )
            ],
        )
        connector_task = asyncio.create_task(connector.start())
        toolset = TunnelToolset(
            "demo_agent",
            registry=registry,
            self_url=cloud_url,
        )
        try:
            await _wait_for_server_count(registry, 1, connector_task)

            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{cloud_url}/tunnel/servers",
                    params={"agent": "demo_agent"},
                    headers={"Authorization": f"Bearer {_FAKE_TUNNEL_TOKEN}"},
                )
            assert response.status_code == 200
            listed = response.json()["servers"]
            assert [item["name"] for item in listed] == ["fixture"]
            assert all("headers" not in item and "query" not in item for item in listed)
            assert all(not item.get("address") for item in listed)

            tools = await toolset.get_tools()
            assert {tool.name for tool in tools} == {"lookup"}

            result = await tools[0].run_async(
                args={"value": "hello"},
                tool_context=None,
            )
            assert "tunnel_fixture:hello" in str(result)
            assert ("lookup", "hello") in local_mcp.log.calls
            assert local_mcp.log.authorization
            assert set(local_mcp.log.authorization) == {f"Bearer {_FAKE_LOCAL_MCP_TOKEN}"}
            assert registry.proxy_token not in str(local_mcp.log.authorization)
            assert _FAKE_TUNNEL_TOKEN not in str(local_mcp.log.authorization)

            connector_task.cancel()
            with suppress(asyncio.CancelledError):
                await connector_task
            await _wait_for_server_count(registry, 0, connector_task)
            assert await toolset.get_tools() == []
        finally:
            connector_task.cancel()
            with suppress(asyncio.CancelledError):
                await connector_task
            await toolset.close()
