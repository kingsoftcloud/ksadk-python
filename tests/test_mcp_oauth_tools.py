"""Developer-owned graphs keep legacy tools separate from OAuth execution."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ksadk.mcp_runtime.oauth import create_mcp_agent
from ksadk.mcp_runtime.oauth import langgraph as oauth
from ksadk.mcp_runtime.oauth.tools import McpOAuthState, McpOAuthTools
from ksadk.mcp_runtime.oauth.tools import _pending
from ksadk.runners.langgraph_runner import LangGraphRunner
from tests.test_mcp_oauth_agent import CATALOG, ScriptedModel, ai_call
from tests.test_mcp_oauth_local import local_env as local_env
from tests.test_mcp_oauth_local import provider as provider


def custom_graph(
    model, oauth_tools, legacy, *, checkpointer=None, before=None, discover=None, tool_objects=False
):
    async def begin(state):
        if before is not None:
            before.append("business-before")
        return oauth_tools.reset(state)

    async def chat(state, config):
        definitions = (
            await oauth_tools.get_tools(state) if tool_objects else oauth_tools.model_tools(state)
        )
        result = await model.bind_tools(definitions).ainvoke(state["messages"], config=config)
        return {"messages": [result]}

    async def old_tools(state, config):
        call = _pending(state)
        return {"messages": [await legacy.ainvoke(call, config=config)]}

    async def oauth_node(state, config):
        tools = {t.name: t for t in await oauth_tools.get_tools(state)}
        call = _pending(state)
        return await tools[call["name"]].ainvoke(call, config=config)

    def route(state):
        if state.get("mcp_stop"):
            return "stop"
        call = _pending(state)
        if call is None:
            return END if isinstance(state["messages"][-1], AIMessage) else "model"
        return "oauth" if oauth_tools.owns(call, state) else "legacy"

    # All nodes, state and edges belong to the caller; SDK never compiles this graph.
    graph = StateGraph(McpOAuthState)
    graph.add_node("business", begin)
    graph.add_node("model", chat)
    graph.add_node("legacy", old_tools)
    graph.add_node("oauth", oauth_node if tool_objects else oauth_tools.execute)
    graph.add_node("stop", oauth_tools.stop)
    graph.add_edge(START, "business")
    if discover:

        async def discover_node(state):
            catalog = await oauth.list_mcp_tools(discover, request_key="discover-node")
            return {"mcp_catalogs": {discover: catalog}}

        graph.add_node("discover", discover_node)
        graph.add_edge("business", "discover")
        graph.add_edge("discover", "model")
    else:
        graph.add_edge("business", "model")
    for name in ["model", "legacy", "oauth"]:
        graph.add_conditional_edges(name, route)
    graph.add_edge("stop", END)
    return graph.compile(checkpointer=checkpointer)


@pytest.mark.parametrize("tool_objects", [False, True])
async def test_custom_nodes_and_legacy_tools_survive_oauth_cold_resume(
    monkeypatch, tmp_path, tool_objects
):
    from langchain_core.tools import tool

    before, reads = [], []

    @tool
    async def old_key_tool(query: str):
        """Unchanged developer-owned tool; SDK OAuth must never wrap or authorize it."""
        reads.append(query)
        return "legacy result"

    async def discovery(*args, **kwargs):
        interrupt("OAuth consent")
        return CATALOG

    invoke = AsyncMock(return_value={"isError": False})
    monkeypatch.setattr(oauth, "list_mcp_tools", discovery)
    monkeypatch.setattr(oauth, "call_mcp_tool", invoke)
    model = ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "old_key_tool", "args": {"query": "before"}, "id": "legacy-1"},
                    {"name": "connect_cloud", "args": {}, "id": "connect"},
                ],
            )
        ]
    )
    tools = McpOAuthTools({"cloud": "cloud API"})
    config = {"configurable": {"thread_id": "developer-owned-thread"}}
    path = str(tmp_path / "state.sqlite")
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        graph = custom_graph(
            model, tools, old_key_tool, checkpointer=saver, before=before, tool_objects=tool_objects
        )
        result = await graph.ainvoke({"messages": [HumanMessage(content="query")]}, config)
        assert result["__interrupt__"] and reads == ["before"] and len(before) == 1
    final_model = ScriptedModel(
        responses=[
            ai_call("cloud__SearchApis", "query", {"query": "resource"}),
            ai_call("old_key_tool", "legacy-2", {"query": "after"}),
            AIMessage(content="done"),
        ]
    )
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        graph = custom_graph(
            final_model,
            tools,
            old_key_tool,
            checkpointer=saver,
            before=before,
            tool_objects=tool_objects,
        )
        result = await graph.ainvoke(Command(resume=True), config)
    assert reads == ["before", "after"] and len(before) == 1
    assert invoke.await_args.args == ("cloud", "SearchApis", {"query": "resource"})
    assert invoke.await_args.kwargs["request_key"] == "query"
    assert result["messages"][-1].content == "done"
    assert not hasattr(graph.builder, "_ksadk_mcp_agent")
    assert tools.model_tools(result)[0]["function"]["parameters"] == CATALOG[0]["inputSchema"]


async def test_oauth_component_never_dispatches_a_legacy_tool(monkeypatch):
    invoke = AsyncMock()
    monkeypatch.setattr(oauth, "call_mcp_tool", invoke)
    tools = McpOAuthTools(["cloud"])
    state = {"messages": [ai_call("old_key_tool", "old")]}
    assert not tools.owns(_pending(state), state)
    with pytest.raises(ValueError, match="not owned"):
        await tools.execute(state, {})
    invoke.assert_not_awaited()


@pytest.mark.parametrize("approval", [False, True])
async def test_explicit_discovery_binds_only_provider_tools_and_developer_policy(
    monkeypatch, approval
):
    from ksadk.tools.gateway import ToolPolicy

    policy = ToolPolicy(requires_approval=True)
    options = {"execution_policies": {"cloud": {"SearchApis": policy}}} if approval else {}
    tools = McpOAuthTools(["cloud"], discovery_tools=False, **options)
    assert tools.model_tools({}) == []
    assert not tools.owns({"name": "connect_cloud"}, {})
    state = {
        "messages": [ai_call("cloud__SearchApis", "search", {"query": "test"})],
        "mcp_catalogs": {"cloud": CATALOG},
    }
    assert [t["function"]["name"] for t in tools.model_tools(state)] == ["cloud__SearchApis"]
    invoke = AsyncMock(return_value={"isError": False})
    monkeypatch.setattr(oauth, "call_mcp_tool", invoke)
    await tools.execute(state, {})
    assert invoke.await_args.kwargs["policy"] == (policy if approval else None)


def test_execution_policy_rejects_unknown_connections_and_untyped_values():
    from ksadk.tools.gateway import ToolPolicy

    for policies in [
        {"other": {"CallApi": ToolPolicy()}},
        {"cloud": {"CallApi": {"requires_approval": True}}},
    ]:
        with pytest.raises(ValueError, match="execution_policies"):
            McpOAuthTools(["cloud"], execution_policies=policies)


async def test_rejection_stops_pending_calls_and_reset_clears_catalog(monkeypatch):
    from ksadk.mcp_runtime.oauth.client import BrokerError

    monkeypatch.setattr(
        oauth, "list_mcp_tools", AsyncMock(side_effect=BrokerError("TOOL_APPROVAL_DENIED"))
    )
    tools = McpOAuthTools(["cloud"])
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "connect_cloud", "args": {}, "id": "denied"},
                    {"name": "old_key_tool", "args": {}, "id": "must-not-run"},
                ],
            )
        ]
    }
    update = await tools.execute(state, {})
    state = {**state, **update, "messages": state["messages"] + update["messages"]}
    assert state["mcp_stop"]
    stopped = tools.stop(state)
    assert isinstance(stopped["messages"][0], ToolMessage)
    assert stopped["messages"][0].tool_call_id == "must-not-run"
    assert tools.reset(state) == {"mcp_catalogs": {}, "mcp_stop": False}


async def test_sdk_graph_auto_checkpoint_and_custom_factory_precedence(tmp_path, monkeypatch):
    from langgraph.checkpoint.memory import InMemorySaver

    from tests.test_mcp_oauth_agent import ScriptedModel

    graph = create_mcp_agent(ScriptedModel(responses=[AIMessage(content="hello")]), mcp_servers=[])
    runner = LangGraphRunner(SimpleNamespace(name="plain"), str(tmp_path))
    runner._agent, runner._module = graph, SimpleNamespace()
    monkeypatch.setenv("KSADK_CHECKPOINT_PATH", str(tmp_path / "checkpoint.sqlite"))
    await runner._bind_local_checkpoint(backend="sqlite")
    try:
        assert runner._agent.checkpointer is runner._sdk_local_saver
        assert runner._sdk_mcp_agent() is not None
        state = runner._prepare_state_with_hook(
            {"input": "new"}, "s", [{"role": "user", "content": "old"}]
        )
        assert [m.content for m in state["messages"]] == ["new"]
        saver = InMemorySaver()
        calls = []

        def factory(*, checkpointer):
            calls.append(checkpointer)
            return graph.copy(update={"checkpointer": checkpointer})

        runner._module = SimpleNamespace(ksadk_graph_factory=factory)
        assert runner._graph_factory()(checkpointer=saver).checkpointer is saver
        assert calls == [saver]
    finally:
        await runner._close_local_checkpoint()


@pytest.mark.parametrize(
    "mode", ["lazy", "explicit", "get_tools", "encapsulated", "native", "native_nested"]
)
async def test_local_kernel_custom_graph_with_unchanged_key_client(
    local_env, tmp_path, provider, mode
):
    import httpx
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import tool
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    from ksadk.mcp_runtime import MCPServerConfig
    from tests.test_mcp_oauth_local import ask, completed, consent, stack

    explicit_discovery = mode == "explicit"

    key_config = MCPServerConfig(
        "legacy", "https://fixture.example.test/key-mcp", "fixture-static-key"
    )

    @tool
    async def legacy_key_read():
        """Existing Key client: no OAuth configuration or SDK auth routing."""

        def http(headers=None, timeout=None, auth=None):
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(provider), headers=headers, timeout=timeout, auth=auth
            )

        async with streamablehttp_client(
            key_config.url, headers=key_config.headers, httpx_client_factory=http
        ) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                result = await client.call_tool("read_snapshot", {})
                return result.model_dump_json()

    class Model(BaseChatModel):
        @property
        def _llm_type(self):
            return "custom-graph-fixture"

        def bind_tools(self, tools, **kwargs):
            if explicit_discovery:
                assert tools and not any(
                    t["function"]["name"].startswith("connect_") for t in tools
                )
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            count = sum(isinstance(m, ToolMessage) for m in messages)
            calls = [
                "legacy_key_read",
                *([] if explicit_discovery else ["connect_research"]),
                "research__read_snapshot",
                "legacy_key_read",
            ]
            message = (
                AIMessage(content="custom mixed done")
                if count == len(calls)
                else ai_call(calls[count], f"call-{count}")
            )
            if mode.startswith("native") and count == 0:
                message = AIMessage(
                    content="",
                    tool_calls=[
                        {"name": calls[0], "id": "call-0", "args": {}},
                        {"name": calls[1], "id": "call-1", "args": {}},
                    ],
                )
            return ChatResult(generations=[ChatGeneration(message=message)])

    prelude = []

    def factory():
        tools = McpOAuthTools(["research"], discovery_tools=not explicit_discovery)
        if mode.startswith("native"):
            from tests.test_mcp_oauth_native_toolnode import native_graph

            child = native_graph(
                Model(),
                tools,
                extra=[legacy_key_read],
                before=lambda state: prelude.append("business-before") or {},
            )
            if mode == "native_nested":
                from langgraph.graph import MessagesState

                parent = StateGraph(MessagesState)
                parent.add_node("agent", child)
                parent.add_edge(START, "agent")
                parent.add_edge("agent", END)
                return parent.compile()
            return child
        if mode == "encapsulated":
            from tests.test_mcp_oauth_tool_node import graph_for

            return graph_for(
                Model(),
                tools,
                tools=[legacy_key_read],
                before=lambda state: prelude.append("business-before") or {},
            )
        return custom_graph(
            Model(),
            tools,
            legacy_key_read,
            before=prelude,
            discover="research" if explicit_discovery else None,
            tool_objects=mode == "get_tools",
        )

    async with stack(tmp_path, provider, [], [], agent_factory=factory) as s:
        wait = await ask(s)
        target = s.local.oauth.broker.data["waits"][wait["wait_id"]]["runtime_ref"]
        assert provider.state.stats["key_calls"] == (0 if explicit_discovery else 1)
    async with stack(tmp_path, provider, [], [], agent_factory=factory) as s:
        await s.local.ensure()
        await consent(s, provider, wait)
        await completed(s, wait)
        assert provider.state.stats["key_calls"] == 2
        assert provider.state.stats["calls"] == provider.state.stats["exchanges"] == 1
        assert len(prelude) == 1
        assert s.local.oauth.broker.data["waits"][wait["wait_id"]]["runtime_ref"] == target
        for path in tmp_path.rglob("*"):
            if path.is_file():
                assert b"fixture-static-key" not in path.read_bytes()


def test_oauth_publishing_does_not_take_over_legacy_key_config(monkeypatch):
    import json

    from ksadk.deployment.mcp import project_mcp_declarations
    from ksadk.mcp_runtime import load_mcp_server_configs
    from tests.test_mcp_oauth_local import DECLARATION

    legacy = {"name": "legacy", "url": "http://localhost:9000/mcp", "api_key": "fake-key"}
    raw = json.dumps([legacy])
    monkeypatch.setenv("KSADK_MCP_SERVERS", raw)
    assert len(project_mcp_declarations({"mcp_servers": [legacy, DECLARATION]})) == 1
    config = load_mcp_server_configs()[0]
    assert config.headers == {"Authorization": "Bearer fake-key"}
    assert config.auth_mode is None and config.tool_filter == ()
