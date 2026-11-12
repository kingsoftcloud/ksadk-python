"""Caller-owned graphs bind OAuth tools to LangGraph's native ToolNode."""

from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import Command, interrupt

from ksadk.mcp_runtime.oauth import langgraph as oauth
from ksadk.mcp_runtime.oauth.client import BrokerError
from ksadk.mcp_runtime.oauth.tools import McpOAuthTools
from tests.test_mcp_oauth_agent import CATALOG, ScriptedModel, ai_call


def graph_for(model, component, *, tools=(), saver=None, before=None, state_schema=MessagesState):
    async def chat(state, config):
        reply = await model.bind_tools([*tools, *await component.get_tools(state)]).ainvoke(
            state["messages"], config=config
        )
        return {"messages": [reply]}

    graph = StateGraph(state_schema)
    graph.add_node("model", chat)
    async def execute(state, config):
        return await ToolNode(await component.get_tools(state, tools=tools)).ainvoke(state, config=config)

    graph.add_node("tools", execute)
    if before:
        graph.add_node("business", before)
        graph.add_edge(START, "business")
        graph.add_edge("business", "model")
    else:
        graph.add_edge(START, "model")
    graph.add_conditional_edges("model", tools_condition)
    graph.add_edge("tools", "model")
    return graph.compile(checkpointer=saver)


async def test_messages_only_graph_discovers_and_calls_without_auth_routing(monkeypatch):
    discover = AsyncMock(return_value=CATALOG)
    invoke = AsyncMock(return_value={"isError": False})
    monkeypatch.setattr(oauth, "list_mcp_tools", discover)
    monkeypatch.setattr(oauth, "call_mcp_tool", invoke)
    model = ScriptedModel(
        responses=[
            ai_call("connect_cloud", "connect"),
            ai_call("cloud__SearchApis", "query", {"query": "user input"}),
            AIMessage(content="done"),
        ]
    )
    component = McpOAuthTools({"cloud": "Cloud API"})
    result = await graph_for(model, component).ainvoke(
        {"messages": [HumanMessage(content="query")]}
    )
    assert set(result) == {"messages"}
    assert result["messages"][-1].content == "done"
    assert [t.name for t in model._bound_tools[1]] == ["cloud__SearchApis"]
    assert invoke.await_args.args == ("cloud", "SearchApis", {"query": "user input"})
    assert invoke.await_args.kwargs["policy"] is None
    # Tools and clients are per-call objects, not checkpoint or global state.
    assert [t.name for t in await component.get_tools({})] == ["connect_cloud"]


async def test_plain_chat_never_contacts_iam_or_mcp(monkeypatch):
    discover = AsyncMock(side_effect=AssertionError("unexpected MCP discovery"))
    monkeypatch.setattr(oauth, "list_mcp_tools", discover)
    result = await graph_for(
        ScriptedModel(responses=[AIMessage(content="hello")]), McpOAuthTools(["cloud"])
    ).ainvoke({"messages": [HumanMessage(content="hi")]})
    assert result["messages"][-1].content == "hello"
    discover.assert_not_awaited()


async def test_cold_resume_does_not_repeat_key_tool_or_business_node(monkeypatch, tmp_path):
    calls, business = [], []

    @tool
    async def legacy_key(query: str):
        """Existing API Key tool, with its own unchanged client."""
        calls.append(query)
        return "key result"

    async def discover(*args, **kwargs):
        interrupt("consent")
        return CATALOG

    monkeypatch.setattr(oauth, "list_mcp_tools", discover)
    monkeypatch.setattr(oauth, "call_mcp_tool", AsyncMock(return_value={"isError": False}))

    def before(state):
        business.append("once")
        return {}

    initial = ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "legacy_key", "id": "key-before", "args": {"query": "before"}},
                    {"name": "connect_cloud", "id": "connect", "args": {}},
                    {"name": "cloud__SearchApis", "id": "query", "args": {"query": "one"}},
                    {"name": "legacy_key", "id": "key-after", "args": {"query": "after"}},
                ],
            )
        ]
    )
    config = {"configurable": {"thread_id": "cold"}}
    path = str(tmp_path / "graph.sqlite")
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        result = await graph_for(
            initial, McpOAuthTools(["cloud"]), tools=[legacy_key], saver=saver, before=before
        ).ainvoke({"messages": [HumanMessage(content="query")]}, config)
        assert result["__interrupt__"] and calls == ["before"] and business == ["once"]
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        restored = graph_for(
            ScriptedModel(responses=[AIMessage(content="done")]),
            McpOAuthTools(["cloud"]),
            tools=[legacy_key],
            saver=saver,
            before=before,
        )
        result = await restored.ainvoke(Command(resume=True), config)
    assert calls == ["before", "after"] and business == ["once"]
    assert result["messages"][-1].content == "done"
    assert [m.tool_call_id for m in result["messages"] if isinstance(m, ToolMessage)] == [
        "key-before",
        "connect",
        "query",
        "key-after",
    ]


async def test_new_turn_refreshes_catalog_without_user_reset_and_handles_stale_name(monkeypatch):
    discover = AsyncMock(return_value=CATALOG)
    invoke = AsyncMock(return_value={"isError": False})
    monkeypatch.setattr(oauth, "list_mcp_tools", discover)
    monkeypatch.setattr(oauth, "call_mcp_tool", invoke)
    model = ScriptedModel(
        responses=[
            ai_call("connect_cloud", "c1"),
            AIMessage(content="done"),
            ai_call("cloud__SearchApis", "stale", {"query": "followup"}),
            ai_call("connect_cloud", "c2"),
            ai_call("cloud__SearchApis", "query", {"query": "new"}),
            AIMessage(content="done again"),
        ]
    )
    graph = graph_for(model, McpOAuthTools(["cloud"]), saver=InMemorySaver())
    config = {"configurable": {"thread_id": "followup"}}
    await graph.ainvoke({"messages": [HumanMessage(content="first")]}, config)
    result = await graph.ainvoke({"messages": [HumanMessage(content="again")]}, config)
    stale = next(
        m for m in result["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "stale"
    )
    assert stale.status == "error" and "connect_cloud" in stale.content
    assert discover.await_count == 2 and invoke.await_count == 1
    assert result["messages"][-1].content == "done again"


async def test_multiple_connections_load_independently_in_one_batch(monkeypatch):
    monkeypatch.setattr(oauth, "list_mcp_tools", AsyncMock(return_value=CATALOG))
    invoke = AsyncMock(return_value={"isError": False})
    monkeypatch.setattr(oauth, "call_mcp_tool", invoke)
    model = ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "connect_alpha", "id": "a", "args": {}},
                    {"name": "connect_beta", "id": "b", "args": {}},
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "alpha__SearchApis", "id": "qa", "args": {"query": "a"}},
                    {"name": "beta__SearchApis", "id": "qb", "args": {"query": "b"}},
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    result = await graph_for(model, McpOAuthTools(["alpha", "beta"])).ainvoke(
        {"messages": [HumanMessage(content="both")]}
    )
    assert [c.args[0] for c in invoke.await_args_list] == ["alpha", "beta"]
    assert {t.name for t in model._bound_tools[-1]} == {"alpha__SearchApis", "beta__SearchApis"}
    assert result["messages"][-1].content == "done"


async def test_explicit_rejection_closes_batch_without_running_later_tools(monkeypatch):
    monkeypatch.setattr(
        oauth, "list_mcp_tools", AsyncMock(side_effect=BrokerError("TOOL_APPROVAL_DENIED"))
    )
    calls = []

    @tool
    async def legacy():
        """Must not execute after the user rejected this batch."""
        calls.append(1)
        return "bad"

    model = ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "connect_cloud", "id": "deny", "args": {}},
                    {"name": "legacy", "id": "later", "args": {}},
                ],
            ),
            AIMessage(content="stopped"),
        ]
    )
    result = await graph_for(model, McpOAuthTools(["cloud"]), tools=[legacy]).ainvoke(
        {"messages": [HumanMessage(content="query")]}
    )
    assert not calls
    replies = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in replies] == ["deny", "later"]
    assert all(m.status == "error" for m in replies)


async def test_legacy_tool_keeps_custom_state_injection_and_command_updates():
    from typing import Annotated

    from langgraph.prebuilt import InjectedState

    class State(MessagesState):
        customer: str
        checked: bool

    @tool
    async def legacy(state: Annotated[dict, InjectedState]):
        """An existing tool with developer-owned state."""
        assert state["customer"] == "user-a"
        return Command(
            update={"checked": True, "messages": [ToolMessage(content="ok", tool_call_id="key")]}
        )

    model = ScriptedModel(responses=[ai_call("legacy", "key"), AIMessage(content="done")])
    result = await graph_for(
        model, McpOAuthTools(["cloud"]), tools=[legacy], state_schema=State
    ).ainvoke({"messages": [HumanMessage(content="query")], "customer": "user-a", "checked": False})
    assert result["checked"] is True and result["customer"] == "user-a"


async def test_custom_reducer_receives_only_new_tool_updates():
    import operator
    from typing import Annotated

    class State(MessagesState):
        total: Annotated[int, operator.add]

    @tool
    async def increment():
        """Return a caller-owned state update."""
        return Command(
            update={"total": 2, "messages": [ToolMessage(content="ok", tool_call_id="inc")]}
        )

    model = ScriptedModel(responses=[ai_call("increment", "inc"), AIMessage(content="done")])
    result = await graph_for(
        model, McpOAuthTools(["cloud"]), tools=[increment], state_schema=State
    ).ainvoke({"messages": [HumanMessage(content="query")], "total": 5})
    assert result["total"] == 7


async def test_catalog_is_a_scoped_artifact_not_model_input(monkeypatch):
    import json

    from langchain_core.messages import convert_to_openai_messages

    monkeypatch.setattr(oauth, "list_mcp_tools", AsyncMock(return_value=CATALOG))
    component = McpOAuthTools(
        {"cloud": {"url": "https://first.example/mcp", "scopes": ["mcp:read"]}}
    )
    result = await graph_for(
        ScriptedModel(responses=[ai_call("connect_cloud", "c"), AIMessage(content="done")]),
        component,
    ).ainvoke({"messages": [HumanMessage(content="query")]})
    assert [t.name for t in await component.get_tools(result)] == ["cloud__SearchApis"]
    changed = McpOAuthTools(
        {"cloud": {"url": "https://second.example/mcp", "scopes": ["mcp:read"]}}
    )
    assert [t.name for t in await changed.get_tools(result)] == ["connect_cloud"]
    assert "inputSchema" not in json.dumps(convert_to_openai_messages(result["messages"]))


async def test_extra_tool_name_collision_fails_before_dispatch(monkeypatch):
    discover = AsyncMock()
    monkeypatch.setattr(oauth, "list_mcp_tools", discover)

    @tool
    async def connect_cloud():
        """Conflicting developer tool must not shadow OAuth."""
        raise AssertionError("must not execute")

    model = ScriptedModel(responses=[ai_call("connect_cloud", "c")])
    with pytest.raises(BrokerError, match="MCP_TOOL_NAME_COLLISION"):
        await graph_for(model, McpOAuthTools(["cloud"]), tools=[connect_cloud]).ainvoke(
            {"messages": [HumanMessage(content="query")]}
        )
    discover.assert_not_awaited()


async def test_tool_control_command_addresses_the_developers_graph():
    from langgraph.graph import END

    @tool
    async def handoff():
        """An existing tool that selects a developer-owned node."""
        return Command(
            update={"messages": [ToolMessage(content="ok", tool_call_id="handoff")]},
            goto="business_result",
        )

    graph = StateGraph(MessagesState)
    graph.add_node("model", lambda state: {"messages": [ai_call("handoff", "handoff")]})
    component = McpOAuthTools(["cloud"])

    async def execute(state, config):
        return await ToolNode(await component.get_tools(state, tools=[handoff])).ainvoke(state, config=config)

    graph.add_node("tools", execute)
    graph.add_node("business_result", lambda state: {"messages": [AIMessage(content="routed")]})
    graph.add_edge(START, "model")
    graph.add_edge("model", "tools")
    graph.add_edge("business_result", END)
    result = await graph.compile().ainvoke({"messages": [HumanMessage(content="query")]})
    assert result["messages"][-1].content == "routed"
