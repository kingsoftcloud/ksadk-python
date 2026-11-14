from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from langchain_core.tools import tool

from ksadk.runners._langgraph_fresh_turn import _FRESH_READ, fresh_user_checkpoint
from ksadk.tunnel.config import TunnelConfig
from ksadk.tunnel.langgraph import LangGraphTunnelTools, _binding, bind_langgraph_tunnel
from ksadk.tunnel.registry import ServerDescriptor, TunnelRegistry

from .conftest import add_connection


def _tool(name: str):
    @tool(name)
    def fake_tool(value: str) -> str:
        """Return the tool name and input."""
        return f"{name}:{value}"

    return fake_tool


def _config(registry: TunnelRegistry, agent: str = "agent") -> TunnelConfig:
    return TunnelConfig(
        agent_name=agent,
        token="fixture-tunnel-token",
        self_url=registry.self_url or "http://127.0.0.1:8000",
    )


def _install_loader(monkeypatch: pytest.MonkeyPatch, discovered: dict[str, list[Any]]):
    calls = []

    async def load(session, *, connection, server_name, **kwargs):
        assert session is None
        calls.append({"connection": connection, "server_name": server_name, **kwargs})
        return discovered.get(server_name, [])

    monkeypatch.setattr("ksadk.tunnel.langgraph._load_mcp_tools", lambda: load)
    return calls


@pytest.mark.asyncio
async def test_dynamic_tools_filter_namespace_and_disconnect(monkeypatch):
    registry = TunnelRegistry()
    registry.self_url = "http://127.0.0.1:9000/"
    calls = _install_loader(
        monkeypatch,
        {"docs": [_tool("search"), _tool("admin")]},
    )
    provider = LangGraphTunnelTools("agent", registry)
    business = _tool("local")

    assert await provider.get_tools(tools=[business]) == [business]
    assert calls == []

    connection, _ = add_connection(
        registry,
        descriptors=[ServerDescriptor(name="docs", tool_filter=["search"])],
    )
    tools = await provider.get_tools(tools=[business])

    assert [item.name for item in tools] == ["local", "docs__search"]
    assert calls[0]["server_name"] == "docs"
    connection_config = calls[0]["connection"]
    assert connection_config["transport"] == "streamable_http"
    assert connection_config["url"] == "http://127.0.0.1:9000/tunnel/mcp/agent/docs"
    assert connection_config["headers"] == {"Authorization": f"Bearer {registry.proxy_token}"}
    assert callable(connection_config["httpx_client_factory"])

    registry.remove_connection(connection.connector_id, connection)
    add_connection(
        registry,
        connector_id="replacement",
        descriptors=[ServerDescriptor(name="docs", tool_filter=["admin"])],
    )
    assert [item.name for item in await provider.get_tools(tools=[business])] == [
        "local",
        "docs__admin",
    ]

    registry.remove_connection("replacement")
    assert await provider.get_tools(tools=[business]) == [business]


@pytest.mark.asyncio
async def test_same_mcp_name_on_multiple_servers_uses_server_namespace(monkeypatch):
    registry = TunnelRegistry()
    provider = LangGraphTunnelTools("agent", registry)
    add_connection(
        registry,
        descriptors=[ServerDescriptor(name="docs"), ServerDescriptor(name="inventory")],
    )
    _install_loader(
        monkeypatch,
        {"docs": [_tool("search")], "inventory": [_tool("search")]},
    )

    assert [item.name for item in await provider.get_tools()] == [
        "docs__search",
        "inventory__search",
    ]


@pytest.mark.asyncio
async def test_single_server_failure_does_not_hide_other_servers(monkeypatch, caplog):
    registry = TunnelRegistry()
    provider = LangGraphTunnelTools("agent", registry)
    add_connection(
        registry,
        descriptors=[ServerDescriptor(name="broken"), ServerDescriptor(name="docs")],
    )

    async def load(session, *, connection, server_name, **kwargs):
        if server_name == "broken":
            raise RuntimeError("fixture discovery failure")
        return [_tool("search")]

    monkeypatch.setattr("ksadk.tunnel.langgraph._load_mcp_tools", lambda: load)
    with caplog.at_level(logging.WARNING):
        tools = await provider.get_tools()

    assert [item.name for item in tools] == ["docs__search"]
    assert "broken" in caplog.text
    assert "RuntimeError" in caplog.text
    assert registry.proxy_token not in caplog.text


@pytest.mark.asyncio
async def test_final_tool_name_conflict_fails_closed(monkeypatch):
    registry = TunnelRegistry()
    provider = LangGraphTunnelTools("agent", registry)
    add_connection(registry)
    _install_loader(monkeypatch, {"docs": [_tool("search")]})

    with pytest.raises(ValueError, match="Duplicate LangGraph tool name"):
        await provider.get_tools(tools=[_tool("docs__search")])


@pytest.mark.asyncio
async def test_disabled_tunnel_preserves_business_tools_without_loader(monkeypatch):
    monkeypatch.delenv("KSADK_ENABLE_TUNNEL", raising=False)
    provider = LangGraphTunnelTools("agent")
    business = _tool("local")

    def fail_loader():
        raise AssertionError("disabled Tunnel must not load the MCP adapter")

    monkeypatch.setattr("ksadk.tunnel.langgraph._load_mcp_tools", fail_loader)
    assert await provider.get_tools(tools=[business]) == [business]


@pytest.mark.asyncio
async def test_enabled_tunnel_requires_runtime_binding(monkeypatch):
    monkeypatch.setenv("KSADK_ENABLE_TUNNEL", "1")
    provider = LangGraphTunnelTools("agent")

    with pytest.raises(RuntimeError, match="runtime execution context"):
        await provider.get_tools()


@pytest.mark.asyncio
async def test_runtime_binding_supplies_registry_and_self_url(monkeypatch):
    monkeypatch.setenv("KSADK_ENABLE_TUNNEL", "1")
    registry = TunnelRegistry()
    registry.self_url = "http://127.0.0.1:9100"
    add_connection(registry)
    calls = _install_loader(monkeypatch, {"docs": [_tool("search")]})
    provider = LangGraphTunnelTools("agent")

    with bind_langgraph_tunnel(_config(registry), registry):
        assert [item.name for item in await provider.get_tools()] == ["docs__search"]

    assert calls[0]["connection"]["url"] == "http://127.0.0.1:9100/tunnel/mcp/agent/docs"


@pytest.mark.asyncio
async def test_runtime_binding_rejects_agent_and_registry_mismatch(monkeypatch):
    monkeypatch.setenv("KSADK_ENABLE_TUNNEL", "1")
    registry = TunnelRegistry()
    other = TunnelRegistry()

    with bind_langgraph_tunnel(_config(registry), registry):
        with pytest.raises(ValueError, match="Agent mismatch"):
            await LangGraphTunnelTools("other-agent").get_tools()
        with pytest.raises(ValueError, match="must not be mixed"):
            await LangGraphTunnelTools("agent", other).get_tools()


@pytest.mark.asyncio
async def test_unusual_public_names_have_stable_bounded_aliases(monkeypatch):
    registry = TunnelRegistry()
    provider = LangGraphTunnelTools("agent", registry)
    add_connection(registry, descriptors=[ServerDescriptor(name="docs space")])
    _install_loader(monkeypatch, {"docs space": [_tool("search/docs")]})

    first = await provider.get_tools()
    second = await provider.get_tools()

    assert first[0].name == second[0].name
    assert first[0].name.startswith("docs_space_")
    assert "__" in first[0].name
    assert len(first[0].name) <= 50
    assert first[0].name != "search/docs"


def test_agent_name_is_required():
    with pytest.raises(ValueError, match="non-empty agent_name"):
        LangGraphTunnelTools(" ")


class _BoundRunner:
    def __init__(self, registry: TunnelRegistry) -> None:
        self._tunnel_config = _config(registry)
        self._tunnel_registry = registry

    @fresh_user_checkpoint
    async def invoke(self, input_data):
        return _binding.get()

    @fresh_user_checkpoint
    async def stream(self, input_data):
        yield _binding.get()
        yield _binding.get()


@pytest.mark.asyncio
async def test_execution_binding_covers_invoke_and_stream_without_yield_leak():
    registry = TunnelRegistry()
    runner = _BoundRunner(registry)

    assert (await runner.invoke({})).registry is registry
    assert _binding.get() is None
    async for current in runner.stream({}):
        assert current.registry is registry
        assert _binding.get() is None
    assert _binding.get() is None


@pytest.mark.asyncio
async def test_interleaved_app_streams_do_not_share_bindings():
    first = _BoundRunner(TunnelRegistry()).stream({})
    second = _BoundRunner(TunnelRegistry()).stream({})

    first_binding = await anext(first)
    assert _binding.get() is None
    second_binding = await anext(second)
    assert _binding.get() is None
    assert first_binding.registry is not second_binding.registry

    await first.aclose()
    await second.aclose()
    assert _binding.get() is None


@pytest.mark.asyncio
async def test_cancellation_restores_execution_and_fresh_checkpoint_contexts():
    registry = TunnelRegistry()
    runner = _BoundRunner(registry)
    started = asyncio.Event()

    @fresh_user_checkpoint
    async def blocking_stream(self, input_data):
        started.set()
        await asyncio.Event().wait()
        yield _binding.get()

    runner.stream = blocking_stream.__get__(runner)

    async def consume():
        async for _ in runner.stream({"input": "blocking"}):
            pass

    task = asyncio.create_task(consume())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _binding.get() is None
    assert _FRESH_READ.get() is None
