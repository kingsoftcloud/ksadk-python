import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph

from ksadk.a2a.invocation_policy import A2AInvocationPolicy, A2AInvocationPolicyDecision
from ksadk.a2a.invocation_service import A2AInvocationService
from ksadk.a2a.langgraph_invocation_context import LangGraphInvocationContextProvider
from ksadk.a2a.langgraph_managed import bind_managed_langgraph, remote_node
from ksadk.events.run_authority import RunEventAuthority, bind_run_event_authority
from ksadk.events.session_event import SessionServiceEventStore
from ksadk.kernel.contracts import ActivationWriteGuard
from ksadk.sessions.in_memory import InMemorySessionService
from tests.a2a.test_invocation_service_r85 import Wire, request


async def setup_runtime():
    sessions = InMemorySessionService()
    await sessions.create_session("caller", "user", "s")
    authority = RunEventAuthority(
        SessionServiceEventStore(sessions), session_id="s", run_id="public-run",
        guard=ActivationWriteGuard(activation_id="test", fencing_token=1),
    )
    wire = Wire()
    service = A2AInvocationService(
        space_client=wire, event_store=authority,
        policy=A2AInvocationPolicy(evaluator=lambda *_: A2AInvocationPolicyDecision("allow")),
    )
    provider = LangGraphInvocationContextProvider(
        authority=authority, service_for_space=lambda _: service, caller_agent_id="caller",
    )
    return authority, service, wire, provider


@pytest.mark.asyncio
async def test_real_graph_native_context_reaches_fenced_service():
    authority, service, wire, provider = await setup_runtime()
    graph = StateGraph(MessagesState)
    graph.add_node("remote", remote_node(request().spec))
    graph.add_edge(START, "remote")
    graph.add_edge("remote", END)
    events = []
    with bind_run_event_authority(authority):
        with bind_managed_langgraph(None, provider, events.append):
            result = await graph.compile().ainvoke({"messages": [HumanMessage("real question")]})
        facts = await authority.list("s")
    assert result["messages"][-1].content == "hello"
    assert len([kind for kind, _ in wire.calls if kind == "send"]) == 1
    native = [e for e in facts if e.source.framework == "langgraph"]
    assert len(native) == 2
    assert native[0].source.metadata["node_id"] == "remote"
    assert native[0].source.metadata["binding_id"] == request().spec.binding_id
    assert set(e.event_id for e in events) == set(e.event_id for e in facts)


def native_config(namespace="remote:task-1"):
    return {"configurable": {"__pregel_task_id": "task-1"},
            "metadata": {"langgraph_node": "remote", "langgraph_checkpoint_ns": namespace}}


@pytest.mark.asyncio
async def test_missing_or_conflicting_native_path_is_rejected():
    authority, _, wire, provider = await setup_runtime()
    with bind_run_event_authority(authority):
        for config in ({}, native_config("other:task-1")):
            with pytest.raises(ValueError, match="A2A_NATIVE_EXECUTION"):
                await provider(request().spec, {}, config, "node")
        with pytest.raises(ValueError, match="A2A_NATIVE_TOOL_TRIGGER_REQUIRED"):
            await provider(request().spec, {}, native_config(), "handoff")
    assert wire.calls == []


@pytest.mark.asyncio
async def test_namespace_isolation_and_only_visible_message_text_forwarded():
    authority, _, _, provider = await setup_runtime()
    payload = {"messages": [HumanMessage("hello"),
                            ToolMessage("private tool body", tool_call_id="call_1")],
               "secret": "private state"}
    with bind_run_event_authority(authority):
        first = await provider(request().spec, payload, native_config(), "node")
        replay = await provider(request().spec, payload, native_config(), "node")
        nested = await provider(request().spec, payload, native_config("parent:x|remote:task-1"), "node")
    assert first.request.message.text_parts == ("hello",)
    assert first.request.handle == replay.request.handle
    assert first.trigger != nested.trigger
    assert first.request.handle.run_id == "public-run"
    assert first.request.handle.session_id == "s"
    assert first.request.context.caller_agent_id == "caller"


@pytest.mark.asyncio
async def test_runner_stream_installs_provider_and_merges_native_and_remote_facts(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from ksadk.runners.langgraph_runner import LangGraphRunner

    authority, service, wire, _ = await setup_runtime()
    policy = A2AInvocationPolicy(evaluator=lambda *_: A2AInvocationPolicyDecision("allow"))
    monkeypatch.setattr(authority, "managed_invocation_policy", lambda: policy)
    services = []

    def service_for_space(space, *, event_store, policy):
        assert event_store is authority
        services.append(space)
        return service

    runner = LangGraphRunner(SimpleNamespace(entry_point="agent.py", agent_variable="root_agent"), ".")
    graph = StateGraph(MessagesState)
    graph.add_node("remote", remote_node(request().spec))
    graph.add_edge(START, "remote")
    graph.add_edge("remote", END)
    runner._agent = graph.compile()
    runner._managed_a2a_bootstrap = SimpleNamespace(
        outbound_enabled=True, runtime_metadata=SimpleNamespace(agent_id="caller"),
        invocation_service_for_space=service_for_space,
    )

    async def collect():
        with bind_run_event_authority(authority):
            return [event async for event in runner.stream_canonical_events(
                {"run_id": "public-run", "session_id": "s", "input": "hello"}
            )]

    events = await asyncio.wait_for(collect(), timeout=10)
    assert events[0].event_type == "run.started"
    assert events[-1].event_type == "run.completed"
    assert any(event.source.framework == "a2a" for event in events)
    assert all(event.run_id == "public-run" for event in events)
    assert services == [request().spec.space_id]
    assert sum(kind == "send" for kind, _ in wire.calls) == 1
