from types import SimpleNamespace

import pytest
from google.adk.events import Event
from google.genai import types
from google.protobuf.json_format import MessageToDict

from ksadk.a2a.adk_runtime import managed_message_parts
from tests.interaction.test_ledger_conformance import (
    AGENT,
    TENANT,
    acquire_guard,
    memory_backend,  # noqa: F401
    sqlite_backend,  # noqa: F401
)


@pytest.fixture(params=["memory_backend", "sqlite_backend"])
def backend(request):
    return request.getfixturevalue(request.param)


def test_default_context_only_forwards_visible_complete_text():
    def event(text, **fields):
        return Event(
            invocation_id="native",
            author="root",
            content=types.Content(
                role="model",
                parts=[
                    types.Part(text=text),
                    types.Part(text="hidden", thought=True),
                    types.Part(
                        function_call=types.FunctionCall(name="tool", args={"token": "fake"})
                    ),
                ],
            ),
            **fields,
        )

    ctx = SimpleNamespace(
        branch="root.left",
        session=SimpleNamespace(
            events=[
                event("parent", branch="root"),
                event("mine", branch="root.left"),
                event("sibling", branch="root.right"),
                event("partial", partial=True),
            ]
        ),
    )
    assert managed_message_parts(ctx, None) == ("[root]\nparent", "[root]\nmine")


@pytest.mark.asyncio
@pytest.mark.parametrize("root_mode", ["remote", "sequential", "synthesis"])
@pytest.mark.parametrize("remote_outcome", ["completed", "failed", "cancelled", "unknown"])
async def test_runner_binds_managed_service_from_configured_adapter(
    backend, root_mode, remote_outcome
):
    from ksadk.a2a.adk import remote_agent
    from ksadk.a2a.invocation_service import A2AInvocationService
    from ksadk.events.run_authority import RunEventAuthority, bind_run_event_authority
    from ksadk.runtime.framework_adapters import ADKRuntimeAdapter
    from ksadk.runtime_context import (
        reset_current_invocation_context,
        session_invocation_context,
        set_current_invocation_context,
    )
    from tests.a2a.test_invocation_service_r85 import Wire, request
    from tests.runners.test_adk_multi_llm_runtime_events import runner_for

    ledger, events = backend
    authority = RunEventAuthority(
        events,
        session_id="s1",
        run_id="run-1",
        guard=await acquire_guard(ledger),
        interaction_ledger=ledger,
        tenant_id=TENANT,
        agent_instance_id=AGENT,
    )
    agent = remote_agent(request().spec)
    ctx = SimpleNamespace(
        agent=agent,
        invocation_id="native-1",
        branch=None,
        session=SimpleNamespace(events=[]),
        user_content=types.Content(role="user", parts=[types.Part(text="hello from runtime")]),
    )
    runner = runner_for([])
    from google.adk.agents import LlmAgent, SequentialAgent

    root = agent
    if root_mode != "remote":
        children = [agent]
        if root_mode == "synthesis":
            children.append(LlmAgent(name="summarizer", model="fixture"))
        root = SequentialAgent(name="root", sub_agents=children)
    runner._agent = root

    async def native_events():
        async for event in agent._run_async_impl(ctx):
            yield event
        if root_mode == "synthesis":
            yield Event(
                invocation_id="native-1",
                author="summarizer",
                content=types.Content(role="model", parts=[types.Part(text="root summary")]),
            )

    async def prepare(**_kwargs):
        return native_events()

    runner._prepare_run_events = prepare
    wire, selected = Wire(), []
    if remote_outcome == "unknown":
        wire.events = []
    elif remote_outcome in {"failed", "cancelled"}:
        from a2a.types import TaskState

        wire.task.status.state = (
            TaskState.TASK_STATE_FAILED
            if remote_outcome == "failed"
            else TaskState.TASK_STATE_CANCELED
        )

    def service_for_space(space, **dependencies):
        selected.append(space)
        assert dependencies["event_store"] is authority
        return A2AInvocationService(space_client=wire, **dependencies)

    bootstrap = SimpleNamespace(
        outbound_enabled=True,
        runtime_metadata=SimpleNamespace(agent_id="caller"),
        invocation_service_for_space=service_for_space,
    )
    adapter = ADKRuntimeAdapter(runner)
    adapter.configure_managed_a2a(bootstrap)
    invocation = session_invocation_context({}, agent_id="caller", session_id="s1")
    invocation.tool_approval_mode = "full"
    token = set_current_invocation_context(invocation)
    try:
        with bind_run_event_authority(authority):
            output = []
            async for event in runner.stream_canonical_events(
                {"input": "hello", "run_id": "private-adapter-run", "session_id": "s1"}
            ):
                persisted, _ = await authority.persist_one("s1", event)
                output.append(persisted)
        assert selected == [request().spec.space_id]
        assert sum(kind == "send" for kind, _ in wire.calls) == 1
        if remote_outcome == "unknown" or (
            remote_outcome in {"failed", "cancelled"} and root_mode != "synthesis"
        ):
            assert output[-1].event_type == "run.failed"
            assert output[-1].error.code == (
                "A2A_DELEGATE_FAILED"
                if remote_outcome in {"failed", "cancelled"}
                else "A2A_DELEGATE_OUTCOME_UNCONFIRMED"
            )
            assert not any(event.event_type == "run.completed" for event in output)
            descriptors = [
                part.data
                for event in output
                for part in getattr(getattr(event, "snapshot", None), "parts", ())
                if getattr(part, "data", {}).get("schema") == "execution.scope/v1"
            ]
            assert descriptors[-1]["status"] == (
                "working" if remote_outcome == "unknown" else remote_outcome
            )
            return
        assert output[-1].event_type == "run.completed"
        assert all(event.run_id == "run-1" for event in output)
        assert output[-1].output_refs
        from ksadk.conversations.runtime_projection import RuntimeConversationProjection
        from ksadk.runners._adk_canonical_stream import legacy_chunks

        projection = RuntimeConversationProjection("s1", profile="flat-v1")
        projected = [projection.apply(event) for event in output]
        answers = [
            item
            for item in projected
            if item and item.kind == "assistant_text" and item.visibility == "public"
        ]
        expected = "root summary" if root_mode == "synthesis" else "hello"
        completed_answers = [item for item in answers if item.lifecycle == "completed"]
        assert len(completed_answers) == 1
        assert completed_answers[0].payload["text"] == expected

        async def replay():
            for event in output:
                yield event

        chunks = [chunk async for chunk in legacy_chunks(replay())]
        assert [chunk["output"] for chunk in chunks if chunk["type"] == "final"] == [expected]
        text_chunks = [chunk for chunk in chunks if chunk["type"] == "text"]
        if root_mode != "synthesis":
            assert not text_chunks
        audit = [
            event
            for event in await events.read("s1", 0, 100)
            if event.event_type == "control.a2a_policy_evaluated"
        ]
        assert audit and all(event.payload["action"] == "allow" for event in audit)
    finally:
        reset_current_invocation_context(token)


def test_server_wires_explicit_cached_and_future_adapters():
    from unittest.mock import Mock

    from fastapi import FastAPI

    from ksadk.a2a.bootstrap import AgentEngineA2ABootstrap
    from ksadk.runtime.adapter import RuntimeRegistry
    from ksadk.runtime.executor import RuntimeExecutor
    from ksadk.runtime.framework_adapters import ADKRuntimeAdapter
    from ksadk.runtime.launch import RuntimeLaunchContext
    from ksadk.server.factory import RuntimeAppConfig, _wire_a2a_if_enabled
    from tests.runners.test_adk_multi_llm_runtime_events import runner_for

    registry = RuntimeRegistry()
    registry.register("adk", lambda _: ADKRuntimeAdapter(runner_for([])))
    executor = RuntimeExecutor(registry)
    launch = RuntimeLaunchContext(runtime_type="adk", project_dir=".")
    cached = executor.capability_adapter(launch)
    explicit = ADKRuntimeAdapter(runner_for([]))
    bootstrap = Mock(spec=AgentEngineA2ABootstrap)
    bootstrap.runtime_metadata = SimpleNamespace(agent_id="caller")
    bootstrap.inbound_enabled = True
    config = RuntimeAppConfig(
        a2a=bootstrap,
        a2a_runtime_adapter=explicit,
        runtime_executor=executor,
        launch_context=launch,
    )
    _wire_a2a_if_enabled(FastAPI(), SimpleNamespace(), config)
    for adapter in (explicit, cached, executor.create_adapter(launch)):
        assert adapter._runner._managed_a2a_bootstrap is bootstrap


def test_delegation_selection_requires_explicit_transfer_or_final_static_child():
    from google.adk.agents import LlmAgent, ParallelAgent, SequentialAgent

    from ksadk.a2a.adk import remote_agent
    from ksadk.runners._adk_canonical_stream import _delegated_binding
    from tests.a2a.test_invocation_service_r85 import request

    def remote(name):
        return remote_agent(
            request().spec.model_copy(update={"local_name": name, "binding_id": name})
        )

    root = LlmAgent(name="root", model="fixture", sub_agents=[remote("left"), remote("right")])
    assert _delegated_binding(root, None) is None
    assert _delegated_binding(root, "right") == "right"
    assert _delegated_binding(root, "missing") is None
    parallel = ParallelAgent(name="parallel", sub_agents=[remote("left"), remote("right")])
    assert _delegated_binding(parallel, None) is None
    sequential = SequentialAgent(name="sequence", sub_agents=[remote("left"), remote("right")])
    assert _delegated_binding(sequential, None) == "right"


@pytest.mark.parametrize("enabled", [False, True])
def test_managed_approval_capability_is_configured_and_live_only(enabled):
    from ksadk.runtime.framework_adapters import ADKRuntimeAdapter
    from tests.runners.test_adk_multi_llm_runtime_events import runner_for

    adapter = ADKRuntimeAdapter(runner_for([]))
    assert not adapter.capabilities().submit_interaction.supported
    adapter.configure_managed_a2a(SimpleNamespace(outbound_enabled=enabled))
    matrix = adapter.capabilities()
    assert matrix.submit_interaction.supported is enabled
    assert matrix.interaction_mode == ("live_submit" if enabled else "unavailable")
    if enabled:
        assert matrix.submit_interaction.reason == "managed_a2a_live_interaction_only"
        assert not matrix.durable_restore.supported


@pytest.mark.asyncio
async def test_managed_capability_does_not_admit_unknown_or_native_adk_interaction():
    from ksadk.runtime.framework_adapters import ADKRuntimeAdapter
    from tests.interaction.test_ledger_conformance import make_record
    from tests.kernel.control_harness import FakeAdapter, command, kernel_stack
    from tests.runners.test_adk_multi_llm_runtime_events import runner_for

    adapter = ADKRuntimeAdapter(runner_for([]))
    adapter.configure_managed_a2a(SimpleNamespace(outbound_enabled=True))
    stack = await kernel_stack(adapter=FakeAdapter(matrix=adapter.capabilities()))
    record = make_record("native-approval").model_copy(update={"provider_id": "adk"})
    await stack.store.request(record, guard=await acquire_guard(stack.store))
    for interaction_id in ("missing", record.interaction_id):
        receipt = await stack.kernel.submit(
            command(
                "submit_interaction",
                idempotency_key=interaction_id,
                payload={
                    "run_id": record.run_id,
                    "interaction_id": interaction_id,
                    "token_ref": "fake-ref",
                    "action": "approve",
                    "response": {},
                },
            ),
            permit=stack.permit("submit_interaction"),
        )
        assert receipt.status == "unsupported"
    assert (await stack.store.get(record.interaction_id)).status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["none", "continue", "cancel", "timeout"])
async def test_kernel_approval_resumes_real_managed_adk_runner(flow):
    import asyncio

    needs_input = flow != "none"

    from ksadk.a2a.adk import remote_agent
    from ksadk.a2a.invocation_service import A2AInvocationService
    from ksadk.kernel.state import RunState
    from ksadk.kernel.worker import AgentKernelWorker
    from ksadk.runtime.framework_adapters import ADKRuntimeAdapter
    from tests.a2a.test_invocation_service_r85 import Wire, request
    from tests.kernel.control_harness import command, kernel_stack
    from tests.runners.test_adk_multi_llm_runtime_events import runner_for

    spec = request().spec
    if flow == "timeout":
        spec = spec.model_copy(update={"timeout_seconds": 1.0})
    agent = remote_agent(spec)
    runner = runner_for([])
    runner._agent = agent
    ctx = SimpleNamespace(
        agent=agent,
        invocation_id="native-approval",
        branch=None,
        session=SimpleNamespace(events=[]),
        user_content=types.Content(role="user", parts=[types.Part(text="hello")]),
    )

    async def prepare(**_kwargs):
        return agent._run_async_impl(ctx)

    runner._prepare_run_events = prepare
    wire = Wire()
    if flow == "timeout":
        async def record_cancel(prepared):
            wire.calls.append(("remote_cancel", prepared))
            return wire.task
        wire.cancel_prepared_task = record_cancel
    completed_task = wire.task
    if needs_input:
        from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus

        pending_task = Task(
            id="remote-1",
            context_id="context-1",
            status=TaskStatus(
                state=TaskState.TASK_STATE_INPUT_REQUIRED,
                message=Message(
                    message_id="question",
                    role=Role.ROLE_AGENT,
                    parts=[Part(text="Please provide input")],
                ),
            ),
        )
        wire.task, wire.events = pending_task, [pending_task]
    adapter = ADKRuntimeAdapter(runner)
    adapter.configure_managed_a2a(
        SimpleNamespace(
            outbound_enabled=True,
            runtime_metadata=SimpleNamespace(agent_id="caller"),
            invocation_service_for_space=lambda space, **deps: A2AInvocationService(
                space_client=wire, **deps
            ),
        )
    )
    stack = await kernel_stack(adapter=adapter)
    lease = await stack.lease()
    worker = AgentKernelWorker(
        stack.store, adapter_factory=lambda: adapter, session_events=stack.events
    )
    await stack.kernel.submit(
        command(
            idempotency_key="real-managed", payload={"content": "hello", "session_context": {}}
        ),
        permit=stack.permit("enqueue"),
    )
    started = await worker.run_once(AGENT, lease)

    async def pending():
        while True:
            records = await stack.store.list_pending_interactions(TENANT, "s1")
            run = await stack.store.load_run(started.run_id)
            if records and run.state == RunState.WAITING:
                return records[0]
            if run.state in {RunState.COMPLETED, RunState.FAILED}:
                rows = await stack.events.read("s1", 0, 100)
                pytest.fail(
                    str(
                        [
                            (row.event_type, row.payload)
                            for row in rows
                            if "failed" in row.event_type
                        ]
                    )
                )
            await asyncio.sleep(0.01)

    record = await asyncio.wait_for(pending(), timeout=3)
    assert record.provider_id == "managed-a2a"
    assert not wire.calls
    reply = await stack.kernel.submit(
        command(
            "submit_interaction",
            idempotency_key="real-managed-reply",
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
    assert reply.status == "accepted"
    assert (await worker.run_once(AGENT, lease)).outcome == "completed"

    if needs_input:
        input_record = await asyncio.wait_for(pending(), timeout=3)
        assert input_record.kind == "structured_input"
        assert input_record.provider_id == "managed-a2a"
        assert (await stack.store.load_run(started.run_id)).state == RunState.WAITING
        assert sum(kind == "send" for kind, _ in wire.calls) == 1
        if flow == "timeout":
            async def failed():
                while True:
                    run = await stack.store.load_run(started.run_id)
                    if run.state == RunState.FAILED:
                        return run
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(failed(), 3)
            assert sum(kind == "send" for kind, _ in wire.calls) == 1
            assert sum(kind == "remote_cancel" for kind, _ in wire.calls) == 1
            return
        if flow == "cancel":
            stopped = await stack.kernel.submit(
                command("interrupt", idempotency_key="stop-input"), permit=stack.permit("interrupt")
            )
            assert stopped.status == "accepted"
            assert (await asyncio.wait_for(worker.run_once(AGENT, lease), 3)).outcome == "completed"
            assert (await stack.store.load_run(started.run_id)).state == RunState.CANCELLED
            assert not await stack.store.list_pending_interactions(TENANT, "s1")
            assert sum(kind == "send" for kind, _ in wire.calls) == 1
            from tests.studio.test_r85_presentation import (
                assert_managed_input_live_and_cursor_replay,
            )

            assert_managed_input_live_and_cursor_replay(
                await stack.events.read("s1", 0, 200), input_record.interaction_id
            )
            return
        wire.task, wire.events = completed_task, [completed_task]
        input_command = command(
            "submit_interaction",
            idempotency_key="remote-input",
            payload={
                "run_id": started.run_id,
                "interaction_id": input_record.interaction_id,
                "token_ref": "fake-ref",
                "action": "submit",
                "response": "supplement",
                "expected_revision": input_record.revision,
            },
        )
        receipt = await stack.kernel.submit(
            input_command, permit=stack.permit("submit_interaction")
        )
        assert receipt.status == "accepted"
        assert (await worker.run_once(AGENT, lease)).outcome == "completed"
        continuation_approval = await asyncio.wait_for(pending(), timeout=3)
        assert continuation_approval.kind == "approval"
        assert continuation_approval.interaction_id != record.interaction_id
        assert sum(kind == "send" for kind, _ in wire.calls) == 1
        duplicate = await stack.kernel.submit(
            input_command, permit=stack.permit("submit_interaction")
        )
        assert duplicate.status == "duplicate"
        receipt = await stack.kernel.submit(
            command(
                "submit_interaction",
                idempotency_key="approve-supplement",
                payload={
                    "run_id": started.run_id,
                    "interaction_id": continuation_approval.interaction_id,
                    "token_ref": "fake-ref",
                    "action": "approve",
                    "response": {},
                    "expected_revision": continuation_approval.revision,
                },
            ),
            permit=stack.permit("submit_interaction"),
        )
        assert receipt.status == "accepted"
        assert (await worker.run_once(AGENT, lease)).outcome == "completed"

    async def completed():
        while True:
            run = await stack.store.load_run(started.run_id)
            if run.state in {RunState.COMPLETED, RunState.FAILED}:
                return run
            await asyncio.sleep(0.01)

    run = await asyncio.wait_for(completed(), timeout=3)
    assert run.state == RunState.COMPLETED
    assert sum(kind == "send" for kind, _ in wire.calls) == (2 if needs_input else 1)
    assert (await stack.store.get(record.interaction_id)).status == "resolved"

    if needs_input:
        sends = [message for kind, message in wire.calls if kind == "send"]
        assert sends[-1].task_id == "remote-1" and sends[-1].context_id == "context-1"
        assert MessageToDict(sends[-1].parts[0].data) == "supplement"
        assert not await stack.store.list_pending_interactions(TENANT, "s1")

        from tests.studio.test_r85_presentation import assert_managed_input_live_and_cursor_replay

        assert_managed_input_live_and_cursor_replay(
            await stack.events.read("s1", 0, 200), input_record.interaction_id
        )


@pytest.mark.asyncio
async def test_unmanaged_adk_adapter_uses_kernel_root_identity(backend):
    from ksadk.events.reducer import StreamReducer
    from ksadk.events.run_authority import RunEventAuthority, bind_run_event_authority
    from ksadk.runtime.adapter import StartRequest
    from ksadk.runtime.framework_adapters import ADKRuntimeAdapter
    from tests.runners.test_adk_multi_llm_runtime_events import native_text_event, runner_for

    ledger, events = backend
    runner = runner_for([native_text_event("hello", "native-final", partial=False)])
    adapter = ADKRuntimeAdapter(runner)
    handle = await adapter.start(StartRequest(input="hello", user_id="user", session_id="s1"))
    authority = RunEventAuthority(
        events, session_id="s1", run_id="run-1", guard=await acquire_guard(ledger)
    )
    reducer = StreamReducer()
    with bind_run_event_authority(authority):
        output = []
        async for event in adapter.stream(handle):
            event, _ = await authority.persist_one("s1", event)
            reducer.apply(event)
            output.append(event)
    roots = [event for event in output if event.event_type.startswith("run.")]
    assert [event.event_type for event in roots] == ["run.started", "run.completed"]
    assert len({event.scope_id for event in roots}) == 1
    assert all(event.run_id == "run-1" for event in roots)
    assert handle.run_id != "run-1"
