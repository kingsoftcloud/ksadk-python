import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ksadk.codex.client import AsyncCodexClient
from ksadk.codex.runtime import CodexRuntimeAdapter
from ksadk.runtime.adapter import StartRequest
from ksadk.events.run_authority import bind_run_event_authority
from tests.a2a.test_invocation_service_r85 import request
from tests.a2a.test_langgraph_invocation_context import setup_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("persist_trigger, checkpoint_failure", [
    (True, False), (False, False), (True, True), (True, "terminal"),
])
async def test_real_sdk_runtime_expert_waits_for_persisted_native_trigger(monkeypatch, persist_trigger, checkpoint_failure, tmp_path):
    from openai_codex import CodexConfig
    from ksadk.a2a.invocation_policy import A2AInvocationPolicy, A2AInvocationPolicyDecision

    authority, service, wire, _ = await setup_runtime()
    from ksadk.kernel.memory_store import InMemoryAgentKernelStore
    from ksadk.kernel.store import ActivationLeaseRequest, RunRecord
    from ksadk.kernel.state import RunState
    store = InMemoryAgentKernelStore(authority._events)
    lease = await store.acquire_activation(ActivationLeaseRequest(
        agent_instance_id='caller', session_id='s', activation_id='test', lease_ttl_seconds=60))
    run = RunRecord(run_id='public-run', agent_instance_id='caller', session_id='s',
                    state=RunState.PENDING)
    await store.save_run_transition(run, expected_fence=lease.fencing_token)
    await store.save_run_transition(run.model_copy(update={'state': RunState.RUNNING}),
                                    expected_fence=lease.fencing_token)
    authority._ledger, authority._agent, authority._tenant = store, 'caller', 'user'
    if checkpoint_failure:
        original_save = store.save_runtime_checkpoint
        async def unavailable(*args, **kwargs):
            if checkpoint_failure == 'terminal' and args[1].get('schema') != 'codex.a2a.session/v1':
                return await original_save(*args, **kwargs)
            raise RuntimeError('fixture checkpoint unavailable')
        monkeypatch.setattr(store, 'save_runtime_checkpoint', unavailable)
    from contextlib import asynccontextmanager
    original_stream = wire.open_prepared_message_stream
    @asynccontextmanager
    async def checked_stream(*args, **kwargs):
        checkpoint = (await store.load_run('public-run')).metadata['runtime_checkpoint']
        assert checkpoint['run_id'] == 'public-run'
        assert checkpoint['calls'][0]['callId'] == 'expert-call'
        async with original_stream(*args, **kwargs) as stream:
            yield stream
    monkeypatch.setattr(wire, 'open_prepared_message_stream', checked_stream)
    monkeypatch.setattr(authority, 'managed_invocation_policy', lambda: A2AInvocationPolicy(
        evaluator=lambda *_: A2AInvocationPolicyDecision('allow')))
    def service_for_space(space, *, event_store, policy):
        assert space == request().spec.space_id
        assert event_store is authority
        return service
    bootstrap = SimpleNamespace(outbound_enabled=True, runtime_metadata=SimpleNamespace(agent_id='caller'),
        invocation_service_for_space=service_for_space)
    client = AsyncCodexClient(config=CodexConfig(env={'CODEX_HOME': str(tmp_path / 'codex')}, launch_args_override=(sys.executable,
        str(Path(__file__).with_name('a2a_app_server.py')))))
    adapter = CodexRuntimeAdapter(client, a2a_experts=[request().spec], managed_a2a_bootstrap=bootstrap)
    events = []
    async def run():
        with bind_run_event_authority(authority):
            handle = await adapter.start(StartRequest(session_id='s', user_id='user', input='expert',
                metadata={'invocation_id': 'public-run'}))
            async for event in adapter.stream(handle):
                if event.event_type == 'run.completed':
                    checkpoint = (await store.load_run('public-run')).metadata['runtime_checkpoint']
                    assert checkpoint['schema'] == 'codex.a2a.session/v1'
                if persist_trigger or getattr(event, 'item_kind', None) != 'tool_call':
                    await authority.persist_one('s', event)
                events.append(event)
    try:
        await asyncio.wait_for(run(), 10)
    finally:
        await adapter.close_all()
    assert events[-1].event_type == ('run.failed' if checkpoint_failure else 'run.completed')
    invoked = persist_trigger and checkpoint_failure is not True
    assert any(e.source.framework == 'a2a' for e in events) is invoked
    assert sum(kind == 'send' for kind, _ in wire.calls) == int(invoked)
    native = [e for e in events if getattr(e, 'item_kind', None) == 'tool_call' and e.source.framework == 'codex']
    assert native and native[0].source.native_item_id == 'expert-call'
    assert all(e.run_id == 'public-run' for e in events)
    assert ('runtime_checkpoint' in (await store.load_run('public-run')).metadata) is (
        checkpoint_failure is not True)
    if checkpoint_failure:
        assert not any(e.event_type == 'run.completed' for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['approve', 'reject', 'cancel', 'input', 'cancel_input', 'timeout'])
async def test_expert_kernel_owns_live_approval_and_cancel(action, monkeypatch, tmp_path):
    from openai_codex import CodexConfig
    from ksadk.a2a.invocation_service import A2AInvocationService
    from ksadk.kernel.state import RunState
    from ksadk.kernel.worker import AgentKernelWorker
    from tests.a2a.test_invocation_service_r85 import Wire
    from tests.kernel.control_harness import AGENT, TENANT, command, kernel_stack

    errors = []
    from ksadk.a2a.codex import CodexExpert
    original_invoke = CodexExpert.invoke
    async def capture(self, *args, **kwargs):
        try:
            return await original_invoke(self, *args, **kwargs)
        except Exception as exc:
            errors.append(repr(exc))
            raise
    monkeypatch.setattr(CodexExpert, 'invoke', capture)
    wire = Wire()
    async def cancel_prepared(prepared):
        wire.calls.append(('cancel', prepared))
        return wire.task
    monkeypatch.setattr(wire, 'cancel_prepared_task', cancel_prepared)
    completed_task = wire.task
    needs_input = action in {"input", "cancel_input", "timeout"}
    if needs_input:
        from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus
        wire.task = Task(id="remote-1", context_id="context-1", status=TaskStatus(
            state=TaskState.TASK_STATE_INPUT_REQUIRED,
            message=Message(message_id="question", role=Role.ROLE_AGENT,
                parts=[Part(text="Please provide input")]),
        ))
        wire.events = [wire.task]
    bootstrap = SimpleNamespace(outbound_enabled=True, runtime_metadata=SimpleNamespace(agent_id='caller'),
        invocation_service_for_space=lambda _, **deps: A2AInvocationService(space_client=wire, **deps))
    client = AsyncCodexClient(config=CodexConfig(env={'CODEX_HOME': str(tmp_path / 'codex')}, launch_args_override=(sys.executable,
        str(Path(__file__).with_name('a2a_app_server.py')))))
    spec = request().spec.model_copy(update={'timeout_seconds': 1.0}) if action == 'timeout' else request().spec
    adapter = CodexRuntimeAdapter(client, a2a_experts=[spec], managed_a2a_bootstrap=bootstrap)
    stack = await kernel_stack(adapter=adapter)
    lease = await stack.lease()
    worker = AgentKernelWorker(stack.store, adapter_factory=lambda: adapter, session_events=stack.events)
    try:
        await stack.kernel.submit(command(idempotency_key='expert-run',
            payload={'content': 'call expert', 'session_context': {}}), permit=stack.permit('enqueue'))
        started = await worker.run_once(AGENT, lease)
        async def pending():
            while True:
                records = await stack.store.list_pending_interactions(TENANT, 's1')
                if records:
                    return records[0]
                run = await stack.store.load_run(started.run_id)
                assert run.state not in {RunState.COMPLETED, RunState.FAILED}, errors
                await asyncio.sleep(.01)
        record = await asyncio.wait_for(pending(), 5)
        assert not wire.calls
        if action == 'cancel':
            receipt = await stack.kernel.submit(command('interrupt', idempotency_key='cancel'),
                permit=stack.permit('interrupt'))
        else:
            receipt = await stack.kernel.submit(command('submit_interaction', idempotency_key='reply', payload={
                'run_id': started.run_id, 'interaction_id': record.interaction_id,
                'token_ref': 'fake-ref', 'action': 'approve' if needs_input else action, 'response': {}, 'expected_revision': record.revision}),
                permit=stack.permit('submit_interaction'))
        assert receipt.status == 'accepted'
        assert (await asyncio.wait_for(worker.run_once(AGENT, lease), 5)).outcome == 'completed'
        if needs_input:
            input_record = await asyncio.wait_for(pending(), 5)
            assert input_record.kind == "structured_input"
            assert input_record.provider_id == "managed-a2a"
            assert sum(kind == "send" for kind, _ in wire.calls) == 1
            if action == "timeout":
                pass
            elif action == "cancel_input":
                receipt = await stack.kernel.submit(command("interrupt", idempotency_key="cancel-input"),
                    permit=stack.permit("interrupt"))
                assert receipt.status == "accepted"
                assert (await asyncio.wait_for(worker.run_once(AGENT, lease), 5)).outcome == "completed"
            else:
                wire.task, wire.events = completed_task, [completed_task]
                input_command = command("submit_interaction", idempotency_key="input", payload={
                    "run_id": started.run_id, "interaction_id": input_record.interaction_id,
                    "token_ref": "fake-ref", "action": "submit", "response": "supplement",
                    "expected_revision": input_record.revision})
                receipt = await stack.kernel.submit(input_command, permit=stack.permit("submit_interaction"))
                assert receipt.status == "accepted"
                assert (await worker.run_once(AGENT, lease)).outcome == "completed"
                continuation = await asyncio.wait_for(pending(), 5)
                assert continuation.kind == "approval"
                assert continuation.interaction_id != record.interaction_id
                assert sum(kind == "send" for kind, _ in wire.calls) == 1
                duplicate = await stack.kernel.submit(input_command, permit=stack.permit("submit_interaction"))
                assert duplicate.status == "duplicate"
                receipt = await stack.kernel.submit(command("submit_interaction", idempotency_key="continue", payload={
                    "run_id": started.run_id, "interaction_id": continuation.interaction_id,
                    "token_ref": "fake-ref", "action": "approve", "response": {},
                    "expected_revision": continuation.revision}), permit=stack.permit("submit_interaction"))
                assert receipt.status == "accepted"
                assert (await worker.run_once(AGENT, lease)).outcome == "completed"
        async def finished():
            while True:
                run = await stack.store.load_run(started.run_id)
                if run.state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}:
                    return run
                await asyncio.sleep(.01)
        run = await asyncio.wait_for(finished(), 5)
        assert run.state == (RunState.FAILED if action == 'timeout' else RunState.CANCELLED if action in {'cancel', 'cancel_input'} else RunState.COMPLETED)
        assert sum(kind == 'send' for kind, _ in wire.calls) == (2 if action == 'input' else int(action in {'approve', 'cancel_input', 'timeout'}))
        if action == 'input':
            sends = [message for kind, message in wire.calls if kind == 'send']
            assert sends[-1].task_id == 'remote-1' and sends[-1].context_id == 'context-1'
        if action in {'cancel_input', 'timeout'}:
            assert sum(kind == 'cancel' for kind, _ in wire.calls) == 1
        assert not await stack.store.list_pending_interactions(TENANT, 's1')
    finally:
        await asyncio.wait_for(adapter.close_all(), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile,environment", [("ask", "full"), ("risk", "full"), ("full", "ask")]
)
@pytest.mark.parametrize("ambient", [False, True])
async def test_kernel_expert_request_profile_overrides_environment(
    profile, environment, ambient, monkeypatch, tmp_path
):
    from contextlib import nullcontext

    from openai_codex import CodexConfig

    from ksadk.a2a.invocation_service import A2AInvocationService
    from ksadk.kernel.state import RunState
    from ksadk.kernel.worker import AgentKernelWorker
    from ksadk.runtime_context import platform_invocation_scope, session_invocation_context
    from tests.a2a.test_invocation_service_r85 import Wire
    from tests.kernel.control_harness import AGENT, TENANT, command, kernel_stack

    monkeypatch.setenv("KSADK_TOOL_APPROVAL_MODE", environment)
    wire = Wire()
    bootstrap = SimpleNamespace(
        outbound_enabled=True,
        runtime_metadata=SimpleNamespace(agent_id="caller"),
        invocation_service_for_space=lambda _, **deps: A2AInvocationService(
            space_client=wire, **deps
        ),
    )
    client = AsyncCodexClient(
        config=CodexConfig(
            env={"CODEX_HOME": str(tmp_path / "codex")},
            launch_args_override=(
                sys.executable,
                str(Path(__file__).with_name("a2a_app_server.py")),
            ),
        )
    )
    adapter = CodexRuntimeAdapter(
        client, a2a_experts=[request().spec], managed_a2a_bootstrap=bootstrap
    )
    stack = await kernel_stack(adapter=adapter)
    lease = await stack.lease()
    worker = AgentKernelWorker(
        stack.store, adapter_factory=lambda: adapter, session_events=stack.events
    )
    outer = session_invocation_context(
        None,
        agent_id="caller",
        user_id="kernel-user",
        session_id="s1",
        runner_type="codex",
        tool_approval_mode=environment,
    )
    try:
        await stack.kernel.submit(
            command(
                idempotency_key="request-profile",
                payload={
                    "content": "call expert",
                    "runtime_options": {"tool_approval_mode": profile},
                },
            ),
            permit=stack.permit("enqueue"),
        )
        with platform_invocation_scope(outer) if ambient else nullcontext():
            started = await worker.run_once(AGENT, lease)
        if profile != "full":
            async with asyncio.timeout(5):
                while True:
                    records = await stack.store.list_pending_interactions(TENANT, "s1")
                    if records:
                        break
                    run = await stack.store.load_run(started.run_id)
                    assert run.state not in {RunState.COMPLETED, RunState.FAILED}
                    await asyncio.sleep(0.01)
            assert not wire.calls
            record = records[0]
            assert record.kind == "approval"
            await stack.kernel.submit(
                command(
                    "submit_interaction",
                    idempotency_key="allow",
                    payload={
                        "run_id": started.run_id,
                        "interaction_id": record.interaction_id,
                        "token_ref": "fake-ref",
                        "action": "approve",
                        "response": {},
                        "expected_revision": record.revision,
                    },
                ),
                permit=stack.permit("submit_interaction"),
            )
            assert (await worker.run_once(AGENT, lease)).outcome == "completed"
        async with asyncio.timeout(5):
            while True:
                run = await stack.store.load_run(started.run_id)
                if run.state in {RunState.COMPLETED, RunState.FAILED}:
                    break
                await asyncio.sleep(0.01)
        assert run.state is RunState.COMPLETED
        assert sum(kind == "send" for kind, _ in wire.calls) == 1
        assert not await stack.store.list_pending_interactions(TENANT, "s1")
        assert outer.tool_approval_mode == environment
    finally:
        await adapter.close_all()
