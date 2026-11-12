import pytest

from ksadk.a2a.langgraph_managed import bind_managed_langgraph, handoff_tool, remote_node
from tests.a2a.test_invocation_service_r85 import request, service, trigger


@pytest.mark.asyncio
async def test_node_preserves_canonical_facts_and_replays_without_second_send():
    req = request()
    svc, store, wire = await service()
    published = []
    node = remote_node(req.spec)
    assert node.remote_spec == req.spec
    with bind_managed_langgraph(svc, lambda *_: (req, trigger()), published.append):
        result = await node({"messages": []}, {})
        replay = await node({"messages": []}, {})
    assert result["messages"][0].content == replay["messages"][0].content == "hello"
    assert sum(kind == "send" for kind, _ in wire.calls) == 1
    facts = await store.list("s", run_id=req.handle.run_id)
    assert {e.event_id for e in published} == {e.event_id for e in facts}


@pytest.mark.asyncio
async def test_missing_runtime_context_does_not_call_network():
    with pytest.raises(RuntimeError, match="A2A_MANAGED_INVOCATION_CONTEXT_REQUIRED"):
        await remote_node(request().spec)({"messages": []}, {})


@pytest.mark.asyncio
async def test_fixed_binding_cannot_be_replaced_by_provider():
    req = request()
    svc, _, wire = await service()
    changed = req.spec.model_copy(update={"local_name": "other"})
    with bind_managed_langgraph(svc, lambda *_: (req, trigger()), lambda _: None):
        with pytest.raises(ValueError, match="A2A_BINDING_MISMATCH"):
            await remote_node(changed)({}, {})
    assert wire.calls == []


def test_factories_require_fixed_version():
    unpinned = request().spec.model_copy(update={"expected_version_id": None})
    for factory in (remote_node, handoff_tool):
        with pytest.raises(ValueError, match="A2A_VERSION_PIN_REQUIRED"):
            factory(unpinned)


@pytest.mark.asyncio
async def test_handoff_exposes_message_only_and_uses_same_service():
    req = request()
    svc, _, wire = await service()
    tool = handoff_tool(req.spec)
    assert set(tool.args) == {"message"}
    with bind_managed_langgraph(svc, lambda *_: (req, trigger()), lambda _: None):
        assert await tool.ainvoke({"message": "hello"}) == "hello"
    assert sum(kind == "send" for kind, _ in wire.calls) == 1


@pytest.mark.asyncio
async def test_denied_policy_does_not_prepare_or_send():
    from ksadk.a2a.invocation_policy import A2AInvocationPolicy, A2AInvocationPolicyDecision

    req = request()
    svc, _, wire = await service(policy=A2AInvocationPolicy(
        evaluator=lambda *_: A2AInvocationPolicyDecision("deny")
    ))
    with bind_managed_langgraph(svc, lambda *_: (req, trigger()), lambda _: None):
        with pytest.raises(PermissionError, match="A2A_APPROVAL_REQUIRED"):
            await remote_node(req.spec)({}, {})
    assert wire.calls == []


@pytest.mark.asyncio
async def test_node_runs_in_real_state_graph_with_native_node_metadata():
    from langgraph.graph import END, START, MessagesState, StateGraph

    req = request()
    svc, _, wire = await service()
    metadata = []

    def provider(spec, state, config, surface):
        metadata.append((config["metadata"]["langgraph_node"], surface))
        return req, trigger()

    graph = StateGraph(MessagesState)
    graph.add_node("remote", remote_node(req.spec))
    graph.add_edge(START, "remote")
    graph.add_edge("remote", END)
    with bind_managed_langgraph(svc, provider, lambda _: None):
        result = await graph.compile().ainvoke({"messages": []})
    assert result["messages"][-1].content == "hello"
    assert metadata == [("remote", "node")]
    assert sum(kind == "send" for kind, _ in wire.calls) == 1


@pytest.mark.asyncio
async def test_cancel_requests_service_cancel_without_faking_remote_cancel(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    from tests.a2a.test_invocation_service_r85 import Wire

    entered = asyncio.Event()

    class SlowWire(Wire):
        @asynccontextmanager
        async def open_prepared_message_stream(self, prepared, message):
            async def stream():
                entered.set()
                await asyncio.Event().wait()
                yield self.task

            yield stream()

    req = request()
    svc, _, _ = await service(wire=SlowWire())
    cancellations = []
    original_cancel = svc.cancel

    async def cancel(handle):
        result = await original_cancel(handle)
        cancellations.append(result)
        return result

    monkeypatch.setattr(svc, "cancel", cancel)
    with bind_managed_langgraph(svc, lambda *_: (req, trigger()), lambda _: None):
        task = asyncio.create_task(remote_node(req.spec)({}, {}))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    assert len(cancellations) == 1
    assert cancellations[0].outcome != "remote_cancelled"
