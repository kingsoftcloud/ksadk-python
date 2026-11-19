"""Tunnel failures become terminal answers, without executing the graph again."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import HumanMessage
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from ksadk.tunnel import LocalMCPService, TunnelConnector, TunnelError
from ksadk.tunnel.failure import tunnel_failure
from tests.test_langgraph_tunnel import Model
from tests.test_mcp_tunnel import connected, local_mcp, serve


def failure_graph(*, checkpointer, tunnel_tools=None):
    class State(MessagesState):
        tunnel_catalog: list[dict]

    async def chat(state, config):
        catalog = await tunnel_tools.discover() if tunnel_tools else []
        tools = await tunnel_tools.get_tools(catalog) if tunnel_tools else []
        user = next(m for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
        model = Model(preferred="wait_forever" if user.content == "wait" else "echo")
        reply = await model.bind_tools(tools).ainvoke(state["messages"], config)
        return {"messages": [reply], "tunnel_catalog": catalog}

    async def execute(state, config):
        tools = await tunnel_tools.get_tools(state["tunnel_catalog"])
        return await ToolNode(tools).ainvoke(state, config)

    graph = StateGraph(State)
    graph.add_node("chat", chat)
    graph.add_node("tools", execute)
    graph.add_edge(START, "chat")
    graph.add_conditional_edges("chat", tools_condition)
    graph.add_edge("tools", "chat")
    return graph.compile(checkpointer=checkpointer)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("endpoint", ["/v1/chat/completions", "/v1/responses"])
async def test_http_disconnect_is_an_answer_and_next_turn_does_not_replay(
    tmp_path, monkeypatch, stream, endpoint
):
    from ksadk.cli.runtime_bootstrap import create_runtime_web_app
    from ksadk.detection import FrameworkDetector

    monkeypatch.setenv("KSADK_SESSION_BACKEND", "memory")
    monkeypatch.setenv("KSADK_TUNNEL_TOKEN", "fixture-tunnel-token")
    monkeypatch.delenv("AGENT_RUNTIME_ID", raising=False)
    monkeypatch.delenv("KSADK_TUNNEL_PEER_SECRET", raising=False)
    monkeypatch.setenv("AGENT_KERNEL_ENABLED", "0")
    (tmp_path / "agent.py").write_text(
        "from ksadk import Agent\n"
        "from tests.test_tunnel_failure_responses import failure_graph\n"
        "ksadk_graph_factory = failure_graph\n"
        "root_agent = Agent(failure_graph(checkpointer=None), enable_tunnel=True)\n"
    )
    (tmp_path / "agentengine.yaml").write_text(
        "framework: langgraph\nentry_point: agent.py\nagent_variable: root_agent\n"
    )
    app = create_runtime_web_app(FrameworkDetector(str(tmp_path)).detect(), tmp_path)
    async with local_mcp() as (endpoint_url, calls, started, _), serve(app) as url:
        connector = TunnelConnector(
            url,
            token="fixture-tunnel-token",
            services=[LocalMCPService("erp", endpoint_url, tool_filter=("echo", "wait_forever"))],
            reconnect_min=0.01,
            reconnect_max=0.05,
        )
        async with connected(connector), httpx.AsyncClient(timeout=30, trust_env=False) as http:

            def payload(message):
                common = {"session_id": "failure-session", "stream": stream}
                if endpoint.endswith("responses"):
                    return {**common, "input": message}
                return {**common, "messages": [{"role": "user", "content": message}]}

            request = asyncio.create_task(http.post(url + endpoint, json=payload("wait")))
            await asyncio.wait_for(started.wait(), 15)
            await connector._ws.close(code=1012)
            result = await request
            assert result.status_code == 200, result.text
            assert "结果未知" in result.text, result.text
            assert "未自动重试" in result.text
            assert "response.failed" not in result.text
            assert calls == ["wait"]
            # A fresh turn in the same checkpoint thread must not resume the failed tool.
            for _ in range(100):
                if connector.connected.is_set():
                    break
                await asyncio.sleep(0.01)
            recovered = await http.post(url + endpoint, json=payload("fresh"))
            assert recovered.status_code == 200, recovered.text
            assert calls == ["wait", "fresh"]


def test_group_handling_preserves_other_failures_and_cancellation():
    assert tunnel_failure(TunnelError("disconnected")).indeterminate is False
    group = ExceptionGroup("private detail", [TunnelError("timeout", indeterminate=True)])
    failure = tunnel_failure(group)
    assert failure.indeterminate
    assert "private detail" not in failure.message
    assert (
        tunnel_failure(ExceptionGroup("mixed", [TunnelError("disconnected"), ValueError("bad")]))
        is None
    )
    assert tunnel_failure(ValueError("bad")) is None
    assert tunnel_failure(asyncio.CancelledError()) is None


@pytest.mark.parametrize("unknown", [False, True])
async def test_adk_canonical_failure_answer_keeps_tool_failed(unknown):
    from google.adk.events import Event
    from google.genai import types

    from ksadk.events.reducer import StreamReducer
    from ksadk.runners._adk_canonical_stream import canonical_events

    async def native():
        yield Event(
            author="agent",
            invocation_id="native-run",
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(id="call-1", name="inventory", args={})
                    )
                ],
            ),
        )
        raise TunnelError("disconnected", indeterminate=unknown)

    runner = SimpleNamespace(
        _agent=SimpleNamespace(name="agent"),
        _extract_event_usage=lambda _: {},
        _extract_approval_signals=lambda _: [],
    )
    span = SimpleNamespace(set_attribute=lambda *_: None)
    events = [
        event
        async for event in canonical_events(
            runner, native(), run_id="public-run", session_id="session", span=span
        )
    ]
    reducer = StreamReducer()
    for event in events:
        reducer.apply(event)
    assert events[-1].event_type == "run.completed"
    text = "".join(
        part.text
        for item in reducer.snapshot().items
        for part in item.parts
        if hasattr(part, "text")
    )
    assert ("结果未知" if unknown else "工具调用失败") in text
    assert "未自动重试" in text
    assert any(
        getattr(part, "is_error", False) for item in reducer.snapshot().items for part in item.parts
    )


@pytest.mark.parametrize("unknown", [False, True])
async def test_direct_graph_invoke_returns_failure_without_reinvoking(tmp_path, unknown):
    from ksadk.runners.langgraph_runner import LangGraphRunner

    calls = []

    async def ainvoke(payload, **kwargs):
        calls.append(payload)
        raise TunnelError("disconnected", indeterminate=unknown)

    async def prepare():
        pass

    async def config(_):
        return {"configurable": {"thread_id": "thread"}}

    runner = LangGraphRunner(SimpleNamespace(), str(tmp_path))
    runner._agent = SimpleNamespace(ainvoke=ainvoke)
    runner.prepare_runtime_capabilities = prepare
    runner._get_session_config = config
    output = await runner.invoke({"input": "inventory", "_ksadk_force_graph_invoke": True})
    assert ("结果未知" if unknown else "工具调用失败") in output["output"]
    assert output["metadata"]["tunnel_failure"]["automatic_retry"] is False
    assert len(calls) == 1


async def test_legacy_graph_stream_stops_after_disconnect(tmp_path, monkeypatch):
    from tests.test_langgraph_tunnel import runner_for
    from tests.test_mcp_tunnel import cloud
    from tests.test_mcp_tunnel import connector as make_connector

    async with local_mcp() as (url, calls, started, _), cloud() as (_, reg, gateway, _):
        graph = failure_graph(checkpointer=None)
        module = SimpleNamespace(ksadk_graph_factory=failure_graph)
        runner = runner_for(graph, module, reg, tmp_path, monkeypatch)
        try:
            client = make_connector(gateway, url)
            async with connected(client):

                async def consume():
                    return [
                        c async for c in runner.stream({"input": "wait", "session_id": "legacy"})
                    ]

                request = asyncio.create_task(consume())
                await asyncio.wait_for(started.wait(), 10)
                await client._ws.close(code=1012)
                chunks = await request
                assert chunks[-1]["type"] == "final"
                assert "结果未知" in chunks[-1]["output"]
                assert calls == ["wait"]
        finally:
            await runner.close()


@pytest.mark.parametrize("unknown", [False, True])
async def test_adk_direct_invoke_ends_on_failure(tmp_path, unknown):
    from ksadk.runners.adk_runner import ADKRunner

    executions = []

    async def ensure_session(_):
        return "session"

    async def prepare(**kwargs):
        async def native():
            executions.append("started")
            yield SimpleNamespace(
                content=SimpleNamespace(parts=[SimpleNamespace(text="查询中", thought=False)])
            )
            raise TunnelError("disconnected", indeterminate=unknown)

        return native()

    runner = ADKRunner(SimpleNamespace(), str(tmp_path))
    runner._prepare_trace_metadata = lambda _: (None, [], None, "agent")
    runner._ensure_session = ensure_session
    runner._native_user_for_session = lambda _: "user"
    runner._prepare_run_events = prepare
    runner._extract_event_usage = lambda _: {}
    result = await runner.invoke({"input": "inventory", "session_id": "session"})
    assert ("结果未知" if unknown else "工具调用失败") in result["output"]
    assert "查询中" not in result["output"]
    assert executions == ["started"]
