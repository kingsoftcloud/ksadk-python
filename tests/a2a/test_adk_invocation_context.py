from types import SimpleNamespace

import pytest
from google.adk.events import Event, EventActions
from google.genai import types

from ksadk.a2a.adk_invocation_context import native_handoff_trigger
from ksadk.events.adapters.adk import ADKAdapterContext, ADKEventAdapter
from tests.a2a.test_invocation_service_r85 import request


def pair(branch=None, suffix="1"):
    call = Event(
        id="call-event-" + suffix,
        invocation_id="native-1",
        author="root",
        branch=branch,
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        id="call_1", name="transfer_to_agent", args={"agent_name": "expert"}
                    )
                )
            ],
        ),
    )
    response = Event(
        id="response-" + suffix,
        invocation_id="native-1",
        author="root",
        branch=branch,
        actions=EventActions(transfer_to_agent="expert"),
        content=types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id="call_1", name="transfer_to_agent", response={}
                    )
                )
            ],
        ),
    )
    return call, response


def test_handoff_matches_actual_mapper_identity_and_ignores_other_branch():
    call, response = pair()
    other = pair("parallel", "2")
    ctx = SimpleNamespace(
        invocation_id="native-1",
        branch=None,
        session=SimpleNamespace(events=[call, response, *other]),
    )
    trigger = native_handoff_trigger(ctx, request().spec)
    mapped = ADKEventAdapter().map(call, ADKAdapterContext(run_id="public-run"))
    tool = next(event for event in mapped if event.item_kind == "tool_call")
    assert trigger.scope_id == tool.scope_id
    assert trigger.item_id == tool.item_id
    assert trigger.call_id == "call_1"
    ctx.branch = "parallel"
    assert native_handoff_trigger(ctx, request().spec) != trigger


def test_prior_handoff_cannot_be_reused_after_another_event():
    call, response = pair()
    newer = Event(id="later", invocation_id="native-1", author="root")
    ctx = SimpleNamespace(
        invocation_id="native-1",
        branch=None,
        session=SimpleNamespace(events=[call, response, newer]),
    )
    assert native_handoff_trigger(ctx, request().spec) is None


def test_missing_native_call_does_not_fabricate_trigger():
    _, response = pair()
    ctx = SimpleNamespace(
        invocation_id="native-1", branch=None, session=SimpleNamespace(events=[response])
    )
    with pytest.raises(ValueError, match="A2A_NATIVE_TRIGGER_CALL_MISSING"):
        native_handoff_trigger(ctx, request().spec)


@pytest.mark.asyncio
async def test_static_workflow_trigger_is_persisted_and_replayed_once():
    from google.adk.agents import SequentialAgent

    from ksadk.a2a.adk import remote_agent
    from ksadk.a2a.adk_invocation_context import ADKWorkflowTrigger
    from ksadk.events.run_authority import RunEventAuthority, bind_run_event_authority
    from ksadk.events.session_event import SessionServiceEventStore
    from ksadk.kernel.contracts import ActivationWriteGuard
    from ksadk.sessions.in_memory import InMemorySessionService

    sessions = InMemorySessionService()
    await sessions.create_session("caller", "user", "s")
    authority = RunEventAuthority(
        SessionServiceEventStore(sessions),
        session_id="s",
        run_id="public-run",
        guard=ActivationWriteGuard(activation_id="test", fencing_token=1),
    )
    agent = remote_agent(request().spec)
    root = SequentialAgent(name="workflow", sub_agents=[agent])
    assert agent.parent_agent is root
    ctx = SimpleNamespace(agent=agent, invocation_id="native-1", branch=None)
    with bind_run_event_authority(authority):
        selector = ADKWorkflowTrigger(authority, clock=lambda: 1.0)
        trigger, prefix = await selector(ctx, request().spec)
        assert [event.event_type for event in prefix] == ["item.started", "item.completed"]
        assert {event.item_id for event in prefix} == {trigger.item_id}
        assert prefix[1].snapshot.parts[0].call_id == trigger.call_id
        restored = await ADKWorkflowTrigger(authority, clock=lambda: 999.0)(ctx, request().spec)
        assert restored == (trigger, prefix)
        assert len(await authority.list("s")) == 2
        from ksadk.a2a.adk import bind_managed_a2a, canonical_event_from_adk
        from ksadk.a2a.adk_invocation_context import ADKInvocationContextProvider
        from ksadk.a2a.invocation_policy import A2AInvocationPolicy, A2AInvocationPolicyDecision
        from ksadk.a2a.invocation_service import A2AInvocationService
        from tests.a2a.test_invocation_service_r85 import Wire

        wire = Wire()
        service = A2AInvocationService(
            space_client=wire,
            event_store=authority,
            policy=A2AInvocationPolicy(evaluator=lambda *_: A2AInvocationPolicyDecision("allow")),
        )
        provider = ADKInvocationContextProvider(
            authority=authority,
            service_for_space=lambda _: service,
            caller_agent_id="caller",
            message_selector=lambda *_: ("workflow input",),
        )
        ctx.session = SimpleNamespace(events=[])
        with bind_managed_a2a(None, provider):
            native = [event async for event in agent._run_async_impl(ctx)]
        assert tuple(canonical_event_from_adk(event) for event in native[:2]) == prefix
        assert sum(kind == "send" for kind, _ in wire.calls) == 1
        ctx.invocation_id = "native-2"
        other, _ = await selector(ctx, request().spec)
        assert other != trigger


@pytest.mark.asyncio
async def test_provider_recovers_original_request_and_selects_declared_space():
    from ksadk.a2a.adk import remote_agent
    from ksadk.a2a.adk_invocation_context import ADKInvocationContextProvider
    from ksadk.a2a.invocation_policy import (
        A2AApprovalRequired,
        A2AInvocationPolicy,
        A2AInvocationPolicyDecision,
    )
    from ksadk.a2a.invocation_service import A2AInvocationService
    from ksadk.events.run_authority import RunEventAuthority, bind_run_event_authority
    from ksadk.events.session_event import SessionServiceEventStore
    from ksadk.kernel.contracts import ActivationWriteGuard
    from ksadk.sessions.in_memory import InMemorySessionService
    from tests.a2a.test_invocation_service_r85 import Wire

    sessions = InMemorySessionService()
    await sessions.create_session("caller", "user", "s")
    authority = RunEventAuthority(
        SessionServiceEventStore(sessions),
        session_id="s",
        run_id="public-run",
        guard=ActivationWriteGuard(activation_id="test", fencing_token=1),
    )
    decisions, selected = ["approval_required"], []
    wire = Wire()
    service = A2AInvocationService(
        space_client=wire,
        event_store=authority,
        policy=A2AInvocationPolicy(evaluator=lambda *_: A2AInvocationPolicyDecision(decisions[0])),
    )

    def service_for_space(space):
        selected.append(space)
        return service

    ctx = SimpleNamespace(
        invocation_id="native-1", branch=None, session=SimpleNamespace(events=list(pair()))
    )
    provider = ADKInvocationContextProvider(
        authority=authority,
        service_for_space=service_for_space,
        caller_agent_id="caller",
        message_selector=lambda *_: ("original input",),
    )
    with bind_run_event_authority(authority):
        resolved = await provider(ctx, request().spec)
        with pytest.raises(A2AApprovalRequired):
            _ = [
                event
                async for event in service.invoke(resolved.request, trigger_ref=resolved.trigger)
            ]
        cold_provider = ADKInvocationContextProvider(
            authority=authority,
            service_for_space=service_for_space,
            caller_agent_id="caller",
            message_selector=lambda *_: pytest.fail("must recover original message"),
            clock=lambda: 9999999999.0,
        )
        recovered = await cold_provider(ctx, request().spec)
        assert recovered.request == resolved.request
        assert selected == [request().spec.space_id] * 2
        decisions[0] = "allow"
        agent = remote_agent(request().spec, invocation_context_provider=cold_provider)
        output = [event async for event in agent._run_async_impl(ctx)]
        assert output
        assert sum(kind == "send" for kind, _ in wire.calls) == 1
        another = SimpleNamespace(
            invocation_id="native-1",
            branch=None,
            session=SimpleNamespace(events=list(pair(suffix="new-occurrence"))),
        )
        fresh = await provider(another, request().spec)
        assert fresh.request.scope_id != resolved.request.scope_id
