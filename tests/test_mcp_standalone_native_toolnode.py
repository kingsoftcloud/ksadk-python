"""OAuth-only and unchanged Key-only clients work without a mixed tool batch."""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from tests.test_mcp_oauth_agent import ScriptedModel, ai_call
from tests.test_mcp_oauth_local import local_env as local_env
from tests.test_mcp_oauth_local import provider as provider


def standalone_graph(model, get_tools, *, nested=False):
    async def chat(state, config):
        message = await model.bind_tools(await get_tools(state)).ainvoke(state["messages"], config)
        return {"messages": [message]}

    async def execute(state, config):
        return await ToolNode(await get_tools(state)).ainvoke(state, config)

    graph = StateGraph(MessagesState)
    graph.add_node("model", chat)
    graph.add_node("tools", execute)
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", tools_condition)
    graph.add_edge("tools", "model")
    if nested:
        parent = StateGraph(MessagesState)
        parent.add_node("agent", graph.compile())
        parent.add_edge(START, "agent")
        parent.add_edge("agent", END)
        return parent.compile()
    return graph.compile()


@pytest.mark.parametrize("nested", [False, True])
async def test_single_oauth_mcp_native_kernel_cold_resume(local_env, tmp_path, provider, nested):
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.outputs import ChatGeneration, ChatResult

    from ksadk.mcp_runtime.oauth.tools import McpOAuthTools
    from tests.test_mcp_oauth_local import ask, completed, consent, stack

    class Model(BaseChatModel):
        @property
        def _llm_type(self):
            return "oauth-only-fixture"

        def bind_tools(self, tools, **kwargs):
            assert tools and all(
                t.name == "connect_research" or t.name.startswith("research__") for t in tools
            )
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            count = sum(isinstance(m, ToolMessage) for m in messages)
            names = ["connect_research", "research__read_snapshot"]
            reply = ai_call(names[count], names[count]) if count < 2 else AIMessage(content="done")
            return ChatResult(generations=[ChatGeneration(message=reply)])

    def factory():
        oauth = McpOAuthTools(["research"])

        async def get_tools(state):
            return await oauth.get_tools(state)  # No tools=, no Key client.

        return standalone_graph(Model(), get_tools, nested=nested)

    async with stack(tmp_path, provider, [], [], agent_factory=factory) as s:
        wait = await ask(s)
        target = s.local.oauth.broker.data["waits"][wait["wait_id"]]["runtime_ref"]
        assert provider.state.stats["key_calls"] == provider.state.stats["calls"] == 0
    async with stack(tmp_path, provider, [], [], agent_factory=factory) as s:
        await s.local.ensure()
        await consent(s, provider, wait)
        await completed(s, wait)
        assert provider.state.stats["key_calls"] == 0
        assert provider.state.stats["exchanges"] == provider.state.stats["calls"] == 1
        assert s.local.oauth.broker.data["waits"][wait["wait_id"]]["runtime_ref"] == target


@pytest.mark.parametrize("nested", [False, True])
async def test_single_key_mcp_uses_unwrapped_client_tools(monkeypatch, local_env, tmp_path, provider, nested):
    from fastmcp import FastMCP
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    from tests.agui.test_runtime_adapter import _input
    from tests.test_mcp_oauth_local import stack

    from tests.test_mcp_runtime import _run_fastmcp_http_server

    def forbidden(*args, **kwargs):
        raise AssertionError("Key-only Agent must not initialize OAuth")

    monkeypatch.setattr("ksadk.mcp_runtime.oauth.tools.McpOAuthTools.__init__", forbidden)
    monkeypatch.setattr("ksadk.mcp_runtime.oauth.local.persistence.LocalVault.__init__", forbidden)
    calls, authorized = [], []
    server = FastMCP("existing-key-client")

    @server.tool
    def read_snapshot() -> str:
        calls.append(1)
        return "snapshot"

    async def require_key(request, call_next):
        if request.headers.get("Authorization") != "Bearer fixture-static-key":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        authorized.append(1)
        return await call_next(request)

    app = server.http_app(path="/mcp", transport="streamable-http")
    app.add_middleware(BaseHTTPMiddleware, dispatch=require_key)
    with _run_fastmcp_http_server(app) as base:
        client = MultiServerMCPClient(
            {
                "key": {
                    "url": base + "/mcp",
                    "transport": "streamable_http",
                    "headers": {"Authorization": "Bearer fixture-static-key"},
                }
            }
        )
        original = await client.get_tools()

        async def get_tools(state):
            return original  # Direct client tools, no OAuth wrappers.

        def graph():
            return standalone_graph(
                ScriptedModel(responses=[ai_call("read_snapshot", "read"), AIMessage(content="done")]),
                get_tools, nested=nested,
            )
        result = await asyncio.wait_for(
            graph().ainvoke({"messages": [HumanMessage(content="read")]}), timeout=15
        )
        assert calls == [1] and authorized
        # The same developer-owned client also runs through local Web with a
        # nonempty Key-only MCP declaration. Neither call needs an OAuth vault.
        entries = [{"name": "key", "auth_mode": "api_key", "url": base + "/mcp"}]
        async with stack(tmp_path, provider, [], [], declarations=entries, agent_factory=graph) as s:
            reply = await s.client.post("/agentengine/agui", json=_input().model_dump(mode="json", by_alias=True))
            assert reply.status_code == 200 and "done" in reply.text
            assert "mcp.authorization.requested" not in reply.text
            assert s.local.oauth.broker is None and s.local.oauth.socket is None
            assert not s.local.oauth.directory.exists()
    assert calls == [1, 1]
    assert result["messages"][-1].content == "done"
    assert len([m for m in result["messages"] if isinstance(m, ToolMessage)]) == 1
    assert "__interrupt__" not in result
    assert "oauth" not in json.dumps([t.name for t in original]).lower()
