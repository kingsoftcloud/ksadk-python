"""Real LangGraph execution against the authorized WebSocket/MCP tunnel."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from ksadk._mcp_compat import MCPServer
from mcp.types import CallToolResult, TextContent

from ksadk.detection import DetectionResult, FrameworkType
from ksadk.mcp_runtime.oauth import create_mcp_agent
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime.factory import create_runtime_adapter
from ksadk.runtime.launch import RuntimeLaunchContext, RuntimeServices
from ksadk.tunnel import TunnelError, TunnelTools
from ksadk.tunnel.runtime import TunnelBinding
from ksadk.tunnel.server import TunnelAgentPolicy
from tests.test_mcp_tunnel import cloud, connected, connector, eventually, local_mcp, serve


class Model(BaseChatModel):
    """Stateless model: select a discovered tool, then echo its actual result."""

    preferred: str = "echo"

    @property
    def _llm_type(self):
        return "deterministic-tunnel-fixture"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools])

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if isinstance(messages[-1], ToolMessage):
            result = json.loads(messages[-1].content)
            response = AIMessage(content=result.get("content", [{"text": "failed"}])[0]["text"])
        else:
            definition = next(
                (
                    tool
                    for tool in kwargs.get("tools", [])
                    if self.preferred in tool["function"]["name"]
                ),
                None,
            )
            if definition is None:
                response = AIMessage(content="offline")
            else:
                arguments = {"text": messages[-1].content} if self.preferred == "echo" else {}
                response = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": definition["function"]["name"],
                            "args": arguments,
                            "id": "call-1",
                        }
                    ],
                )
        return ChatResult(generations=[ChatGeneration(message=response)])


class Statuses(AsyncCallbackHandler):
    def __init__(self):
        self.outputs = []

    async def on_tool_end(self, output, **kwargs):
        self.outputs.append(output)


def runner_for(graph, module, registry, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "ksadk.runners.langgraph_runner.load_agent_module", lambda *_a, **_k: (graph, module)
    )
    binding = TunnelBinding(
        TunnelAgentPolicy("deployment-17", "fake-tunnel-token", ("erp",)), registry
    )
    detection = DetectionResult(
        type=FrameworkType.LANGGRAPH,
        name="display-name",
        entry_point="agent.py",
        package_path=str(tmp_path),
        agent_variable="root_agent",
    )
    context = RuntimeLaunchContext(
        "langgraph", tmp_path, detection=detection, services=RuntimeServices(tunnel_binding=binding)
    )
    adapter = create_runtime_adapter(context)
    adapter._runner.load_agent()
    return adapter._runner


async def test_sdk_graph_discovers_and_calls_real_mcp_without_mutating_shared_graph(
    tmp_path, monkeypatch
):
    declared = create_mcp_agent(Model(), mcp_servers=[]).with_config({"tags": ["preserved"]})
    async with local_mcp() as (endpoint, calls, _, _), cloud() as (_, registry, url, _):
        runner = runner_for(declared, SimpleNamespace(), registry, tmp_path, monkeypatch)
        try:
            assert runner._agent is not declared
            assert declared.builder._ksadk_mcp_agent.tunnel_tools is None
            assert runner._agent.config["tags"] == ["preserved"]
            assert (await runner._agent.ainvoke({"messages": [HumanMessage(content="before")]}))[
                "messages"
            ][-1].content == "offline"
            async with connected(connector(url, endpoint)):
                outputs = Statuses()
                result = await runner._agent.ainvoke(
                    {"messages": [HumanMessage(content="你好🌏")]}, {"callbacks": [outputs]}
                )
                (observed,) = outputs.outputs
                assert observed.artifact["structuredContent"] == {"text": "你好🌏"}
                assert result["messages"][-1].content == "你好🌏"
                tool_message = next(m for m in result["messages"] if isinstance(m, ToolMessage))
                assert tool_message.status == "success"
                assert json.loads(tool_message.content)["structuredContent"] == {"text": "你好🌏"}
                catalog = result["mcp_tunnel_catalog"]
                assert len(catalog) == 2
                assert "fake-local" not in json.dumps(catalog)
                assert "127.0.0.1" not in json.dumps(catalog)
                assert calls == ["你好🌏"]
                # The imported root graph remains a valid independent graph.
                assert (await declared.ainvoke({"messages": [HumanMessage(content="isolated")]}))[
                    "messages"
                ][-1].content == "offline"
            await eventually(lambda: registry.connections("deployment-17") == [])
            assert (await runner._agent.ainvoke({"messages": [HumanMessage(content="after")]}))[
                "messages"
            ][-1].content == "offline"
        finally:
            await runner.close()


class CustomState(MessagesState):
    tunnel_catalog: list[dict]


def custom_graph(*, checkpointer, tunnel_tools, preferred="echo"):
    model = Model(preferred=preferred)

    async def chat(state, config):
        catalog = await tunnel_tools.discover()
        tools = await tunnel_tools.get_tools(catalog)
        response = await model.bind_tools(tools).ainvoke(state["messages"], config)
        return {"messages": [response], "tunnel_catalog": catalog}

    async def execute(state, config):
        tools = await tunnel_tools.get_tools(state["tunnel_catalog"])
        return await ToolNode(tools).ainvoke(state, config)

    builder = StateGraph(CustomState)
    builder.add_node("chat", chat)
    builder.add_node("tools", execute)
    builder.add_edge(START, "chat")
    builder.add_conditional_edges("chat", tools_condition)
    builder.add_edge("tools", "chat")
    return builder.compile(checkpointer=checkpointer)


async def test_custom_stategraph_explicit_factory_real_toolnode_and_error_status(
    tmp_path, monkeypatch
):
    mcp = MCPServer("business-error")

    @mcp.tool()
    async def business_error() -> CallToolResult:
        return CallToolResult(
            content=[TextContent(type="text", text="business-rejected")],
            structuredContent={"reason": "permission"},
            isError=True,
        )

    outputs = Statuses()
    declared = StateGraph(MessagesState)
    declared.add_node("original", lambda state: {})
    declared.add_edge(START, "original")
    declared.add_edge("original", END)
    root = declared.compile()
    factories = []

    def factory(*, checkpointer, tunnel_tools):
        factories.append((checkpointer, tunnel_tools))
        return custom_graph(
            checkpointer=checkpointer, tunnel_tools=tunnel_tools, preferred="business_error"
        )

    from tests.mcp_http_fixture import streamable_app

    async with serve(streamable_app(mcp, stateless_http=True, json_response=True)) as local, cloud() as (_, registry, url, _):
        runner = runner_for(
            root, SimpleNamespace(ksadk_graph_factory=factory), registry, tmp_path, monkeypatch
        )
        try:
            assert len(factories) == 1
            async with connected(connector(url, local + "/mcp", filters=("business_error",))):
                result = await runner._agent.ainvoke(
                    {"messages": [HumanMessage(content="perform")]}, {"callbacks": [outputs]}
                )
                message = next(m for m in result["messages"] if isinstance(m, ToolMessage))
                assert message.status == "error"
                assert message.artifact["isError"] is True
                assert message.artifact["structuredContent"] == {"reason": "permission"}
                assert result["messages"][-1].content == "business-rejected"
                assert len(outputs.outputs) == 1
                assert outputs.outputs[0].status == "error"
        finally:
            await runner.close()


async def test_langchain_tool_snapshot_refuses_new_connector_generation():
    async with local_mcp() as (endpoint, calls, _, _), cloud() as (_, registry, url, _):
        tools = TunnelTools("deployment-17", registry)
        async with connected(connector(url, endpoint)):
            catalog = await tools.discover()
            stale = next(t for t in await tools.get_tools(catalog) if "echo" in t.name)
            old = registry.connections("deployment-17")[0][1]
            await old.websocket.close(code=1012)
            await eventually(
                lambda: bool(registry.connections("deployment-17"))
                and registry.connections("deployment-17")[0][1] is not old
            )
            with pytest.raises(TunnelError, match="disconnected"):
                await stale.ainvoke(
                    {
                        "type": "tool_call",
                        "id": "stale",
                        "name": stale.name,
                        "args": {"text": "must not rebind"},
                    }
                )
            fresh = next(t for t in await tools.get_tools() if "echo" in t.name)
            message = await fresh.ainvoke(
                {
                    "type": "tool_call",
                    "id": "fresh",
                    "name": fresh.name,
                    "args": {"text": "new-generation"},
                }
            )
            assert message.artifact["structuredContent"] == {"text": "new-generation"}
            assert calls == ["new-generation"]


async def test_two_apps_share_imported_sdk_graph_without_sharing_tunnel_bindings(
    tmp_path, monkeypatch
):
    declared = create_mcp_agent(Model(), mcp_servers=[])
    async with (
        local_mcp() as (endpoint, calls, _, _),
        cloud() as (_, first, url, _),
        cloud() as (_, second, url2, _),
    ):
        one = runner_for(declared, SimpleNamespace(), first, tmp_path, monkeypatch)
        two = runner_for(declared, SimpleNamespace(), second, tmp_path, monkeypatch)
        disabled = LangGraphRunner(
            SimpleNamespace(entry_point="agent.py", agent_variable="root_agent"), str(tmp_path)
        )
        disabled.load_agent()
        try:
            assert one._tunnel_tools._registry is first
            assert two._tunnel_tools._registry is second
            assert disabled._agent is declared
            async with connected(connector(url, endpoint)):
                assert (await one._agent.ainvoke({"messages": [HumanMessage(content="first")]}))[
                    "messages"
                ][-1].content == "first"
                assert (await two._agent.ainvoke({"messages": [HumanMessage(content="second")]}))[
                    "messages"
                ][-1].content == "offline"
                assert (
                    await disabled._agent.ainvoke({"messages": [HumanMessage(content="disabled")]})
                )["messages"][-1].content == "offline"
                async with connected(connector(url2, endpoint)):
                    await one.close()
                    assert (
                        await two._agent.ainvoke(
                            {"messages": [HumanMessage(content="still-online")]}
                        )
                    )["messages"][-1].content == "still-online"
                assert calls == ["first", "still-online"]
        finally:
            await one.close()
            await two.close()
            await disabled.close()


async def test_sdk_unknown_outcome_stops_graph_without_model_replay(tmp_path, monkeypatch):
    declared = create_mcp_agent(Model(preferred="wait_forever"), mcp_servers=[])
    async with (
        local_mcp() as (endpoint, calls, started, _),
        cloud() as (_, registry, url, _),
    ):
        runner = runner_for(declared, SimpleNamespace(), registry, tmp_path, monkeypatch)
        try:
            async with connected(connector(url, endpoint)):
                connection = registry.connections("deployment-17")[0][1]
                request = asyncio.create_task(
                    runner._agent.ainvoke({"messages": [HumanMessage(content="start")]})
                )
                await asyncio.wait_for(started.wait(), 5)
                await connection.websocket.close(code=1012)
                state = await asyncio.wait_for(request, 5)
                assert state["mcp_tunnel_unknown"]
                assert state["mcp_call_count"] == 1
                assert state["messages"][-1].content.startswith("工具调用结果未知")
                assert calls == ["wait"]
        finally:
            await runner.close()


def test_custom_graph_requires_explicit_factory_integration(tmp_path, monkeypatch):
    builder = StateGraph(MessagesState)
    builder.add_node("answer", lambda _: {"messages": [AIMessage(content="plain")]})
    builder.add_edge(START, "answer")
    builder.add_edge("answer", END)
    graph = builder.compile()
    from ksadk.tunnel import TunnelRegistry

    with pytest.raises(TypeError, match="tunnel_tools"):
        runner_for(graph, SimpleNamespace(), TunnelRegistry(), tmp_path, monkeypatch)
    with pytest.raises(TypeError, match="tunnel_tools"):
        runner_for(
            graph,
            SimpleNamespace(ksadk_graph_factory=lambda *, checkpointer: graph),
            TunnelRegistry(),
            tmp_path,
            monkeypatch,
        )


@pytest.mark.parametrize("sdk", [False, True])
async def test_checkpoint_rebuild_and_model_reload_keep_binding_and_call_factory_once(
    tmp_path, monkeypatch, sdk
):
    from ksadk.tunnel import TunnelRegistry

    saver = InMemorySaver()
    declared = create_mcp_agent(Model(), mcp_servers=[], checkpointer=saver)
    calls = []

    def factory(*, checkpointer, tunnel_tools):
        calls.append((checkpointer, tunnel_tools))
        return custom_graph(checkpointer=checkpointer, tunnel_tools=tunnel_tools)

    module = SimpleNamespace() if sdk else SimpleNamespace(ksadk_graph_factory=factory)
    runner = runner_for(declared, module, TunnelRegistry(), tmp_path, monkeypatch)
    try:
        bound = runner._tunnel_tools
        assert runner._agent.checkpointer is saver
        calls.clear()
        # Simulate an already prepared managed saver; no database is needed to
        # prove the reload path calls the developer's factory only once.
        runner._managed_checkpoint_pool = SimpleNamespace()
        runner._load_agent(force_reload=True)
        assert runner._agent.checkpointer is saver
        assert runner._tunnel_tools is bound
        assert len(calls) == (0 if sdk else 1)
        new_saver = InMemorySaver()
        rebuilt = runner._graph_factory()(checkpointer=new_saver)
        assert rebuilt.checkpointer is new_saver
        if sdk:
            assert rebuilt.builder._ksadk_mcp_agent.tunnel_tools is bound
        else:
            assert calls[-1] == (new_saver, bound)
    finally:
        runner._managed_checkpoint_pool = None
        await runner.close()


def test_langgraph_tunnel_import_and_sdk_graph_do_not_require_adk():
    script = """
import importlib.abc
import sys
class NoAdk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "google.adk" or fullname.startswith("google.adk."):
            raise ModuleNotFoundError("ADK absent", name=fullname)
sys.meta_path.insert(0, NoAdk())
from ksadk.tunnel import TunnelTools, TunnelRegistry
from ksadk.mcp_runtime.oauth import create_mcp_agent
assert TunnelTools("stable", TunnelRegistry()) is not None
assert not any(name.startswith("google.adk") for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


async def test_wrapped_sdk_graph_requires_integration_on_factory_result(tmp_path, monkeypatch):
    root = create_mcp_agent(Model(), mcp_servers=[])

    def wrap(child, checkpointer):
        builder = StateGraph(MessagesState)
        builder.add_node("child", child)
        builder.add_edge(START, "child")
        builder.add_edge("child", END)
        return builder.compile(checkpointer=checkpointer)

    def legacy_factory(*, checkpointer):
        return wrap(root, checkpointer)

    def integrated_factory(*, checkpointer, tunnel_tools):
        child = custom_graph(checkpointer=None, tunnel_tools=tunnel_tools)
        return wrap(child, checkpointer)

    async with local_mcp() as (endpoint, calls, _, _), cloud() as (_, registry, url, _):
        with pytest.raises(TypeError, match="tunnel_tools"):
            runner_for(
                root,
                SimpleNamespace(ksadk_graph_factory=legacy_factory),
                registry,
                tmp_path,
                monkeypatch,
            )
        runner = runner_for(
            root,
            SimpleNamespace(ksadk_graph_factory=integrated_factory),
            registry,
            tmp_path,
            monkeypatch,
        )
        try:
            async with connected(connector(url, endpoint)):
                result = await runner._agent.ainvoke(
                    {"messages": [HumanMessage(content="wrapped")]}
                )
                assert result["messages"][-1].content == "wrapped"
                assert calls == ["wrapped"]
        finally:
            await runner.close()
