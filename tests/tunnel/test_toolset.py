from __future__ import annotations

import pytest

from ksadk.tunnel.protocol.mcp import McpProtocol
from ksadk.tunnel.registry import ServerDescriptor, TunnelRegistry
from ksadk.tunnel.toolset import TunnelToolset

from .conftest import add_connection


class FakeHandler:
    instances = []

    def __init__(self, descriptor, proxy_url, proxy_token) -> None:
        self.descriptor = descriptor
        self.proxy_url = proxy_url
        self.proxy_token = proxy_token
        self.close_calls = 0
        self.__class__.instances.append(self)

    async def get_tools(self, readonly_context=None):
        return [f"tool:{self.descriptor.name}:{self.descriptor.tool_filter}"]

    async def close(self) -> None:
        self.close_calls += 1


@pytest.fixture(autouse=True)
def clear_handlers():
    FakeHandler.instances.clear()


@pytest.mark.asyncio
async def test_dynamic_handler_cache_rebuild_offline_and_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ksadk.tunnel.toolset.get_protocol", lambda protocol: FakeHandler)
    registry = TunnelRegistry()
    toolset = TunnelToolset("agent", registry, self_url="http://127.0.0.1:9000/")

    assert await toolset.get_tools() == []
    connection, _ = add_connection(
        registry,
        descriptors=[ServerDescriptor(name="docs", tool_filter=["search"])],
    )
    assert await toolset.get_tools() == ["tool:docs:['search']"]
    first = FakeHandler.instances[-1]
    assert first.proxy_url == "http://127.0.0.1:9000/tunnel/mcp/agent/docs"
    assert first.proxy_token == registry.proxy_token

    assert await toolset.get_tools() == ["tool:docs:['search']"]
    assert len(FakeHandler.instances) == 1

    registry.remove_connection(connection.connector_id, connection)
    replacement, _ = add_connection(
        registry,
        connector_id="replacement",
        descriptors=[ServerDescriptor(name="docs", tool_filter=["read"])],
    )
    assert await toolset.get_tools() == ["tool:docs:['read']"]
    second = FakeHandler.instances[-1]
    assert second is not first
    assert first.close_calls == 1

    registry.remove_connection(replacement.connector_id, replacement)
    assert await toolset.get_tools() == []
    assert second.close_calls == 1
    await toolset.close()
    await toolset.close()
    assert second.close_calls == 1


@pytest.mark.asyncio
async def test_same_descriptor_reconnect_rebuilds_handler_for_new_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ksadk.tunnel.toolset.get_protocol", lambda protocol: FakeHandler)
    registry = TunnelRegistry()
    toolset = TunnelToolset("agent", registry)
    first_connection, _ = add_connection(registry, connector_id="first")
    await toolset.get_tools()
    first_handler = FakeHandler.instances[-1]

    registry.remove_connection(first_connection.connector_id, first_connection)
    add_connection(registry, connector_id="second")
    await toolset.get_tools()
    second_handler = FakeHandler.instances[-1]

    assert second_handler is not first_handler
    assert first_handler.close_calls == 1


@pytest.mark.asyncio
async def test_close_is_idempotent_and_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ksadk.tunnel.toolset.get_protocol", lambda protocol: FakeHandler)
    registry = TunnelRegistry()
    add_connection(registry)
    toolset = TunnelToolset("agent", registry)
    await toolset.get_tools()
    handler = FakeHandler.instances[-1]

    await toolset.close()
    await toolset.close()
    assert handler.close_calls == 1
    assert await toolset.get_tools() == []
    assert len(FakeHandler.instances) == 1


def test_self_url_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = TunnelRegistry()
    registry.self_url = "http://127.0.0.1:7000/"
    monkeypatch.setenv("KSADK_TUNNEL_SELF_URL", "http://127.0.0.1:7100/")
    monkeypatch.setenv("PORT", "7200")

    assert TunnelToolset("agent", registry, "http://127.0.0.1:7300/")._self_base_url() == (
        "http://127.0.0.1:7300"
    )
    assert TunnelToolset("agent", registry)._self_base_url() == "http://127.0.0.1:7000"
    registry.self_url = None
    assert TunnelToolset("agent", registry)._self_base_url() == "http://127.0.0.1:7100"
    monkeypatch.delenv("KSADK_TUNNEL_SELF_URL")
    assert TunnelToolset("agent", registry)._self_base_url() == "http://127.0.0.1:7200"
    monkeypatch.delenv("PORT")
    assert TunnelToolset("agent", registry)._self_base_url() == "http://127.0.0.1:8000"


@pytest.mark.asyncio
async def test_mcp_protocol_reuses_runtime_config_and_closes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = []

    class FakeMcpToolset:
        def __init__(self) -> None:
            self.close_calls = 0

        async def get_tools(self, readonly_context=None):
            return ["mcp-tool"]

        async def close(self) -> None:
            self.close_calls += 1

    def build(config):
        created.append(config)
        return FakeMcpToolset()

    monkeypatch.setattr("ksadk.tunnel.protocol.mcp.build_mcp_toolset", build)
    protocol = McpProtocol(
        ServerDescriptor(name="docs", tool_filter=["search"]),
        "http://127.0.0.1:8000/tunnel/mcp/agent/docs",
        "internal-proxy-token",
    )
    assert await protocol.get_tools() == ["mcp-tool"]
    config = created[0]
    assert config.url == "http://127.0.0.1:8000/tunnel/mcp/agent/docs"
    assert config.api_key == "internal-proxy-token"
    assert config.headers == {"Authorization": "Bearer internal-proxy-token"}
    assert config.tool_filter == ("search",)
    assert "internal-proxy-token" not in repr(config)

    toolset = protocol._toolset
    await protocol.close()
    await protocol.close()
    assert toolset.close_calls == 1
