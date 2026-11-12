import time
from contextlib import asynccontextmanager

import pytest
from a2a.types import Artifact, Message, Part, Role, Task, TaskState, TaskStatus

from ksadk.a2a.control_plane import (
    A2ARoute,
    A2ARouteInterface,
    A2ATarget,
    PreparedA2AOperation,
    RemoteTaskReference,
)
from ksadk.a2a.invocation_policy import A2AInvocationPolicy, A2AInvocationPolicyDecision
from ksadk.a2a.invocation_service import A2AInvocationService
from ksadk.a2a.invocation_types import (
    A2AInvocationContext,
    A2AInvocationHandle,
    A2AInvocationMessage,
    A2AInvocationRequest,
    RemoteAgentSpec,
)
from ksadk.a2a.presentation import ScopeTriggerRef
from ksadk.events.canonical_store import RuntimeEventStore
from ksadk.sessions.in_memory import InMemorySessionService


def request():
    return A2AInvocationRequest(
        spec=RemoteAgentSpec(
            local_name="expert",
            binding_id="expert",
            space_id="a2a-space-00000000000040008000000000000001",
            target_agent_id="a2a-agent-00000000000040008000000000000001",
            expected_version_id="a2a-version-00000000000040008000000000000001",
        ),
        handle=A2AInvocationHandle(
            run_id="public-run",
            session_id="s",
            parent_scope_id="root",
            call_id="call_1",
            binding_id="expert",
            target_agent_id="a2a-agent-00000000000040008000000000000001",
            attempt_id="attempt-1",
        ),
        message=A2AInvocationMessage(message_id="message-1", text_parts=("hello",)),
        context=A2AInvocationContext(caller_agent_id="caller", audit_id="audit-1"),
        deadline_unix_seconds=time.time() + 60,
        idempotency_token="idem-1",
    )


class Wire:
    def __init__(self, events=None, task=None):
        self.control_plane = self
        self.calls = []
        self.binding = None
        self.task = task or Task(
            id="remote-1",
            context_id="context-1",
            status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
            artifacts=[Artifact(artifact_id="a1", parts=[Part(text="hello")])],
        )
        self.events = events if events is not None else [self.task]

    def prepared(self):
        return PreparedA2AOperation(
            platform_task_id="a2a-task-00000000000040008000000000000001",
            target=A2ATarget(
                "a2a-agent-00000000000040008000000000000001",
                "a2a-version-00000000000040008000000000000001",
                "hash",
            ),
            route=A2ARoute(
                "hosted_gateway", A2ARouteInterface("https://example.com", "JSONRPC", "1.0")
            ),
            call_permit="fake",
            call_permit_expires_at="2099",
            remote_task=self.binding,
        )

    async def prepare_call(self, **kwargs):
        self.calls.append(("prepare", kwargs))
        return self.prepared()

    async def prepare_task_operation(self, **kwargs):
        self.calls.append(("operation", kwargs))
        return self.prepared()

    async def bind_remote_task(self, **kwargs):
        self.calls.append(("bind", kwargs))
        self.binding = RemoteTaskReference(kwargs["remote_task_id"], kwargs["remote_context_id"])

    @asynccontextmanager
    async def open_prepared_message_stream(self, prepared, message):
        self.calls.append(("send", message))

        async def stream():
            for event in self.events:
                yield event

        yield stream()

    async def get_prepared_task(self, prepared):
        self.calls.append(("get", prepared))
        return self.task

    async def cancel_prepared_task(self, prepared):
        return self.task


async def service(wire=None, policy=None):
    sessions = InMemorySessionService()
    await sessions.create_session(agent_id="caller", user_id="u", session_id="s")
    store = RuntimeEventStore(sessions)
    wire = wire or Wire()
    policy = policy or A2AInvocationPolicy(
        evaluator=lambda *_: A2AInvocationPolicyDecision("allow")
    )
    return A2AInvocationService(space_client=wire, event_store=store, policy=policy), store, wire


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,approval,allowed",
    [("risk", None, False), ("ask", None, False), ("full", None, True),
     ("risk", "approved", True), ("full", "denied", False)],
)
async def test_runtime_policy_enforces_shared_profile_before_prepare(mode, approval, allowed):
    from ksadk.a2a.invocation_policy import runtime_a2a_invocation_policy
    from ksadk.runtime_context import (
        reset_current_invocation_context,
        session_invocation_context,
        set_current_invocation_context,
    )

    decisions = []

    async def audit(req, decision, route):
        decisions.append((decision.action, route))

    policy = runtime_a2a_invocation_policy(
        audit=audit, approval_resolver=lambda _request: approval
    )
    svc, _, wire = await service(policy=policy)
    req = request()
    # A correlation ID alone must never authorize the request.
    req = req.model_copy(update={"context": req.context.model_copy(
        update={"approval_request_id": "claimed-approval"}
    )})
    context = session_invocation_context({}, agent_id="caller", session_id="s")
    context.tool_approval_mode = mode
    token = set_current_invocation_context(context)
    try:
        if allowed:
            events = [event async for event in svc.invoke(req, trigger_ref=trigger())]
            assert events
            assert any(kind == "send" for kind, _ in wire.calls)
            assert decisions == [
                ("allow", None), ("allow", "hosted_gateway"), ("allow", "hosted_gateway")
            ]
        else:
            with pytest.raises(PermissionError, match="A2A_APPROVAL_"):
                _ = [event async for event in svc.invoke(req, trigger_ref=trigger())]
            assert not wire.calls
            assert len(decisions) == 1
    finally:
        reset_current_invocation_context(token)


@pytest.mark.asyncio
async def test_runtime_policy_audit_failure_prevents_outbound():
    from ksadk.a2a.invocation_policy import runtime_a2a_invocation_policy
    from ksadk.runtime_context import (
        reset_current_invocation_context,
        session_invocation_context,
        set_current_invocation_context,
    )

    async def audit(*_args):
        raise RuntimeError("audit unavailable")

    svc, _, wire = await service(policy=runtime_a2a_invocation_policy(audit=audit))
    context = session_invocation_context({}, agent_id="caller", session_id="s")
    context.tool_approval_mode = "full"
    token = set_current_invocation_context(context)
    try:
        with pytest.raises(RuntimeError, match="audit unavailable"):
            _ = [event async for event in svc.invoke(request(), trigger_ref=trigger())]
    finally:
        reset_current_invocation_context(token)
    assert not wire.calls


@pytest.mark.asyncio
async def test_runtime_policy_requires_runtime_context_even_with_approval():
    from ksadk.a2a.invocation_policy import runtime_a2a_invocation_policy

    decisions = []
    policy = runtime_a2a_invocation_policy(
        audit=lambda _req, decision, _route: decisions.append(decision),
        approval_resolver=lambda _req: "approved",
    )
    svc, _, wire = await service(policy=policy)
    with pytest.raises(PermissionError, match="A2A_RUNTIME_CONTEXT_REQUIRED"):
        _ = [event async for event in svc.invoke(request(), trigger_ref=trigger())]
    assert not wire.calls
    assert decisions[0].action == "deny"


def trigger():
    return ScopeTriggerRef(scope_id="root", item_id="root-call-item", call_id="call_1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("platform_task_id", "a2a-task-00000000000040008000000000000002"),
        ("remote_task_id", "other-remote"),
        ("remote_context_id", "other-context"),
    ],
)
async def test_bound_handle_conflicts_are_rejected_before_network(field, value):
    svc, _, wire = await service()
    initial = request()
    async for _ in svc.invoke(initial, trigger_ref=trigger()):
        pass
    bound = svc.handle(initial.scope_id)
    conflict = A2AInvocationHandle.model_validate({**bound.model_dump(), field: value})
    before = len(wire.calls)
    with pytest.raises(ValueError, match="A2A_HANDLE_CONFLICT"):
        await svc.get(conflict)
    with pytest.raises(ValueError, match="A2A_HANDLE_CONFLICT"):
        await svc.cancel(conflict)
    assert len(wire.calls) == before
    assert svc.snapshot(initial.handle) == svc.snapshot(bound)


@pytest.mark.asyncio
async def test_terminal_only_peer_reconciles_before_child_closure_and_persists_before_yield():
    svc, store, wire = await service()
    events = []
    async for event in svc.invoke(request(), trigger_ref=trigger()):
        assert event in await store.list("s", run_id="public-run")
        events.append(event)
    assert not any(e.event_type.startswith("run.") for e in events)
    first = events[0].initial.parts[0].data
    assert first["schema"] == "execution.scope/v1"
    assert first["trigger_ref"]["item_id"] == "root-call-item"
    assert all(e.scope_id == request().scope_id for e in events)
    assert events[-1].snapshot.parts[0].data["status"] == "completed"
    assert events[-1].snapshot.parts[0].data["output_refs"]
    from ksadk.events.canonical import RunCompleted, RunStarted, SourceRef
    from ksadk.events.reducer import StreamReducer

    reducer = StreamReducer()
    root = dict(
        schema_version=2,
        timestamp=1.0,
        run_id="public-run",
        scope_id="root",
        source=SourceRef(framework="adk"),
    )
    reducer.apply(RunStarted(**root, event_id="root-start", seq=0, status="running"))
    for event in events:
        reducer.apply(event)
    assert reducer.snapshot().status == "running"
    reducer.apply(
        RunCompleted(
            **root, event_id="root-end", seq=events[-1].seq + 1, status="completed", output_refs=()
        )
    )
    assert [kind for kind, _ in wire.calls] == ["prepare", "send", "bind", "operation", "get"]
    previous = len(await store.list("s", run_id="public-run"))
    snapshot = await svc.get(svc.handle(request().scope_id))
    assert snapshot.status == "completed"
    assert len(await store.list("s", run_id="public-run")) == previous


@pytest.mark.asyncio
async def test_policy_denial_has_no_outbound_or_prepare():
    svc, store, wire = await service(policy=A2AInvocationPolicy())
    with pytest.raises(PermissionError, match="A2A_POLICY_NOT_CONFIGURED"):
        async for _ in svc.invoke(request(), trigger_ref=trigger()):
            pass
    assert not wire.calls
    assert not await store.list("s", run_id="public-run")


@pytest.mark.asyncio
async def test_message_only_peer_has_one_child_scope():
    wire = Wire(
        events=[Message(message_id="answer", role=Role.ROLE_AGENT, parts=[Part(text="answer")])]
    )
    svc, store, _ = await service(wire)
    events = [e async for e in svc.invoke(request(), trigger_ref=trigger())]
    assert events[-1].snapshot.parts[0].data["status"] == "completed"
    assert len({e.scope_id for e in events}) == 1
    assert not any(kind == "get" for kind, _ in wire.calls)


@pytest.mark.asyncio
async def test_multiple_messages_keep_distinct_items_in_one_scope():
    wire = Wire(
        events=[
            Message(message_id=f"answer-{i}", role=Role.ROLE_AGENT, parts=[Part(text=str(i))])
            for i in range(2)
        ]
    )
    svc, _, _ = await service(wire)
    events = [e async for e in svc.invoke(request(), trigger_ref=trigger())]
    messages = [e for e in events if e.event_type == "item.completed" and e.item_kind == "message"]
    assert len({e.item_id for e in messages}) == 2
    assert len({e.scope_id for e in events}) == 1


@pytest.mark.asyncio
async def test_continue_hashes_bound_wire_and_duplicate_replays_after_completion():
    from ksadk.a2a.invocation_types import A2AContinuationRequest
    from ksadk.a2a.message_digest import canonical_message_sha256

    initial = request()
    pending = Task(
        id="remote-1",
        context_id="context-1",
        status=TaskStatus(
            state=TaskState.TASK_STATE_INPUT_REQUIRED,
            message=Message(
                message_id="question", role=Role.ROLE_AGENT, parts=[Part(text="next?")]
            ),
        ),
    )
    svc, _, wire = await service(Wire(events=[pending], task=pending))
    events = [e async for e in svc.invoke(initial, trigger_ref=trigger())]
    assert events[-1].snapshot.parts[0].data["status"] == "input_required"
    followup = A2AContinuationRequest(
        spec=initial.spec,
        handle=svc.handle(initial.scope_id),
        message=A2AInvocationMessage(message_id="followup", text_parts=("continue",)),
        context=initial.context,
        deadline_unix_seconds=time.time() + 60,
        idempotency_token="followup-idem",
    )
    wire.task = Task(
        id="remote-1",
        context_id="context-1",
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
    )
    wire.events = [wire.task]
    events = [e async for e in svc.continue_task(followup)]
    send = [value for kind, value in wire.calls if kind == "send"][-1]
    permit = [
        value
        for kind, value in wire.calls
        if kind == "operation" and value["operation"] == "send_message"
    ][-1]
    assert send.task_id == "remote-1" and send.context_id == "context-1"
    assert permit["message_sha256"] == canonical_message_sha256(send)
    assert events[-1].snapshot.parts[0].data["status"] == "completed"
    count = len(wire.calls)
    replayed = [e async for e in svc.continue_task(followup)]
    assert replayed and replayed[-1].snapshot.parts[0].data["status"] == "completed"
    assert len(wire.calls) == count
    changed = followup.model_copy(
        update={"message": A2AInvocationMessage(message_id="followup", text_parts=("changed",))}
    )
    with pytest.raises(ValueError, match="A2A_IDEMPOTENCY_CONFLICT"):
        _ = [e async for e in svc.continue_task(changed)]


@pytest.mark.asyncio
async def test_managed_native_events_survive_real_runner_without_second_projection():
    from types import SimpleNamespace

    from ksadk.a2a.adk import bind_managed_a2a, remote_agent
    from tests.runners.test_adk_multi_llm_runtime_events import runner_for

    req = request()
    svc, store, _ = await service()
    with bind_managed_a2a(svc, lambda ctx, spec: (req, trigger())):
        native = [
            e
            async for e in remote_agent(req.spec)._run_async_impl(
                SimpleNamespace(invocation_id="native", branch=None)
            )
        ]
    stored = await store.list("s", run_id=req.handle.run_id)
    output = [
        e
        async for e in runner_for(native).stream_canonical_events(
            {"input": "hello", "run_id": req.handle.run_id}
        )
    ]
    child = [e for e in output if not e.event_type.startswith("run.")]
    assert child == stored
    assert output[-1].event_type == "run.completed"
    assert output[-1].seq > max(e.seq for e in stored)


@pytest.mark.asyncio
async def test_send_failure_is_unknown_and_never_resubmitted():
    class Broken(Wire):
        @asynccontextmanager
        async def open_prepared_message_stream(self, prepared, message):
            self.calls.append(("send", message))
            raise ConnectionError("private transport detail")
            yield

    svc, store, wire = await service(Broken())
    req = request()
    with pytest.raises(ConnectionError):
        _ = [e async for e in svc.invoke(req, trigger_ref=trigger())]
    snapshot = svc.snapshot(svc.handle(req.scope_id))
    assert snapshot.status == "unknown" and snapshot.error.code == "A2A_SEND_OUTCOME_UNKNOWN"
    assert "private" not in snapshot.model_dump_json()
    facts = await store.list(req.handle.session_id, run_id=req.handle.run_id)
    public = "\n".join(e.model_dump_json() for e in facts)
    assert "A2A_SEND_OUTCOME_UNKNOWN" in public
    assert "private transport detail" not in public
    descriptor = facts[-1].snapshot.parts[0].data
    assert descriptor["status"] == "failed"
    replayed = [e async for e in svc.invoke(req, trigger_ref=trigger())]
    assert replayed == facts
    assert len([x for x in wire.calls if x[0] == "send"]) == 1


@pytest.mark.asyncio
async def test_native_factory_context_binding_and_tool_exposure_guard():
    from types import SimpleNamespace

    from ksadk.a2a.adk import bind_managed_a2a, canonical_event_from_adk, remote_agent
    from ksadk.a2a.exposure import compile_exposure_plan
    from ksadk.toolsets.a2a import bind_managed_a2a_tools, call_a2a_agent

    req = request()
    agent = remote_agent(req.spec)
    svc, store, wire = await service()
    assert not wire.calls
    with bind_managed_a2a(svc, lambda ctx, spec: (req, trigger())):
        events = [
            e
            async for e in agent._run_async_impl(
                SimpleNamespace(invocation_id="native", branch=None)
            )
        ]
    stored = await store.list("s", run_id="public-run")
    assert all(canonical_event_from_adk(e) in stored for e in events)
    plan = compile_exposure_plan([req.spec], framework="adk", consumer_id="root")
    count = len(wire.calls)
    with bind_managed_a2a_tools(svc, lambda *_: (req, trigger()), plan):
        result = await call_a2a_agent("expert", "hello")
    assert result["error_type"] == "A2A_EXPOSURE_CONFLICT"
    assert len(wire.calls) == count


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "unknown", "input_required"])
async def test_generic_tool_only_reports_confirmed_completion_as_success(outcome):
    from ksadk.a2a.exposure import compile_exposure_plan
    from ksadk.toolsets.a2a import bind_managed_a2a_tools, call_a2a_agent

    wire = Wire(events=[] if outcome == "unknown" else None)
    if outcome == "input_required":
        wire.task.status.state = TaskState.TASK_STATE_INPUT_REQUIRED
        wire.task.status.message.CopyFrom(
            Message(message_id="question", role=Role.ROLE_AGENT, parts=[Part(text="Provide input")])
        )
    svc, _, wire = await service(wire)
    req = request()
    plan = compile_exposure_plan([], framework="langgraph", consumer_id="root")
    with bind_managed_a2a_tools(svc, lambda *_: (req, trigger()), plan):
        result = await call_a2a_agent("expert", "hello")
    assert result["ok"] is (outcome == "completed")
    assert result.get("state") == outcome, result
    assert result["reply"] == ("hello" if outcome == "completed" else "")
    assert sum(kind == "send" for kind, _ in wire.calls) == 1


@pytest.mark.asyncio
async def test_completed_invocation_replays_original_facts_without_sending_again():
    svc, store, wire = await service()
    req = request()
    original = [event async for event in svc.invoke(req, trigger_ref=trigger())]
    before = await store.list("s", run_id=req.handle.run_id)
    replayed = [event async for event in svc.invoke(req, trigger_ref=trigger())]
    assert replayed == original
    assert await store.list("s", run_id=req.handle.run_id) == before
    assert sum(kind == "send" for kind, _ in wire.calls) == 1


@pytest.mark.asyncio
async def test_generic_tool_retry_returns_the_same_durable_result():
    from ksadk.a2a.exposure import compile_exposure_plan
    from ksadk.toolsets.a2a import bind_managed_a2a_tools, call_a2a_agent

    svc, _, wire = await service()
    req = request()
    plan = compile_exposure_plan([], framework="langgraph", consumer_id="root")
    with bind_managed_a2a_tools(svc, lambda *_: (req, trigger()), plan):
        original = await call_a2a_agent("expert", "hello")
        replayed = await call_a2a_agent("expert", "hello")
    assert replayed == original
    assert original["reply"] == "hello"
    assert sum(kind == "send" for kind, _ in wire.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["descriptor", "output"])
async def test_terminal_replay_rejects_missing_facts_without_resending(monkeypatch, missing):
    svc, store, wire = await service()
    req = request()
    _ = [event async for event in svc.invoke(req, trigger_ref=trigger())]
    original_list = store.list

    async def incomplete(*args, **kwargs):
        return [
            event
            for event in await original_list(*args, **kwargs)
            if not (
                event.event_type == "item.completed"
                and (
                    event.item_kind == "status"
                    if missing == "descriptor"
                    else event.item_kind in {"message", "artifact"}
                )
            )
        ]

    monkeypatch.setattr(store, "list", incomplete)
    with pytest.raises(ValueError, match="A2A_TERMINAL_RECOVERY_FACT_REQUIRED"):
        _ = [event async for event in svc.invoke(req, trigger_ref=trigger())]
    assert sum(kind == "send" for kind, _ in wire.calls) == 1


@pytest.mark.asyncio
async def test_unknown_bound_result_reconciles_without_reopening_failed_local_scope():
    class BrokenAfterBind(Wire):
        @asynccontextmanager
        async def open_prepared_message_stream(self, prepared, message):
            self.calls.append(("send", message))

            async def stream():
                working = Task()
                working.CopyFrom(self.task)
                working.status.state = TaskState.TASK_STATE_WORKING
                working.artifacts.clear()
                yield working
                raise ConnectionError("private transport detail")

            yield stream()

    svc, store, wire = await service(BrokenAfterBind())
    req = request()
    with pytest.raises(ConnectionError):
        _ = [e async for e in svc.invoke(req, trigger_ref=trigger())]
    handle = svc.handle(req.scope_id)
    assert handle.remote_task_id == "remote-1"
    assert svc.snapshot(handle).status == "unknown"
    wire.task.status.state = TaskState.TASK_STATE_WORKING
    assert (await svc.get(handle)).status == "working"
    wire.task.status.state = TaskState.TASK_STATE_COMPLETED
    reconciled = await svc.get(handle)
    assert reconciled.status == "completed"
    facts = await store.list(req.handle.session_id, run_id=req.handle.run_id)
    descriptors = [
        p.data
        for e in facts
        for p in getattr(getattr(e, "snapshot", None), "parts", ())
        if getattr(p, "data", {}).get("schema") == "execution.scope/v1"
    ]
    assert descriptors[-1]["status"] == "failed"
    assert len([c for c in wire.calls if c[0] == "send"]) == 1


@pytest.mark.asyncio
async def test_timeout_cleanup_preserves_unknown_remote_outcome_and_frozen_scope():
    class Broken(Wire):
        @asynccontextmanager
        async def open_prepared_message_stream(self, prepared, message):
            self.calls.append(("send",message))
            raise TimeoutError("A2A_CONNECT_TIMEOUT")
            yield

    svc,store,wire=await service(Broken())
    req=request()
    with pytest.raises(TimeoutError,match="A2A_CONNECT_TIMEOUT"):
        _=[event async for event in svc.invoke(req,trigger_ref=trigger())]
    before=await store.list(req.handle.session_id,run_id=req.handle.run_id)
    result=await svc.cancel(svc.handle(req.scope_id))
    assert result.outcome=="cancel_requested_but_remote_unknown"
    assert result.snapshot.status=="unknown"
    assert await store.list(req.handle.session_id,run_id=req.handle.run_id)==before
    assert len([entry for entry in wire.calls if entry[0]=='send'])==1
