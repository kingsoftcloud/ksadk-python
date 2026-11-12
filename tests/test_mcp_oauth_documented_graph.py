"""Execute the published custom-graph example across turns and native waits."""

import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, interrupt

from ksadk.mcp_runtime.oauth import langgraph as oauth
from tests.test_mcp_oauth_agent import CATALOG, ScriptedModel, ai_call


def documented_graph(model, saver, guide_name):
    document = (
        Path(__file__).resolve().parents[1]
        / "docs-site/content/docs/framework/guides"
        / guide_name
    )
    source = next(
        block
        for block in re.findall(r"```python\n(.*?)```", document.read_text(), re.S)
        if "def ksadk_graph_factory" in block
    )
    namespace = {"model": model}
    exec(compile(source, str(document), "exec"), namespace)
    return namespace["ksadk_graph_factory"](checkpointer=saver)


@pytest.mark.parametrize("guide_name", ["mcp-oauth.mdx", "mcp-oauth.en.mdx"])
async def test_followup_stale_tool_name_returns_error_and_rediscovers(monkeypatch, guide_name):
    discover = AsyncMock(return_value=CATALOG)
    invoke = AsyncMock(return_value={"isError": False})
    monkeypatch.setattr(oauth, "list_mcp_tools", discover)
    monkeypatch.setattr(oauth, "call_mcp_tool", invoke)
    model = ScriptedModel(
        responses=[
            ai_call("connect_cloud", "connect-1"),
            ai_call("cloud__SearchApis", "query-1", {"query": "first"}),
            AIMessage(content="first done"),
            # A real model may choose a tool remembered from the previous turn,
            # even though the new turn currently advertises only connect_cloud.
            ai_call("cloud__SearchApis", "stale", {"query": "followup"}),
            ai_call("connect_cloud", "connect-2"),
            ai_call("cloud__SearchApis", "query-2", {"query": "followup"}),
            AIMessage(content="followup done"),
        ]
    )
    saver = InMemorySaver()
    graph = documented_graph(model, saver, guide_name)
    config = {"configurable": {"thread_id": "followup"}}
    await graph.ainvoke({"messages": [HumanMessage(content="first")]}, config)
    result = await graph.ainvoke({"messages": [HumanMessage(content="again")]}, config)
    rejected = next(
        m for m in result["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "stale"
    )
    assert rejected.status == "error" and "connect_cloud" in rejected.content
    assert result["messages"][-1].content == "followup done"
    assert [call.kwargs["request_key"] for call in invoke.await_args_list] == ["query-1", "query-2"]
    assert discover.await_count == 2


@pytest.mark.parametrize("guide_name", ["mcp-oauth.mdx", "mcp-oauth.en.mdx"])
async def test_toolnode_executes_one_pending_call_per_checkpoint_on_cold_resume(monkeypatch, guide_name):
    discovered, executed = [], []

    async def discover(*args, **kwargs):
        discovered.append(kwargs["request_key"])
        return CATALOG

    async def invoke(*args, **kwargs):
        if kwargs["request_key"] == "query-1":
            interrupt("authorization")
        executed.append(kwargs["request_key"])
        return {"isError": False}

    monkeypatch.setattr(oauth, "list_mcp_tools", discover)
    monkeypatch.setattr(oauth, "call_mcp_tool", invoke)
    model = ScriptedModel(
        responses=[
            ai_call("connect_cloud", "connect"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "cloud__SearchApis", "id": "query-1", "args": {"query": "one"}},
                    {"name": "cloud__SearchApis", "id": "query-2", "args": {"query": "two"}},
                ],
            ),
        ]
    )
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "cold"}}
    result = await documented_graph(model, saver, guide_name).ainvoke(
        {"messages": [HumanMessage(content="query")]}, config
    )
    assert result["__interrupt__"] and discovered == ["connect"] and executed == []
    restored = documented_graph(ScriptedModel(responses=[AIMessage(content="done")]), saver, guide_name)
    result = await restored.ainvoke(Command(resume=True), config)
    assert discovered == ["connect"] and executed == ["query-1", "query-2"]
    assert result["messages"][-1].content == "done"
