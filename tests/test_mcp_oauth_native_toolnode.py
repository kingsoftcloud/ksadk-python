"""Native ToolNode with SDK-returned tools: no graph rewriting or custom nodes."""

from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import Command, interrupt

from ksadk.mcp_runtime.oauth import langgraph as oauth
from ksadk.mcp_runtime.oauth.client import BrokerError
from ksadk.mcp_runtime.oauth.tools import McpOAuthState, McpOAuthTools
from tests.test_mcp_oauth_agent import CATALOG, ScriptedModel, ai_call


def native_graph(
    model, component, *, extra=(), saver=None, before=None, schema=MessagesState, nested=False
):
    async def get_tools(state):
        return await component.get_tools(state, tools=extra)

    async def chat(state, config):
        reply = await model.bind_tools(await get_tools(state)).ainvoke(state["messages"], config)
        return {"messages": [reply]}

    async def execute(state, config):
        return await ToolNode(await get_tools(state)).ainvoke(state, config)

    graph = StateGraph(schema)
    graph.add_node("model", chat)
    graph.add_node("tools", execute)
    if before:
        graph.add_node("business", before)
        graph.add_edge(START, "business")
        graph.add_edge("business", "model")
    else:
        graph.add_edge(START, "model")
    graph.add_conditional_edges("model", tools_condition)
    graph.add_edge("tools", "model")
    if nested:
        parent = StateGraph(schema)
        parent.add_node("agent", graph.compile())
        parent.add_edge(START, "agent")
        parent.add_edge("agent", END)
        return parent.compile(checkpointer=saver)
    return graph.compile(checkpointer=saver)


@pytest.mark.parametrize("schema", [MessagesState, McpOAuthState])
async def test_native_toolnode_accepts_multiple_oauth_calls(monkeypatch, schema):
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
                    {"name": "alpha__SearchApis", "id": "qa", "args": {"query": "one"}},
                    {"name": "beta__SearchApis", "id": "qb", "args": {"query": "two"}},
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    result = await native_graph(model, McpOAuthTools(["alpha", "beta"]), schema=schema).ainvoke(
        {"messages": [HumanMessage(content="both")]}
    )
    assert [c.args[0] for c in invoke.await_args_list] == ["alpha", "beta"]
    assert all(c.kwargs["policy"] is None for c in invoke.await_args_list)
    assert {t.name for t in model._bound_tools[1]} == {"alpha__SearchApis", "beta__SearchApis"}
    assert result["messages"][-1].content == "done"
    if schema is MessagesState:
        assert set(result) == {"messages"}
    else:
        assert result["mcp_catalogs"] == {"alpha": CATALOG, "beta": CATALOG}


@pytest.mark.parametrize("nested", [False, True])
async def test_key_then_oauth_same_native_batch_cold_resume_executes_key_once(
    monkeypatch, tmp_path, nested
):
    executed, prelude = [], []

    @tool
    async def legacy_key(query: str):
        """Existing Key tool: own client and authentication, no OAuth configuration."""
        executed.append(query)
        return "key result"

    async def discover(*args, **kwargs):
        interrupt("consent")
        return CATALOG

    monkeypatch.setattr(oauth, "list_mcp_tools", discover)
    monkeypatch.setattr(oauth, "call_mcp_tool", AsyncMock(return_value={"isError": False}))
    model = ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "legacy_key", "id": "key", "args": {"query": "once"}},
                    {"name": "connect_cloud", "id": "connect", "args": {}},
                ],
            )
        ]
    )

    def before(state):
        prelude.append(1)
        return {}

    path = str(tmp_path / "native.sqlite")
    config = {"configurable": {"thread_id": "original"}}
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        first = native_graph(
            model,
            McpOAuthTools(["cloud"]),
            extra=[legacy_key],
            saver=saver,
            before=before,
            nested=nested,
        )
        result = await first.ainvoke({"messages": [HumanMessage(content="query")]}, config)
        assert result["__interrupt__"]
        assert executed == ["once"] and prelude == [1]
    # New graph objects and connection: only the persisted saver survives.
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        final = ScriptedModel(
            responses=[
                ai_call("cloud__SearchApis", "read", {"query": "vpc"}),
                AIMessage(content="done"),
            ]
        )
        restored = native_graph(
            final,
            McpOAuthTools(["cloud"]),
            extra=[legacy_key],
            saver=saver,
            before=before,
            nested=nested,
        )
        result = await restored.ainvoke(Command(resume=True), config)
    assert executed == ["once"] and prelude == [1]
    assert [m.tool_call_id for m in result["messages"] if isinstance(m, ToolMessage)] == [
        "key",
        "connect",
        "read",
    ]
    assert result["messages"][-1].content == "done"


async def test_later_oauth_wait_reuses_finished_oauth_and_key_results(monkeypatch, tmp_path):
    calls = []

    @tool
    async def key():
        """Unchanged ordinary tool."""
        calls.append("key")
        return "ok"

    async def discover(connection, **kwargs):
        if connection == "beta":
            interrupt("second consent")
        calls.append(connection)
        return CATALOG

    monkeypatch.setattr(oauth, "list_mcp_tools", discover)
    model = ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "connect_alpha", "id": "a", "args": {}},
                    {"name": "key", "id": "key", "args": {}},
                    {"name": "connect_beta", "id": "b", "args": {}},
                ],
            )
        ]
    )
    config = {"configurable": {"thread_id": "second-wait"}}
    saver = InMemorySaver()
    result = await native_graph(
        model, McpOAuthTools(["alpha", "beta"]), extra=[key], saver=saver
    ).ainvoke({"messages": [HumanMessage(content="both")]}, config)
    assert result["__interrupt__"] and calls == ["alpha", "key"]
    result = await native_graph(
        ScriptedModel(responses=[AIMessage(content="done")]),
        McpOAuthTools(["alpha", "beta"]),
        extra=[key],
        saver=saver,
    ).ainvoke(Command(resume=True), config)
    assert calls == ["alpha", "key", "beta"]
    assert result["messages"][-1].content == "done"


async def test_native_tools_keep_injected_state_and_command_reducers():
    import operator
    from typing import Annotated

    from langgraph.prebuilt import InjectedState

    class State(MessagesState):
        count: Annotated[int, operator.add]

    @tool
    async def old_tool(state: Annotated[dict, InjectedState]):
        """Original business tool with native state injection."""
        assert state["count"] == 5
        return Command(
            update={"count": 2, "messages": [ToolMessage(content="ok", tool_call_id="old")]}
        )

    model = ScriptedModel(responses=[ai_call("old_tool", "old"), AIMessage(content="done")])
    result = await native_graph(
        model, McpOAuthTools(["cloud"]), extra=[old_tool], schema=State
    ).ainvoke({"messages": [HumanMessage(content="count")], "count": 5})
    assert result["count"] == 7


async def test_plain_chat_and_existing_client_are_not_modified(monkeypatch):
    forbidden = AsyncMock(side_effect=AssertionError("must not discover"))
    monkeypatch.setattr(oauth, "list_mcp_tools", forbidden)

    @tool
    async def legacy(query: str):
        """Existing Key tool."""
        return query

    original = legacy.ainvoke.__func__
    component = McpOAuthTools(["cloud"])
    tools = await component.get_tools({}, tools=[legacy])
    assert tools[0] is not legacy
    assert legacy.ainvoke.__func__ is original
    assert tools[0].name == legacy.name and tools[0].args == legacy.args
    result = await native_graph(
        ScriptedModel(responses=[AIMessage(content="hello")]), component
    ).ainvoke({"messages": [HumanMessage(content="hi")]})
    assert result["messages"][-1].content == "hello"
    forbidden.assert_not_awaited()


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
    result = await native_graph(model, McpOAuthTools(["cloud"]), extra=[legacy]).ainvoke(
        {"messages": [HumanMessage(content="query")]}
    )
    assert not calls
    replies = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in replies] == ["deny", "later"]
    assert all(m.status == "error" for m in replies)


async def test_corrupt_nested_receipt_fails_without_reexecuting_tool(monkeypatch):
    from copy import deepcopy

    class CorruptOnResume(InMemorySaver):
        corrupt = False

        async def aget_tuple(self, config):
            saved = await super().aget_tuple(config)
            if saved and self.corrupt:
                writes = deepcopy(saved.pending_writes)
                for _, channel, value in writes:
                    if (
                        channel == "__return__"
                        and isinstance(value, dict)
                        and value.get("call", {}).get("name") == "key"
                    ):
                        value["call"]["args"] = {"forged": True}
                return saved._replace(pending_writes=writes)
            return saved

    calls = []

    @tool
    async def key():
        """A completed ordinary tool must not run again on a corrupt receipt."""
        calls.append(1)
        return "ok"

    async def discover(*args, **kwargs):
        interrupt("consent")
        return CATALOG

    monkeypatch.setattr(oauth, "list_mcp_tools", discover)
    model = ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "key", "id": "key", "args": {}},
                    {"name": "connect_cloud", "id": "connect", "args": {}},
                ],
            )
        ]
    )
    saver = CorruptOnResume()
    graph = native_graph(model, McpOAuthTools(["cloud"]), extra=[key], saver=saver, nested=True)
    config = {"configurable": {"thread_id": "corrupt"}}
    first = await graph.ainvoke({"messages": [HumanMessage(content="query")]}, config)
    assert first["__interrupt__"] and calls == [1]
    saver.corrupt = True
    with pytest.raises(BrokerError, match="MCP_TOOL_REPLAY_MISMATCH"):
        await graph.ainvoke(Command(resume=True), config)
    assert calls == [1]
