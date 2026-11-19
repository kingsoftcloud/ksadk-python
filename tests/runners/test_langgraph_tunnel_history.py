"""Strict provider message pairing after a real failed MCP/checkpoint turn."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from ksadk.tunnel.middleware import TunnelMiddleware
from tests.test_langgraph_tunnel import runner_for
from tests.test_mcp_tunnel import cloud, connected, connector, eventually, local_mcp


def require_paired_messages(messages):
    """OpenAI-style tool results must immediately follow their own AI block.

    Reusing an ID in a later, fully separate tool-call block is allowed. A
    result from an older turn must never satisfy a newer call with that ID.
    """
    pending = set()
    for message in messages:
        if isinstance(message, ToolMessage):
            if message.tool_call_id not in pending:
                raise ValueError("provider: orphan or duplicate tool result")
            pending.remove(message.tool_call_id)
            continue
        if pending:
            raise ValueError("provider: unanswered tool_calls before next message")
        if isinstance(message, AIMessage):
            pending = {call["id"] for call in message.tool_calls}
    if pending:
        raise ValueError("provider: unanswered tool_calls at end of conversation")


class StrictModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "strict-provider-tool-pairing"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools])

    def response(self, messages, **kwargs):
        require_paired_messages(messages)
        user = next(message for message in reversed(messages) if isinstance(message, HumanMessage))
        if user.content == "next":
            return AIMessage(content="accepted-next-turn")
        if isinstance(messages[-1], ToolMessage):
            return AIMessage(content="completed-tool-turn")
        preferred = "wait_forever" if user.content == "fail" else "echo"
        tool = next(t["function"] for t in kwargs["tools"] if preferred in t["function"]["name"])
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "provider-reused-id",
                    "name": tool["name"],
                    "args": {} if preferred == "wait_forever" else {"text": "success"},
                }
            ],
        )

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self.response(messages, **kwargs))])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        response = self.response(messages, **kwargs)
        calls = [
            {
                "name": call["name"],
                "id": call["id"],
                "index": index,
                "args": json.dumps(call["args"]),
            }
            for index, call in enumerate(response.tool_calls)
        ]
        yield ChatGenerationChunk(
            message=AIMessageChunk(content=response.content, tool_call_chunks=calls)
        )


class State(MessagesState):
    tunnel_catalog: list[dict]


def build_graph(model, *, kind, checkpointer, tunnel_tools):
    if kind == "create_agent":
        return create_agent(
            model=model,
            checkpointer=checkpointer,
            middleware=[TunnelMiddleware(tunnel_tools)] if tunnel_tools else [],
        )

    async def chat(state, config):
        catalog = await tunnel_tools.discover() if tunnel_tools else []
        tools = await tunnel_tools.get_tools(catalog) if tunnel_tools else []
        response = await model.bind_tools(tools).ainvoke(state["messages"], config)
        return {"messages": [response], "tunnel_catalog": catalog}

    async def execute(state, config):
        return await ToolNode(await tunnel_tools.get_tools(state["tunnel_catalog"])).ainvoke(
            state, config
        )

    graph = StateGraph(State)
    graph.add_node("chat", chat)
    graph.add_node("tools", execute)
    graph.add_edge(START, "chat")
    graph.add_conditional_edges("chat", tools_condition)
    graph.add_edge("tools", "chat")
    return graph.compile(checkpointer=checkpointer)


@asynccontextmanager
async def saver_for(kind, path):
    if kind == "memory":
        yield InMemorySaver()
    elif kind == "postgres":
        dsn = os.getenv("KSADK_TEST_FRAMEWORK_DSN")
        if not dsn:
            pytest.skip("isolated PostgreSQL requires KSADK_TEST_FRAMEWORK_DSN")
        from ksadk.harness.engine.postgres_checkpointer import create_bounded_postgres_saver

        saver, pool = await create_bounded_postgres_saver(dsn)
        try:
            yield saver
        finally:
            await pool.close()
    else:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
            yield saver


@pytest.mark.parametrize("mode", ["invoke", "stream"])
@pytest.mark.parametrize("saver_kind", ["memory", "sqlite", "postgres"])
@pytest.mark.parametrize("kind", ["stategraph", "create_agent"])
async def test_next_turn_pairs_failed_call_even_when_provider_reuses_id(
    tmp_path, monkeypatch, mode, saver_kind, kind
):
    async with saver_for(saver_kind, tmp_path / "checkpoint.sqlite") as saver:
        model = StrictModel()

        def factory(*, checkpointer, tunnel_tools=None):
            return build_graph(
                model, kind=kind, checkpointer=checkpointer, tunnel_tools=tunnel_tools
            )

        module = SimpleNamespace(
            ksadk_graph_factory=factory,
            ksadk_prepare_state=lambda payload, context: {
                "messages": [HumanMessage(content=payload["input"])]
            },
        )
        async with local_mcp() as (endpoint, calls, started, _), cloud() as (_, registry, url, _):
            runner = runner_for(
                factory(checkpointer=saver), module, registry, tmp_path, monkeypatch
            )
            session_id = "strict-history-" + uuid4().hex

            async def turn(text):
                payload = {"input": text, "session_id": session_id}
                if mode == "invoke":
                    return (await runner.invoke({**payload, "_ksadk_force_graph_invoke": True}))[
                        "output"
                    ]
                chunks = [chunk async for chunk in runner.stream(payload)]
                return next(
                    chunk["output"] for chunk in reversed(chunks) if chunk["type"] == "final"
                )

            try:
                async with connected(connector(url, endpoint)) as task:
                    assert await turn("success") == "completed-tool-turn"
                    failed = asyncio.create_task(turn("fail"))
                    try:
                        await asyncio.wait_for(started.wait(), 10)
                        connection = registry.connections("deployment-17")[0][1]
                        await connection.websocket.close(code=1012)
                        assert "结果未知" in await asyncio.wait_for(failed, 10)
                    finally:
                        failed.cancel()
                        await asyncio.gather(failed, return_exceptions=True)
                    config = await runner._get_session_config(session_id)
                    saved = await runner._agent.aget_state(config)
                    with pytest.raises(ValueError, match="unanswered"):
                        require_paired_messages(saved.values["messages"])
                    await eventually(lambda: bool(registry.connections("deployment-17")))
                    assert await turn("next") == "accepted-next-turn"
                    repaired = await runner._agent.aget_state(config)
                    require_paired_messages(repaired.values["messages"])
                    replies = [
                        message
                        for message in repaired.values["messages"]
                        if isinstance(message, ToolMessage)
                    ]
                    assert len(replies) == 2
                    assert replies[0].status == "success"
                    assert replies[1].status == "error"
                    assert (
                        replies[1].additional_kwargs["ksadk_history_recovery"]["replayed"] is False
                    )
                    assert calls == ["success", "wait"]
                    assert not task.done()
                    historical = await runner._agent.aget_state(saved.config)
                    with pytest.raises(ValueError, match="unanswered"):
                        require_paired_messages(historical.values["messages"])
            finally:
                await runner.close()


async def test_postgres_recovery_matches_delayed_receipt_to_its_call(tmp_path):
    from ksadk.runtime.langgraph_checkpoint_view import fresh_user_checkpoint

    async with saver_for("postgres", tmp_path) as saver:

        def chat(state):
            if state["messages"][-1].content == "continue":
                require_paired_messages(state["messages"])
                return {"messages": [AIMessage(content="accepted next turn")]}
            return {}

        graph = StateGraph(MessagesState)
        graph.add_node("chat", chat)
        graph.add_edge(START, "chat")
        app = graph.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": uuid4().hex}}
        call = {"id": "same", "name": "lookup", "args": {}}
        history = [
            AIMessage(content="", tool_calls=[call]),
            HumanMessage(content="intervening context"),
            ToolMessage(content="real receipt", tool_call_id="same"),
            AIMessage(content="", tool_calls=[call]),
        ]
        await app.ainvoke({"messages": history}, config)
        before = await saver.aget_tuple(config)

        @fresh_user_checkpoint
        async def new_turn(_, input_data):
            return await app.ainvoke(
                {"messages": [HumanMessage(content=input_data["input"])]}, config
            )

        messages = (await new_turn(None, {"input": "continue"}))["messages"]
        assert messages[-1].content == "accepted next turn"
        replies = [message for message in messages if isinstance(message, ToolMessage)]
        assert [(m.tool_call_id, m.status) for m in replies] == [
            ("same", "success"),
            ("same", "error"),
        ]
        assert replies[0].content == "real receipt"
        assert replies[1].additional_kwargs["ksadk_history_recovery"]["replayed"] is False
        original = await saver.aget_tuple(before.config)
        assert original.checkpoint["channel_values"]["messages"] == history


async def test_postgres_parallel_pending_receipt_survives_next_turn(tmp_path):
    from ksadk.runtime.langgraph_checkpoint_view import fresh_user_checkpoint

    completed = asyncio.Event()
    executions = []

    def choose(call_id):
        def select(state):
            call = {"id": call_id, "name": "lookup", "args": {}}
            return {"messages": [AIMessage(content="", tool_calls=[call])]}

        return select

    async def success(state):
        executions.append("A")
        completed.set()
        return {"messages": [ToolMessage(content="real receipt", tool_call_id="A")]}

    async def failure(state):
        await completed.wait()
        executions.append("B")
        raise RuntimeError("fixture interruption")

    def continue_chat(state):
        require_paired_messages(state["messages"])
        return {"messages": [AIMessage(content="accepted next turn")]}

    graph = StateGraph(MessagesState)
    graph.add_node("continue_chat", continue_chat)
    for call_id, execute in (("A", success), ("B", failure)):
        graph.add_node("select_" + call_id, choose(call_id))
        graph.add_node("execute_" + call_id, execute)
        graph.add_edge("select_" + call_id, "execute_" + call_id)
    graph.add_conditional_edges(
        START,
        lambda state: (
            "continue_chat" if state["messages"][-1].content == "next" else ["select_A", "select_B"]
        ),
    )
    async with saver_for("postgres", tmp_path) as saver:
        app = graph.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": uuid4().hex}}
        with pytest.raises(RuntimeError, match="fixture interruption"):
            await app.ainvoke({"messages": [HumanMessage(content="both")]}, config)
        saved = await saver.aget_tuple(config)
        assert any(
            isinstance(message, ToolMessage) and message.content == "real receipt"
            for _, channel, values in saved.pending_writes
            if channel == "messages"
            for message in values
        )

        @fresh_user_checkpoint
        async def next_turn(_, payload):
            return await app.ainvoke({"messages": [HumanMessage(content=payload["input"])]}, config)

        messages = (await next_turn(None, {"input": "next"}))["messages"]
        assert messages[-1].content == "accepted next turn"
        first, second = [m for m in messages if isinstance(m, ToolMessage)]
        assert (first.tool_call_id, first.status, first.content) == ("A", "success", "real receipt")
        assert second.tool_call_id == "B"
        assert second.additional_kwargs["ksadk_history_recovery"]["outcome"] == "unknown"
        assert executions == ["A", "B"]
