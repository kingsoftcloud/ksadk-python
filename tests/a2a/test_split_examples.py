"""Executable source examples must exercise the real graph and preserve routing."""
import importlib.util
from pathlib import Path
import pytest
from langchain_core.messages import HumanMessage

ROOT = Path(__file__).resolve().parent / "fixtures"

def load(role):
    path = ROOT / f"a2a-langgraph-{role}" / f"a2a_langgraph_{role}" / "agent.py"
    spec = importlib.util.spec_from_file_location(f"example_{role}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [False, True])
async def test_response_loss_probe_sends_once_and_preserves_normal_calls(monkeypatch, selected):
    from contextlib import asynccontextmanager
    import json
    from types import SimpleNamespace
    from ksadk.a2a.space_client import A2ASpaceClient

    path = ROOT / "a2a-langgraph-b/a2a_langgraph_b/fault_probe.py"
    spec = importlib.util.spec_from_file_location("acceptance_fault_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    first = SimpleNamespace(WhichOneof=lambda _: "task", task=SimpleNamespace(id="remote-1"))

    @asynccontextmanager
    async def wire(self, prepared, message):
        calls.append("send")
        async def events():
            calls.append("received")
            yield first
        try:
            yield events()
        finally:
            calls.append("closed")

    monkeypatch.setattr(A2ASpaceClient, "open_prepared_message_stream", wire)
    module.install_response_loss_probe()
    installed = A2ASpaceClient.open_prepared_message_stream
    module.install_response_loss_probe()
    assert installed is A2ASpaceClient.open_prepared_message_stream
    message = SimpleNamespace(message_id="message-1", parts=[SimpleNamespace(
        text=json.dumps({"acceptance_drop_response": selected}))])
    async with installed(None, SimpleNamespace(platform_task_id="a2a-task-test"), message) as stream:
        if selected:
            with pytest.raises(ConnectionError, match="dropped"):
                await anext(stream)
        else:
            assert await anext(stream) is first
    assert calls == ["send", "received", "closed"]

@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["a", "b", "c"])
async def test_leaf_runs_real_graph_without_network(role):
    module = load(role)
    result = await module.ksadk_graph_factory(checkpointer=None).ainvoke(
        {"messages": [HumanMessage(content='{"path": [], "text": "proof"}')]})
    assert result["messages"][-1].content == f"{role.upper()}:proof"

def test_dynamic_route_keeps_remaining_hops_in_remote_message():
    import json
    module = load("a")
    result = module.prepare_call({"messages": [HumanMessage(content=
        '{"path": ["a2a_langgraph_b", "a2a_langgraph_c"], "text": "proof"}')]})
    call = result["messages"][0].tool_calls[0]
    assert call["name"] == "call_a2a_agent"
    assert call["args"]["agent"] == "a2a_langgraph_b"
    assert json.loads(call["args"]["message"])["path"] == ["a2a_langgraph_c"]

@pytest.mark.asyncio
async def test_node_consumes_one_hop_and_forwards_only_current_turn(monkeypatch):
    import json
    from langchain_core.messages import AIMessage
    from ksadk.a2a.langgraph_managed import bind_managed_langgraph
    from tests.a2a.test_invocation_service_r85 import request, service, trigger
    module = load("a")
    req = request()
    monkeypatch.setenv("A2A_EXAMPLE_MODE", "node")
    monkeypatch.setattr(module, "fixed_spec", lambda: req.spec)
    captured = []
    svc, _, _ = await service()
    def provider(spec, state, config, surface):
        captured.append(state["messages"])
        return req.model_copy(update={"spec": spec}), trigger()
    graph = module.ksadk_graph_factory(checkpointer=None)
    with bind_managed_langgraph(svc, provider, lambda _: None):
        for text in ["first", "second"]:
            await graph.ainvoke({"messages": [HumanMessage(content="old input"),
                AIMessage(content="old reply"), HumanMessage(content=json.dumps(
                    {"path": ["b", "c"], "text": text}))]})
    assert len(captured) == 2
    for messages, text in zip(captured, ["first", "second"]):
        assert len(messages) == 1
        assert json.loads(messages[0].content) == {"path": ["c"], "text": text}

@pytest.mark.asyncio
async def test_leaf_native_input_resumes_without_repeating_route():
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command
    module = load("c")
    graph = module.ksadk_graph_factory(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "input-proof"}}
    paused = await graph.ainvoke({"messages": [HumanMessage(content=
        '{"path": [], "text": "proof", "request_input": true}')]}, config)
    assert paused["__interrupt__"]
    completed = await graph.ainvoke(Command(resume={"answer": "confirmed"}), config)
    assert completed["messages"][-1].content == 'C:proof:confirmed'

@pytest.mark.asyncio
async def test_leaf_uses_real_stdio_mcp_round_trip():
    module = load("c")
    result = await module.ksadk_graph_factory(checkpointer=None).ainvoke(
        {"messages": [HumanMessage(content='{"path": [], "text": "proof", "mcp": true}')]})
    assert result["messages"][-1].content == 'C:mcp:proof'


@pytest.mark.parametrize("role", ["a", "b", "c"])
async def test_model_route_retains_real_model_tool_identity(monkeypatch, role):
    import json
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.memory import MemorySaver
    calls = []
    class Model:
        def __init__(self, **kwargs):
            pass
        def bind_tools(self, tools, tool_choice):
            calls.append(tool_choice)
            return self
        async def ainvoke(self, messages):
            request = json.loads(messages[-1].content)
            return AIMessage(content="", tool_calls=[{
                "id": "native-model-call", "name": request["tool"], "args": request["arguments"],
            }])
    monkeypatch.setattr("langchain_openai.ChatOpenAI", Model)
    monkeypatch.setenv("OPENAI_MODEL_NAME", "test-model")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("A2A_EXAMPLE_MODE", "dynamic")
    graph = load(role).ksadk_graph_factory(checkpointer=MemorySaver())
    result = await graph.ainvoke({"messages": [HumanMessage(content=json.dumps({
        "path": ["expert", "leaf"], "text": "proof", "model_route": True,
    }))]}, {"configurable": {"thread_id": "model-route"}}, interrupt_before=["tools"])
    call = result["messages"][-1].tool_calls[0]
    assert calls == ["call_a2a_agent"]
    assert call["id"] == "native-model-call"
    assert json.loads(call["args"]["message"])["path"] == ["leaf"]


@pytest.mark.asyncio
async def test_leaf_accepts_native_adk_context_without_using_assistant_restatement():
    module = load("c")
    context = ('[user]\n{"path": [], "text": "old"}\n[local]\nold reply\n'
        '[user]\n{"path": [], "text": "current"}\n[r86_local]\n'
        '{"path": [], "text": "must-not-use"}')
    result = await module.ksadk_graph_factory(checkpointer=None).ainvoke(
        {"messages": [HumanMessage(content=context)]})
    assert result["messages"][-1].content == "C:current"
