import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage
from langgraph.prebuilt import create_react_agent

from ksadk.a2a.inbound_kernel import InboundKernelSubmission
from ksadk.a2a.invocation_service import A2AInvocationService
from ksadk.a2a.kernel_task_adapter import ManagedKernelA2ATaskAdapter
from ksadk.a2a.resume_store import SQLiteA2AResumeStateStore
from ksadk.kernel.worker import AgentKernelWorker
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter
from ksadk.toolsets.a2a import get_a2a_tools
from tests.a2a.test_inbound_kernel import identity
from tests.a2a.test_invocation_service_r85 import Wire, request
from tests.kernel.control_harness import AGENT, command, kernel_stack
from tests.runners.test_langchain_hitl_langgraph import SeqToolModel


def test_verified_user_entry_keeps_compatibility_adapter_and_runtime_never_downgrades():
    from ksadk.a2a.task_adapter import A2ARuntimeTaskAdapter
    bridge = ManagedKernelA2ATaskAdapter(object(), control_plane=object(), session_events=object())
    verified = identity({"message": {}})
    context = SimpleNamespace(call_context=SimpleNamespace(state={"a2a_identity": verified}))
    assert bridge.for_context(context) is bridge
    context.call_context.state["a2a_identity"] = replace(verified, task_binding=None)
    assert bridge.for_context(context) is bridge
    context.call_context.state["a2a_identity"] = replace(verified, caller_principal_type="user",
        caller_runtime_id=None, authn_mode="api_key", task_binding=None)
    assert type(bridge.for_context(context)) is A2ARuntimeTaskAdapter
    context.call_context.state["a2a_identity"] = replace(
        verified, caller_principal_type="user", caller_runtime_id=None, authn_mode="api_key"
    )
    assert bridge.for_context(context) is bridge


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "reject", "cancel"])
@pytest.mark.parametrize("caller_type", ["runtime", "user"])
async def test_task_adapter_resumes_same_kernel_run_after_adapter_reconstruction(monkeypatch, decision, tmp_path, caller_type):
    spec = request().spec
    monkeypatch.setenv("KSADK_A2A_SPACE_ID", spec.space_id)
    model = SeqToolModel(msgs=[AIMessage(content="", tool_calls=[{
        "name": "call_a2a_agent", "id": "call_1", "args": {"agent": "expert", "message": "hello"},
    }]), AIMessage(content="finished")])
    runner = LangGraphRunner(SimpleNamespace(entry_point="agent.py", agent_variable="root_agent"), ".")
    runner._agent = create_react_agent(model, get_a2a_tools())
    wire = Wire()
    async def discover():
        return [SimpleNamespace(agent_id=spec.target_agent_id, version_id=spec.expected_version_id,
                                agent_card=SimpleNamespace(name="expert", description="test"))]
    runtime = LangGraphRuntimeAdapter(runner)
    runtime.configure_managed_a2a(SimpleNamespace(
        outbound_enabled=True, runtime_metadata=SimpleNamespace(agent_id="caller"),
        client_for_space=lambda _: SimpleNamespace(discover=discover),
        invocation_service_for_space=lambda _, **deps: A2AInvocationService(space_client=wire, **deps),
    ))
    session = "a2a-kernel-test"
    stack = await kernel_stack(adapter=runtime, sessions=(session,))
    lease = await stack.lease(session)
    worker = AgentKernelWorker(stack.store, adapter_factory=lambda: runtime, session_events=stack.events)
    initial_id = None
    calls = []
    class Server:
        async def cancel_inbound_task(self, **args):
            calls.append(args)
            cmd = command("interrupt", session_id=session, idempotency_key="cancel", payload={
                "run_id": args["run_id"], "a2a_origin_command_id": str(initial_id),
            })
            receipt = await stack.kernel.submit(cmd, permit=stack.permit("interrupt", session_id=session))
            await worker.run_once(AGENT, lease)
            return InboundKernelSubmission(session, "ar-target", receipt)

        async def submit_inbound_message(self, **args):
            nonlocal initial_id
            calls.append(args)
            pending = args.get("continuation")
            kind = "submit_interaction" if pending else "enqueue"
            if pending:
                payload = {**pending, "token_ref": "test-permit", "action": decision, "response": {},
                           "a2a_origin_command_id": str(initial_id), "a2a_interaction_kind": pending["kind"]}
                payload.pop("kind")
            else:
                payload = {"content": "call expert", "session_context": {}}
            cmd = command(kind, session_id=session, idempotency_key=str(len(calls)), payload=payload)
            if not pending:
                initial_id = cmd.command_id
            receipt = await stack.kernel.submit(cmd, permit=stack.permit(kind, session_id=session))
            await worker.run_once(AGENT, lease)
            return InboundKernelSubmission(session, "ar-target", receipt)
    server = Server()
    states = SQLiteA2AResumeStateStore(tmp_path / "resume.sqlite")
    def adapter():
        return ManagedKernelA2ATaskAdapter(runtime, control_plane=server, session_events=stack.events, event_dispatcher=AsyncMock(),
                                         resume_state_store=states, runtime_type="langgraph")
    first = adapter()
    verified = identity({"message": {"messageId": "first", "parts": [{"text": "call expert"}]}})
    if caller_type == "user":
        verified = replace(verified, caller_principal_type="user", caller_runtime_id=None,
                           authn_mode="api_key")
    context = SimpleNamespace(task_id="remote-task", context_id="context", current_task=None,
                              call_context=SimpleNamespace(tenant="tenant", state={"a2a_identity": verified}))
    handle = await asyncio.wait_for(first.start_task(task_id=context.task_id, context=context, input_data="ignored"), 5)
    async def collect(bridge, active_handle):
        return [event async for event in bridge.stream_task(active_handle)]
    waiting = await asyncio.wait_for(collect(first, handle), 5)
    interaction = next(event for event in waiting if event.event_type == "interaction.requested")
    from ksadk.a2a.interaction_wire import approval_metadata
    preview = approval_metadata(interaction.request)["ksadk_interaction"]["presentation"]
    assert preview["title"] == "call_a2a_agent"
    assert json.loads(preview["description"])["arguments"]["target_agent_id"] == spec.target_agent_id
    assert json.loads(preview["description"])["arguments"]["version_id"] == spec.expected_version_id
    assert not wire.calls
    await first.persist_resume_state(task_id=context.task_id, context=context, handle=handle,
                                    checkpoint_id=None, call_id=interaction.interaction_id, payload_kind="approval_decision")
    context.current_task = SimpleNamespace(id=context.task_id, context_id=context.context_id)
    context.call_context.state["a2a_identity"] = replace(verified, verified_request_body=json.dumps({
        "message": {"messageId": "second", "parts": [{"text": decision}]},
    }).encode())
    def assert_product_facts(bridge):
        if caller_type != "user":
            bridge._event_dispatcher.enqueue.assert_not_awaited()
            return
        batches = bridge._event_dispatcher.enqueue.await_args_list
        assert batches
        for queued in batches:
            envelope = queued.kwargs["events"][0]
            assert queued.kwargs["platform_task_id"] == verified.task_binding.platform_task_id
            assert envelope["fact"]["causation_id"] == str(initial_id)
            assert envelope["fact"]["run_id"] == handle.run_id
            assert envelope["binding"]["remote_task_id"] == context.task_id
    assert_product_facts(first)
    second = adapter()
    if decision == "cancel":
        from ksadk.runtime.adapter import CancelResult
        context.call_context.state["a2a_identity"] = replace(verified,
            task_binding=replace(verified.task_binding, operation="cancel_task"))
        result = await asyncio.wait_for(second.cancel_task(context.task_id, context), 5)
        assert result == CancelResult.INTERRUPTED_ACTIVE_TURN
        run = await stack.store.load_run(handle.run_id)
        assert run.state.value == "cancelled"
        assert not wire.calls
        assert second.was_cancel_accepted(context.task_id, context, handle)
        assert_product_facts(second)
        return
    resumed = await second.resume_task(context.task_id, context, answer=decision)
    assert resumed.run_id == handle.run_id and resumed.session_id == handle.session_id
    completed = await asyncio.wait_for(collect(second, resumed), 5)
    assert any(event.event_type == "run.completed" for event in completed), [event.event_type for event in completed]
    assert sum(kind == "send" for kind, _ in wire.calls) == (1 if decision == "approve" else 0)
    assert calls[1]["continuation"]["interaction_id"] == interaction.interaction_id
    assert all(event.run_id == handle.run_id for event in waiting + completed)

    assert_product_facts(second)
