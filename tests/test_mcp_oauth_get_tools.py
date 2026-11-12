"""Lazy OAuth tools fit caller-owned model/tool nodes without a routing model."""

from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.types import Command

from ksadk.mcp_runtime.oauth import langgraph as oauth
from ksadk.mcp_runtime.oauth.client import BrokerError
from ksadk.mcp_runtime.oauth.tools import McpOAuthTools
from tests.test_mcp_oauth_agent import CATALOG, ai_call


async def test_get_tools_does_not_connect_or_authorize_and_exposes_service_description(monkeypatch):
    discover = AsyncMock(side_effect=AssertionError("get_tools must not access MCP or IAM"))
    monkeypatch.setattr(oauth, "list_mcp_tools", discover)
    component = McpOAuthTools({"cloud": "查询云资源和 API 定义"})
    tools = await component.get_tools({"messages": [HumanMessage(content="hello")]})
    assert len(tools) == 1 and isinstance(tools[0], BaseTool)
    schema = convert_to_openai_tool(tools[0])["function"]
    assert schema["name"] == "connect_cloud"
    assert "查询云资源和 API 定义" in schema["description"]
    assert schema["parameters"]["properties"] == {}
    discover.assert_not_awaited()


async def test_invoking_discovery_returns_checkpoint_update_and_loads_real_tools(monkeypatch):
    discover = AsyncMock(return_value=CATALOG)
    monkeypatch.setattr(oauth, "list_mcp_tools", discover)
    component = McpOAuthTools({"cloud": "云资源"})
    state = {"messages": [ai_call("connect_cloud", "saved-call")]}
    tools = await component.get_tools(state)
    result = await tools[0].ainvoke(state["messages"][-1].tool_calls[0])
    assert isinstance(result, Command)
    discover.assert_awaited_once_with("cloud", request_key="saved-call")
    state = {**state, **result.update, "messages": state["messages"] + result.update["messages"]}
    loaded = await component.get_tools(state)
    assert [tool.name for tool in loaded] == ["cloud__SearchApis"]
    schema = convert_to_openai_tool(loaded[0])["function"]
    assert schema["parameters"] == CATALOG[0]["inputSchema"]
    assert CATALOG[0]["description"] in schema["description"]
    # One component shared by different sessions must never share their catalogs.
    assert [tool.name for tool in await component.get_tools({})] == ["connect_cloud"]


async def test_real_tool_uses_original_call_identity_arguments_and_no_default_approval(monkeypatch):
    invoke = AsyncMock(return_value={"isError": False})
    monkeypatch.setattr(oauth, "call_mcp_tool", invoke)
    component = McpOAuthTools(["cloud"])
    state = {
        "messages": [ai_call("cloud__SearchApis", "call", {"query": "chosen by model"})],
        "mcp_catalogs": {"cloud": CATALOG},
    }
    tool = (await component.get_tools(state))[0]
    result = await tool.ainvoke(state["messages"][-1].tool_calls[0])
    assert isinstance(result, Command)
    invoke.assert_awaited_once_with(
        "cloud",
        "SearchApis",
        {"query": "chosen by model"},
        request_key="call",
        policy=None,
        raise_on_error=False,
    )
    assert result.update["messages"][0].tool_call_id == "call"
    assert "mcp_catalogs" not in tool.model_dump()


async def test_stale_or_foreign_tool_call_cannot_use_a_bound_oauth_tool(monkeypatch):
    discover = AsyncMock()
    monkeypatch.setattr(oauth, "list_mcp_tools", discover)
    state = {"messages": [ai_call("connect_cloud", "saved")]}
    tool = (await McpOAuthTools(["cloud"]).get_tools(state))[0]
    for call in [
        {"name": "connect_cloud", "id": "other", "args": {}},
        {"name": "legacy_key", "id": "saved", "args": {}},
        {"name": "connect_cloud", "id": "saved", "args": {"spoof": True}},
        {},
    ]:
        with pytest.raises(BrokerError, match="MCP_TOOL_CALL_NOT_PENDING"):
            await tool.ainvoke(call)
    discover.assert_not_awaited()


async def test_toolnode_accepts_discovery_command_and_preserves_interrupt(monkeypatch):
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.prebuilt import ToolNode
    from langgraph.types import interrupt

    from ksadk.mcp_runtime.oauth.tools import McpOAuthState

    async def discover(*args, **kwargs):
        interrupt("consent")
        return CATALOG

    monkeypatch.setattr(oauth, "list_mcp_tools", discover)
    component = McpOAuthTools({"cloud": "cloud resources"})

    async def tool_node(state, config):
        return await ToolNode(await component.get_tools(state)).ainvoke(state, config=config)

    graph = StateGraph(McpOAuthState)
    graph.add_node("tools", tool_node)
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    compiled = graph.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "native-toolnode"}}
    result = await compiled.ainvoke({"messages": [ai_call("connect_cloud", "call")]}, config)
    assert result["__interrupt__"]
    result = await compiled.ainvoke(Command(resume=True), config)
    assert result["mcp_catalogs"] == {"cloud": CATALOG}
    assert result["messages"][-1].tool_call_id == "call"
    assert isinstance(result["messages"][0], AIMessage)
