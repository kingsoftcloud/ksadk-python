import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, create_react_agent
from langgraph.types import interrupt

from ksadk.a2a.invocation_service import A2AInvocationService
from ksadk.a2a.inbound_kernel import InboundKernelSubmission, stream_inbound_kernel_facts
from ksadk.kernel.state import RunState
from ksadk.kernel.worker import AgentKernelWorker
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter
from ksadk.toolsets.a2a import get_a2a_tools
from tests.a2a.test_invocation_service_r85 import Wire, request
from tests.kernel.control_harness import AGENT, TENANT, command, kernel_stack
from tests.runners.test_langchain_hitl_langgraph import SeqToolModel


def native_input_graph(*, checkpointer):
    """Keep the removed C example's native-input leaf behavior local to tests."""
    # Preserve the prepare -> tools -> finish topology and structured-input
    # payload from the C example before its removal in 8d961de8.
    async def prepare(state):
        message = next(m for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
        payload = json.loads(message.content)
        assert payload["path"] == []
        text = payload.get("text", "")
        if payload.get("request_input"):
            answer = interrupt(
                {
                    "kind": "structured_input",
                    "prompt": "Provide the acceptance answer",
                    "schema": {
                        "type": "object",
                        "properties": {"answer": {"type": "string"}},
                        "required": ["answer"],
                    },
                }
            )
            text = f"{text}:{answer['answer']}"
        return {"messages": [AIMessage(content=f"C:{text}")]}

    async def finish(state):
        return {"messages": []}

    graph = StateGraph(MessagesState)
    graph.add_node("prepare", prepare)
    graph.add_node("tools", ToolNode([]))
    graph.add_node("finish", finish)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "tools")
    graph.add_edge("tools", "finish")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=checkpointer)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve", "reject", "cancel", "timeout"])
async def test_managed_langgraph_kernel_interaction_keeps_original_stream(monkeypatch, action):
    if action == "timeout":
        from dataclasses import replace
        import time
        from ksadk.a2a.langgraph_invocation_context import LangGraphInvocationContextProvider
        original_request = LangGraphInvocationContextProvider._request

        async def short_deadline(self, *args):
            invocation = await original_request(self, *args)
            return replace(invocation, request=invocation.request.model_copy(update={
                "deadline_unix_seconds": time.time() + 0.5,
            }))

        monkeypatch.setattr(LangGraphInvocationContextProvider, "_request", short_deadline)
    monkeypatch.setenv("KSADK_TOOL_APPROVAL_MODE", "full")
    spec = request().spec
    monkeypatch.setenv("KSADK_A2A_SPACE_ID", spec.space_id)
    model = SeqToolModel(msgs=[AIMessage(content="", tool_calls=[{
        "name": "call_a2a_agent", "id": "call_1",
        "args": {"agent": "expert", "message": "hello"},
    }]), AIMessage(content="finished")])
    runner = LangGraphRunner(SimpleNamespace(entry_point="agent.py", agent_variable="root_agent"), ".")
    runner._agent = create_react_agent(model, get_a2a_tools())
    wire = Wire()
    async def discover():
        return [SimpleNamespace(agent_id=spec.target_agent_id, version_id=spec.expected_version_id,
                                agent_card=SimpleNamespace(name="expert", description="test"))]
    adapter = LangGraphRuntimeAdapter(runner)
    adapter.configure_managed_a2a(SimpleNamespace(
        outbound_enabled=True, runtime_metadata=SimpleNamespace(agent_id="caller"),
        client_for_space=lambda _: SimpleNamespace(discover=discover),
        invocation_service_for_space=lambda _, **deps: A2AInvocationService(space_client=wire, **deps),
    ))
    stack = await kernel_stack(adapter=adapter)
    lease = await stack.lease()
    worker = AgentKernelWorker(stack.store, adapter_factory=lambda: adapter, session_events=stack.events)
    initial_receipt = await stack.kernel.submit(command(idempotency_key="langgraph-run", payload={
        "content": "call expert", "session_context": {},
        "runtime_options": {"tool_approval_mode": "ask"},
    }), permit=stack.permit("enqueue"))
    started = await worker.run_once(AGENT, lease)

    async def pending():
        while True:
            records = await stack.store.list_pending_interactions(TENANT, "s1")
            if records:
                return records[0]
            run = await stack.store.load_run(started.run_id)
            assert run.state not in {RunState.COMPLETED, RunState.FAILED}, run
            await asyncio.sleep(0.01)

    record = await asyncio.wait_for(pending(), 5)
    assert record.provider_id == "managed-a2a"
    assert not wire.calls
    async def projected_facts():
        return [fact async for fact in stream_inbound_kernel_facts(
            InboundKernelSubmission("s1", AGENT, initial_receipt), stack.events,
        )]
    facts = await asyncio.wait_for(projected_facts(), 5)
    assert facts[-1].event_type == "control.run_transition"
    assert facts[-1].payload["state"] == "waiting"
    requests = [fact for fact in facts if fact.family == "interaction" and fact.event_type == "interaction.requested"]
    assert len(requests) == 1
    assert requests[0].payload["interaction_id"] == record.interaction_id
    assert all(fact.run_id == started.run_id for fact in facts)
    # An authenticated A2A continuation still cannot target another task's
    # active run, even when it knows that run's interaction identifier.
    wrong_origin = command("submit_interaction", idempotency_key="wrong-origin", payload={
        "run_id": started.run_id, "interaction_id": record.interaction_id,
        "token_ref": "fake-ref", "action": "approve", "response": {},
        "expected_revision": record.revision, "a2a_origin_command_id": "another-command",
    })
    await stack.kernel.submit(wrong_origin, permit=stack.permit("submit_interaction"))
    await worker.run_once(AGENT, lease)
    assert not wire.calls
    assert (await stack.store.list_pending_interactions(TENANT, "s1"))[0].interaction_id == record.interaction_id
    if action == "cancel":
        receipt = await stack.kernel.submit(command("interrupt", idempotency_key="cancel"),
                                           permit=stack.permit("interrupt"))
    elif action != "timeout":
        receipt = await stack.kernel.submit(command("submit_interaction", idempotency_key="reply", payload={
            "run_id": started.run_id, "interaction_id": record.interaction_id,
            "token_ref": "fake-ref", "action": action, "response": {},
            "expected_revision": record.revision,
            "a2a_origin_command_id": str(initial_receipt.command_id),
        }), permit=stack.permit("submit_interaction"))
    if action != "timeout":
        assert receipt.status == "accepted"
        await asyncio.wait_for(worker.run_once(AGENT, lease), 5)

    async def finished():
        while True:
            run = await stack.store.load_run(started.run_id)
            if run.state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}:
                return run
            await asyncio.sleep(0.01)

    run = await asyncio.wait_for(finished(), 5)
    assert run.state == (RunState.CANCELLED if action == "cancel" else RunState.COMPLETED)
    assert sum(kind == "send" for kind, _ in wire.calls) == (1 if action == "approve" else 0)
    assert not await stack.store.list_pending_interactions(TENANT, "s1")
    if action == "timeout":
        saved = await stack.store.get(record.interaction_id)
        assert saved.status == "expired"

@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["submit", "cancel", "cancel_retry"])
async def test_native_langgraph_input_resumes_through_kernel_ledger(action, monkeypatch):
    from langgraph.checkpoint.memory import MemorySaver
    runner = LangGraphRunner(SimpleNamespace(entry_point="agent.py", agent_variable="root_agent"), ".")
    runner._agent = native_input_graph(checkpointer=MemorySaver())
    runner._managed_a2a_bootstrap = SimpleNamespace(outbound_enabled=False)
    adapter = LangGraphRuntimeAdapter(runner)
    stack = await kernel_stack(adapter=adapter)
    lease = await stack.lease()
    worker = AgentKernelWorker(stack.store, adapter_factory=lambda: adapter, session_events=stack.events)
    await stack.kernel.submit(command(payload={"content":
        '{"path": [], "text": "proof", "request_input": true}'}), permit=stack.permit("enqueue"))
    started = await worker.run_once(AGENT, lease)
    async with asyncio.timeout(5):
        while not (records := await stack.store.list_pending_interactions(TENANT, "s1")):
            await asyncio.sleep(0.01)
    record = records[0]
    assert record.provider_id == "langgraph"
    persisted_run = await stack.store.load_run(started.run_id)
    from ksadk.runtime.adapter import RunHandle
    from ksadk.runtime.executor import handle_digest

    persisted_handle = RunHandle.model_validate(persisted_run.metadata["handle"])
    assert persisted_handle.native_ref["checkpoint_id"] == record.native_target["checkpoint_id"]
    assert persisted_handle.native_ref["thread_id"] == record.native_target["thread_id"]
    assert persisted_handle.native_ref["checkpoint_ns"] == record.native_target["checkpoint_ns"]
    assert persisted_run.metadata["handle_digest"] == handle_digest(persisted_handle)
    assert record.interaction_id in persisted_handle.native_ref["pending_approval_ids"]
    assert (persisted_handle.native_ref["native_waits"][record.interaction_id]["call_id"]
            == record.native_target["call_id"])
    assert (await stack.store.load_run(started.run_id)).state is RunState.WAITING
    if action.startswith("cancel"):
        await asyncio.wait_for(worker._executions[started.run_id].stream_task, 5)
        if action == "cancel_retry":
            execution = worker._executions[started.run_id]
            active = await stack.store.load_run(started.run_id)
            original_cancel = stack.store.cancel

            async def fail_cleanup(*args, **kwargs):
                raise RuntimeError("injected stop after terminal append")

            monkeypatch.setattr(stack.store, "cancel", fail_cleanup)
            with pytest.raises(RuntimeError, match="injected stop"):
                await worker._cancel_suspended_langgraph(execution, active, lease)
            monkeypatch.setattr(stack.store, "cancel", original_cancel)
        receipt = await stack.kernel.submit(command("interrupt", payload={"run_id": started.run_id}),
                                           permit=stack.permit("interrupt"))
        assert receipt.status == "accepted"
        await worker.run_once(AGENT, lease)
        assert (await stack.store.load_run(started.run_id)).state is RunState.CANCELLED
        assert not await stack.store.list_pending_interactions(TENANT, "s1")
        events = await stack.events.read("s1", 0, 100)
        assert sum(e.event_type == "run.canceled" for e in events) == 1
        root = next(e for e in events if e.event_type == "run.started")
        cancelled = next(e for e in events if e.event_type == "run.canceled")
        assert cancelled.payload["scope_id"] == root.payload["scope_id"]
        assert not adapter._pending_cancels
        return
    receipt = await stack.kernel.submit(command("submit_interaction", payload={
        "run_id": started.run_id, "interaction_id": record.interaction_id,
        "token_ref": "test-token", "action": "submit", "response": {"answer": "confirmed"},
        "expected_revision": record.revision,
    }), permit=stack.permit("submit_interaction"))
    assert receipt.status == "accepted", (receipt.error, adapter.capabilities(), record.native_target)
    await worker.run_once(AGENT, lease)
    async with asyncio.timeout(5):
        while (run := await stack.store.load_run(started.run_id)).state not in {
            RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED,
        }:
            await asyncio.sleep(0.01)
    assert run.state is RunState.COMPLETED
    events = await stack.events.read("s1", 0, 100)
    assert 'C:proof:confirmed' in str([e.payload for e in events])
