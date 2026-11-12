"""Public Agent factory: model-selected tools, independent checkpoints and no local coupling."""

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import PrivateAttr

from ksadk.mcp_runtime.oauth import create_mcp_agent


class ScriptedModel(FakeMessagesListChatModel):
    _bound_tools: list = PrivateAttr(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        self._bound_tools.append(tools)
        return self


async def test_plain_chat_has_no_mcp_access(monkeypatch):
    from ksadk.mcp_runtime.oauth import langgraph as oauth

    async def forbidden(*args, **kwargs):
        raise AssertionError("plain chat must not access MCP or IAM")

    monkeypatch.setattr(oauth, "list_mcp_tools", forbidden)
    monkeypatch.setattr(oauth, "call_mcp_tool", forbidden)
    model = ScriptedModel(responses=[AIMessage(content="hello")])
    graph = create_mcp_agent(model, mcp_servers=["cloud"])
    result = await graph.ainvoke({"messages": [HumanMessage(content="hello")]})
    assert result["messages"][-1].content == "hello"
    assert len(model._bound_tools) == 1


def ai_call(name, call_id, args=None):
    return AIMessage(content="", tool_calls=[{"name": name, "id": call_id, "args": args or {}}])


CATALOG = [
    {
        "name": "SearchApis",
        "description": "Provider-owned description",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
]


async def test_model_uses_provider_schema_and_original_arguments(monkeypatch):
    from unittest.mock import AsyncMock

    from ksadk.mcp_runtime.oauth import langgraph as oauth

    discover = AsyncMock(return_value=CATALOG)
    invoke = AsyncMock(
        return_value={"content": [{"type": "text", "text": "real result"}], "isError": False}
    )
    monkeypatch.setattr(oauth, "list_mcp_tools", discover)
    monkeypatch.setattr(oauth, "call_mcp_tool", invoke)
    model = ScriptedModel(
        responses=[
            ai_call("connect_cloud", "connect"),
            ai_call("cloud__SearchApis", "read", {"query": "from model"}),
            AIMessage(content="done"),
        ]
    )
    graph = create_mcp_agent(
        model, mcp_servers={"cloud": "Cloud API docs"}, read_only_tools={"cloud": ["SearchApis"]}
    )
    result = await graph.ainvoke({"messages": [HumanMessage(content="query")]})
    discover.assert_awaited_once_with("cloud", request_key="connect")
    assert model._bound_tools[1][0]["function"]["parameters"] == CATALOG[0]["inputSchema"]
    assert invoke.await_args.args == ("cloud", "SearchApis", {"query": "from model"})
    assert invoke.await_args.kwargs["request_key"] == "read"
    assert invoke.await_args.kwargs["policy"].risk_level == "low"
    assert result["messages"][-1].content == "done"


async def test_two_mcp_servers_with_same_tool_name_remain_isolated(monkeypatch):
    from unittest.mock import AsyncMock

    from ksadk.mcp_runtime.oauth import langgraph as oauth

    monkeypatch.setattr(oauth, "list_mcp_tools", AsyncMock(return_value=CATALOG))
    invoke = AsyncMock(return_value={"isError": False})
    monkeypatch.setattr(oauth, "call_mcp_tool", invoke)
    model = ScriptedModel(
        responses=[
            ai_call("connect_alpha", "c1"),
            ai_call("connect_beta", "c2"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "alpha__SearchApis", "args": {"query": "a"}, "id": "a"},
                    {"name": "beta__SearchApis", "args": {"query": "b"}, "id": "b"},
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    graph = create_mcp_agent(model, mcp_servers=["alpha", "beta"])
    await graph.ainvoke({"messages": [HumanMessage(content="both")]})
    assert [call.args[0] for call in invoke.await_args_list] == ["alpha", "beta"]
    assert all(call.kwargs["policy"] is None for call in invoke.await_args_list)
    assert {tool["function"]["name"] for tool in model._bound_tools[2]} == {
        "alpha__SearchApis",
        "beta__SearchApis",
    }


async def test_later_tool_interrupt_does_not_replay_completed_call_or_model(monkeypatch, tmp_path):
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.types import Command, interrupt

    from ksadk.mcp_runtime.oauth import langgraph as oauth

    async def discover(*_, **__):
        return CATALOG

    calls = []

    async def invoke(connection, tool, args, *, request_key, **kwargs):
        if request_key == "second":
            interrupt("authorize second")
        calls.append(request_key)
        return {"isError": False}

    monkeypatch.setattr(oauth, "list_mcp_tools", discover)
    monkeypatch.setattr(oauth, "call_mcp_tool", invoke)
    model = ScriptedModel(
        responses=[
            ai_call("connect_cloud", "connect"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "cloud__SearchApis", "id": "first", "args": {"query": "one"}},
                    {"name": "cloud__SearchApis", "id": "second", "args": {"query": "two"}},
                ],
            ),
        ]
    )
    config = {"configurable": {"thread_id": "original-thread"}}
    path = str(tmp_path / "checkpoint.sqlite")
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        graph = create_mcp_agent(model, mcp_servers=["cloud"], checkpointer=saver)
        result = await graph.ainvoke({"messages": [HumanMessage(content="both")]}, config)
        assert result["__interrupt__"]
        assert calls == ["first"] and len(model._bound_tools) == 2
    # A new graph and a newly opened saver continue the saved second call.
    final_model = ScriptedModel(responses=[AIMessage(content="done")])
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        graph = create_mcp_agent(final_model, mcp_servers=["cloud"], checkpointer=saver)
        result = await graph.ainvoke(Command(resume=True), config)
    assert calls == ["first", "second"]
    assert result["messages"][-1].content == "done"
    assert len(final_model._bound_tools) == 1


async def test_optional_policies_and_tool_errors_still_apply(monkeypatch):
    from unittest.mock import AsyncMock

    from ksadk.mcp_runtime.oauth import BrokerError
    from ksadk.mcp_runtime.oauth import langgraph as oauth

    monkeypatch.setattr(oauth, "list_mcp_tools", AsyncMock(return_value=CATALOG))
    invoke = AsyncMock(side_effect=BrokerError("MCP_TOOL_ARGUMENTS_NOT_ALLOWED"))
    monkeypatch.setattr(oauth, "call_mcp_tool", invoke)
    model = ScriptedModel(
        responses=[
            ai_call("connect_cloud", "connect"),
            ai_call("cloud__SearchApis", "call", {"query": "denied"}),
            AIMessage(content="denied"),
        ]
    )
    result = await create_mcp_agent(model, mcp_servers=["cloud"]).ainvoke(
        {"messages": [HumanMessage(content="query")]}
    )
    assert result["messages"][-2].status == "error"
    assert "MCP_TOOL_ARGUMENTS_NOT_ALLOWED" in result["messages"][-2].content
    assert invoke.await_args.kwargs["policy"] is None


async def test_rejected_approval_stops_remaining_model_calls(monkeypatch):
    from unittest.mock import AsyncMock

    from ksadk.mcp_runtime.oauth import BrokerError
    from ksadk.mcp_runtime.oauth import langgraph as oauth

    monkeypatch.setattr(oauth, "list_mcp_tools", AsyncMock(return_value=CATALOG))
    invoke = AsyncMock(side_effect=BrokerError("TOOL_APPROVAL_DENIED"))
    monkeypatch.setattr(oauth, "call_mcp_tool", invoke)
    model = ScriptedModel(
        responses=[
            ai_call("connect_cloud", "connect"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "cloud__SearchApis", "id": "denied", "args": {"query": "one"}},
                    {"name": "cloud__SearchApis", "id": "must-not-run", "args": {"query": "two"}},
                ],
            ),
        ]
    )
    result = await create_mcp_agent(model, mcp_servers=["cloud"]).ainvoke(
        {"messages": [HumanMessage(content="query")]}
    )
    assert invoke.await_count == 1
    assert "停止" in result["messages"][-1].content
    assert result["messages"][-2].tool_call_id == "must-not-run"
    assert result["messages"][-2].status == "error"


async def test_existing_langchain_tools_are_supported_without_mcp(monkeypatch):
    from langchain_core.tools import tool

    @tool
    def add_one(number: int) -> int:
        """Add one to the number."""
        return number + 1

    model = ScriptedModel(
        responses=[ai_call("add_one", "add", {"number": 4}), AIMessage(content="5")]
    )
    result = await create_mcp_agent(model, mcp_servers=["cloud"], tools=[add_one]).ainvoke(
        {"messages": [HumanMessage(content="4+1")]}
    )
    assert result["messages"][-2].content == "5"
    assert not result["mcp_catalogs"]


@pytest.mark.parametrize("servers", ["cloud", ["cloud", "cloud"], {"cloud": None}])
def test_invalid_configuration_is_rejected_without_network(servers):
    with pytest.raises(ValueError):
        create_mcp_agent(ScriptedModel(responses=[]), mcp_servers=servers)


async def test_new_turn_rediscovers_catalog_but_shares_no_global_cache(monkeypatch):
    from unittest.mock import AsyncMock

    from langgraph.checkpoint.memory import InMemorySaver

    from ksadk.mcp_runtime.oauth import langgraph as oauth

    discover = AsyncMock(return_value=CATALOG)
    monkeypatch.setattr(oauth, "list_mcp_tools", discover)
    model = ScriptedModel(
        responses=[
            ai_call("connect_cloud", "c1"),
            AIMessage(content="ready"),
            AIMessage(content="hello"),
        ]
    )
    graph = create_mcp_agent(model, mcp_servers=["cloud"], checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "thread"}}
    await graph.ainvoke({"messages": [HumanMessage(content="connect")]}, config)
    result = await graph.ainvoke({"messages": [HumanMessage(content="just chat")]}, config)
    assert discover.await_count == 1
    assert not result["mcp_catalogs"]
    assert model._bound_tools[-1][0]["function"]["name"] == "connect_cloud"


async def test_call_limit_closes_unanswered_tool_calls(monkeypatch):
    from unittest.mock import AsyncMock

    from ksadk.mcp_runtime.oauth import langgraph as oauth

    monkeypatch.setattr(oauth, "list_mcp_tools", AsyncMock(return_value=CATALOG))
    model = ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "connect_cloud", "id": "first", "args": {}},
                    {"name": "connect_cloud", "id": "second", "args": {}},
                ],
            )
        ]
    )
    result = await create_mcp_agent(model, mcp_servers=["cloud"], max_tool_calls=1).ainvoke(
        {"messages": [HumanMessage(content="connect")]}
    )
    assert result["messages"][-2].tool_call_id == "second"
    assert result["messages"][-2].status == "error"
    assert "上限" in result["messages"][-1].content


async def test_provider_may_reuse_call_id_on_a_new_user_turn(monkeypatch):
    from unittest.mock import AsyncMock

    from langgraph.checkpoint.memory import InMemorySaver

    from ksadk.mcp_runtime.oauth import langgraph as oauth

    discover = AsyncMock(return_value=CATALOG)
    monkeypatch.setattr(oauth, "list_mcp_tools", discover)
    model = ScriptedModel(
        responses=[
            ai_call("connect_cloud", "same-id"),
            AIMessage(content="done"),
            ai_call("connect_cloud", "same-id"),
            AIMessage(content="done again"),
        ]
    )
    graph = create_mcp_agent(model, mcp_servers=["cloud"], checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "thread"}}
    await graph.ainvoke({"messages": [HumanMessage(content="first")]}, config)
    result = await graph.ainvoke({"messages": [HumanMessage(content="second")]}, config)
    assert result["messages"][-1].content == "done again"
    assert discover.await_count == 2


async def test_reused_call_id_inside_a_turn_is_rejected(monkeypatch):
    from unittest.mock import AsyncMock

    from ksadk.mcp_runtime.oauth import langgraph as oauth
    from ksadk.mcp_runtime.oauth.client import BrokerError

    monkeypatch.setattr(oauth, "list_mcp_tools", AsyncMock(return_value=CATALOG))
    model = ScriptedModel(responses=[ai_call("connect_cloud", "same")] * 2)
    with pytest.raises(BrokerError, match="MCP_MODEL_TOOL_CALL_ID_INVALID"):
        await create_mcp_agent(model, mcp_servers=["cloud"]).ainvoke(
            {"messages": [HumanMessage(content="connect")]}
        )
