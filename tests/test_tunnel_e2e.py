from __future__ import annotations

import asyncio
from contextlib import suppress

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from pydantic import PrivateAttr

from ksadk.tunnel import (
    LangGraphTunnelTools,
    LocalServer,
    TunnelConnector,
    TunnelRegistry,
    TunnelToolset,
    mount_tunnel,
)
from tests.harness.fixtures.mcp_server import run_fixture_mcp_server, run_http_app

_FAKE_TUNNEL_TOKEN = "fixture-tunnel-token"
_FAKE_LOCAL_MCP_TOKEN = "fixture-local-mcp-token"


class _ScriptedModel(FakeMessagesListChatModel):
    _bound_tools: list = PrivateAttr(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        self._bound_tools.append(tools)
        return self


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


@pytest.mark.asyncio
@pytest.mark.local_process_heavy
@pytest.mark.parametrize("execution_mode", ["invoke", "legacy", "canonical"])
async def test_local_mcp_tunnel_langgraph_toolnode_list_call_and_disconnect(
    monkeypatch, tmp_path, execution_mode
):
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
            label="tunnel_langgraph_fixture",
            required_token=_FAKE_LOCAL_MCP_TOKEN,
        ) as local_mcp,
        run_http_app(app) as cloud_url,
    ):
        monkeypatch.setenv("KSADK_ENABLE_TUNNEL", "1")
        monkeypatch.setenv("KSADK_TUNNEL_TOKEN", _FAKE_TUNNEL_TOKEN)
        registry.self_url = cloud_url
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
        provider = LangGraphTunnelTools("demo_agent")
        try:
            await _wait_for_server_count(registry, 1, connector_task)

            model = _ScriptedModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "fixture__lookup",
                                "args": {"value": "hello"},
                                "id": "langgraph-call-1",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            )

            async def current_tools():
                return await provider.get_tools()

            async def model_node(state, config):
                reply = await model.bind_tools(await current_tools()).ainvoke(
                    state["messages"], config=config
                )
                return {"messages": [reply]}

            async def tools_node(state, config):
                return await ToolNode(await current_tools()).ainvoke(state, config=config)

            workflow = StateGraph(MessagesState)
            workflow.add_node("model", model_node)
            workflow.add_node("tools", tools_node)
            workflow.add_edge(START, "model")
            workflow.add_conditional_edges("model", tools_condition)
            workflow.add_edge("tools", "model")
            graph = workflow.compile()

            from types import SimpleNamespace

            from ksadk.detection.detector import DetectionResult, FrameworkType
            from ksadk.runners.langgraph_runner import LangGraphRunner
            from ksadk.runtime.factory import build_default_runtime_registry
            from ksadk.runtime.launch import RuntimeLaunchContext, RuntimeServices

            detection = DetectionResult(
                type=FrameworkType.LANGGRAPH,
                name="demo_agent",
                entry_point="agent.py",
                package_path=str(tmp_path),
            )
            runner = LangGraphRunner(detection, str(tmp_path))
            context = RuntimeLaunchContext(
                runtime_type="langgraph",
                project_dir=tmp_path,
                detection=detection,
                services=RuntimeServices(
                    tunnel_registry=registry,
                    runner_factory=lambda *_: runner,
                ),
            )
            build_default_runtime_registry().create(context)
            monkeypatch.setattr(
                "ksadk.runners.langgraph_runner.load_agent_module",
                lambda *args, **kwargs: (graph, SimpleNamespace()),
            )
            runner.load_agent()
            request = {
                "input": "lookup hello",
                "session_id": "langgraph-e2e",
                "run_id": "langgraph-e2e-run",
            }
            if execution_mode == "invoke":
                result = await runner.invoke({**request, "_ksadk_force_graph_invoke": True})
                assert result["output"] == "done"
            elif execution_mode == "legacy":
                chunks = [chunk async for chunk in runner.stream(request)]
                assert chunks
            else:
                events = [event async for event in runner.stream_canonical_events(request)]
                assert events

            assert [tool.name for tool in model._bound_tools[0]] == ["fixture__lookup"]
            assert len(model._bound_tools) == 2
            assert ("lookup", "hello") in local_mcp.log.calls
            assert local_mcp.log.authorization
            assert set(local_mcp.log.authorization) == {f"Bearer {_FAKE_LOCAL_MCP_TOKEN}"}
            assert registry.proxy_token not in str(local_mcp.log.authorization)
            assert _FAKE_TUNNEL_TOKEN not in str(local_mcp.log.authorization)

            connector_task.cancel()
            with suppress(asyncio.CancelledError):
                await connector_task
            await _wait_for_server_count(registry, 0, connector_task)
            offline_provider = LangGraphTunnelTools(
                "demo_agent", registry=registry, self_url=cloud_url
            )
            assert await offline_provider.get_tools() == []
        finally:
            connector_task.cancel()
            with suppress(asyncio.CancelledError):
                await connector_task
            await registry.close()
