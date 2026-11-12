import pytest


from ksadk.a2a.runtime_approval import (
    KernelA2AApprovalResolver,
    approval_event,
    approval_id,
    approval_target,
    bound_approval_request,
)
from tests.a2a.test_invocation_service_r85 import request
from tests.interaction.test_ledger_conformance import (
    AGENT,
    TENANT,
    acquire_guard,
    make_record,
    make_submission,
    memory_backend,  # noqa: F401
    sqlite_backend,  # noqa: F401
)

def test_managed_approval_preview_identifies_target_and_message_without_private_context():
    import json
    from ksadk.kernel.approval_presentation import approval_presentation

    req = request()
    req = req.model_copy(update={"message": req.message.model_copy(update={
        "text_parts": ("Review the official documentation",),
        "data_parts": ({"api_key": "fixture-private-secret", "decision": "approve"},),
    })})
    event = approval_event(req, timestamp=1.0)
    preview = approval_presentation(event.request)
    detail = json.loads(preview.description)
    assert preview.title == req.spec.local_name
    assert detail["arguments"]["target_agent_id"] == req.spec.target_agent_id
    assert detail["arguments"]["version_id"] == req.spec.expected_version_id
    assert detail["arguments"]["text_parts"] == ["Review the official documentation"]
    assert detail["arguments"]["data_parts"][0]["api_key"] == "[REDACTED]"
    assert detail["risk"] == "high"
    assert "fixture-private-secret" not in preview.description
    assert "audit_id" not in preview.description
    assert bound_approval_request(event, run_id=req.handle.run_id,
                                  session_id=req.handle.session_id) == req


@pytest.fixture(params=["memory_backend", "sqlite_backend"])
def backend(request):
    return request.getfixturevalue(request.param)


@pytest.mark.asyncio
@pytest.mark.parametrize("action,expected", [("approve", "approved"), ("reject", "denied")])
async def test_resolves_only_exact_durable_invocation(backend, action, expected):
    store, events = backend
    from tests.a2a.test_invocation_service_r85 import request as invocation_request

    req = invocation_request()
    req = req.model_copy(
        update={"handle": req.handle.model_copy(update={"session_id": "s1", "run_id": "run-1"})}
    )
    resolver = KernelA2AApprovalResolver(store, events, tenant_id=TENANT, agent_instance_id=AGENT)
    assert await resolver(req) is None
    guard = await acquire_guard(store)
    record = make_record(approval_id(req)).model_copy(
        update={"provider_id": "managed-a2a", "native_target": approval_target(req)}
    )
    await store.request(record, guard=guard)
    assert await resolver(req) is None
    await store.resolve(make_submission(record.interaction_id, action=action), guard=guard)
    # Fresh resolver has no process-local approval state.
    resolver = KernelA2AApprovalResolver(store, events, tenant_id=TENANT, agent_instance_id=AGENT)
    assert await resolver(req) == expected
    changed = req.model_copy(
        update={
            "message": req.message.model_copy(update={"text_parts": ("different instruction",)})
        }
    )
    assert await resolver(changed) is None
    other_call = req.model_copy(
        update={"handle": req.handle.model_copy(update={"call_id": "another-call"})}
    )
    assert await resolver(other_call) is None
    claimed = other_call.model_copy(
        update={
            "context": req.context.model_copy(update={"approval_request_id": record.interaction_id})
        }
    )
    assert await resolver(claimed) is None
    wrong_tenant = KernelA2AApprovalResolver(
        store, events, tenant_id="another-tenant", agent_instance_id=AGENT
    )
    assert await wrong_tenant(req) is None


def test_approval_target_does_not_expose_message_or_idempotency_token():
    req = request()
    target = approval_target(req)
    assert set(target) == {"schema", "invocation_sha256"}
    assert len(target["invocation_sha256"]) == 64
    correlated = req.model_copy(
        update={"context": req.context.model_copy(update={"approval_request_id": "untrusted-hint"})}
    )
    assert approval_id(correlated) == approval_id(req)


@pytest.mark.asyncio
async def test_run_owned_policy_uses_one_fenced_log_and_expires(backend):
    from ksadk.events.run_authority import RunEventAuthority, bind_run_event_authority
    from ksadk.runtime_context import (
        reset_current_invocation_context,
        session_invocation_context,
        set_current_invocation_context,
    )

    store, events = backend
    guard = await acquire_guard(store)
    req = request()
    req = req.model_copy(
        update={"handle": req.handle.model_copy(update={"run_id": "run-1", "session_id": "s1"})}
    )
    authority = RunEventAuthority(
        events,
        session_id="s1",
        run_id="run-1",
        guard=guard,
        interaction_ledger=store,
        tenant_id=TENANT,
        agent_instance_id=AGENT,
    )
    context = session_invocation_context({}, agent_id="caller", session_id="s1")
    context.tool_approval_mode = "risk"
    token = set_current_invocation_context(context)
    try:
        with bind_run_event_authority(authority):
            policy = authority.managed_invocation_policy()
            assert (await policy.evaluate(req)).action == "approval_required"
            record = make_record(approval_id(req)).model_copy(
                update={"provider_id": "managed-a2a", "native_target": approval_target(req)}
            )
            await store.request(record, guard=guard)
            await store.resolve(make_submission(record.interaction_id), guard=guard)
            assert (await policy.evaluate(req, route_kind="hosted_gateway")).action == "allow"
            log = await events.read("s1", 0, 100)
            audited = [event for event in log if event.event_type == "control.a2a_policy_evaluated"]
            assert [event.payload["action"] for event in audited] == ["approval_required", "allow"]
            assert all(event.run_id == "run-1" for event in audited)
            assert req.idempotency_token not in str([event.payload for event in audited])
        with pytest.raises(RuntimeError, match="no longer active"):
            await policy.evaluate(req)
    finally:
        reset_current_invocation_context(token)


def test_approval_event_binding_is_private_and_scoped():
    from ksadk.events.canonical import parse_runtime_event

    req = request()
    event = approval_event(req, timestamp=1.0)
    assert bound_approval_request(event, run_id="public-run", session_id="s") == req
    wire = event.model_dump(mode="json")
    assert "_invocation" not in wire
    assert req.idempotency_token not in str(wire)
    replay = parse_runtime_event(wire)
    assert bound_approval_request(replay, run_id="public-run", session_id="s") is None
    for run, session in [("other-run", "s"), ("public-run", "other-session")]:
        with pytest.raises(ValueError, match="A2A_APPROVAL_BINDING_CONFLICT"):
            bound_approval_request(event, run_id=run, session_id=session)


@pytest.mark.asyncio
async def test_record_id_without_binding_does_not_authorize(backend):
    store, events = backend
    req = request()
    req = req.model_copy(
        update={"handle": req.handle.model_copy(update={"session_id": "s1", "run_id": "run-1"})}
    )
    guard = await acquire_guard(store)
    record = make_record(approval_id(req))
    await store.request(record, guard=guard)
    await store.resolve(make_submission(record.interaction_id), guard=guard)
    resolver = KernelA2AApprovalResolver(store, events, tenant_id=TENANT, agent_instance_id=AGENT)
    with pytest.raises(ValueError, match="A2A_APPROVAL_BINDING_CONFLICT"):
        await resolver(req)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve", "reject"])
async def test_adk_child_waits_for_durable_approval_before_sending(backend, action):
    from types import SimpleNamespace

    from ksadk.a2a.adk import canonical_event_from_adk, remote_agent
    from ksadk.a2a.invocation_policy import runtime_a2a_invocation_policy
    from ksadk.a2a.invocation_service import A2AInvocationService
    from ksadk.events.run_authority import RunEventAuthority, bind_run_event_authority
    from ksadk.runtime_context import (
        reset_current_invocation_context,
        session_invocation_context,
        set_current_invocation_context,
    )
    from tests.a2a.test_invocation_service_r85 import Wire, trigger

    store, events = backend
    req = request()
    req = req.model_copy(
        update={"handle": req.handle.model_copy(update={"session_id": "s1", "run_id": "run-1"})}
    )
    guard = await acquire_guard(store)
    authority = RunEventAuthority(events, session_id="s1", run_id="run-1", guard=guard)
    policy = runtime_a2a_invocation_policy(
        audit=lambda *_: None,
        approval_resolver=KernelA2AApprovalResolver(
            store, events, tenant_id=TENANT, agent_instance_id=AGENT
        ),
    )
    wire = Wire()
    service = A2AInvocationService(space_client=wire, event_store=authority, policy=policy)
    agent = remote_agent(
        req.spec,
        invocation_service=service,
        invocation_context_provider=lambda *_: (req, trigger()),
    )
    context = session_invocation_context({}, agent_id="caller", session_id="s1")
    context.tool_approval_mode = "risk"
    token = set_current_invocation_context(context)
    try:
        with bind_run_event_authority(authority):
            stream = agent._run_async_impl(SimpleNamespace(invocation_id="native-1", branch=None))
            try:
                approval = canonical_event_from_adk(await anext(stream))
                assert approval.interaction_id == approval_id(req)
                assert not wire.calls
                record = make_record(approval_id(req)).model_copy(
                    update={"provider_id": "managed-a2a", "native_target": approval_target(req)}
                )
                await store.request(record, guard=guard)
                receipt = await store.resolve(
                    make_submission(record.interaction_id, action=action), guard=guard
                )
                approval._waiter.deliver(receipt)
                if action == "approve":
                    output = [event async for event in stream]
                    assert output
                    assert sum(kind == "send" for kind, _ in wire.calls) == 1
                else:
                    with pytest.raises(PermissionError, match="A2A_APPROVAL_DENIED"):
                        _ = [event async for event in stream]
                    assert not wire.calls
            finally:
                await stream.aclose()
    finally:
        reset_current_invocation_context(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["close", "timeout"])
async def test_closing_approval_wait_revokes_live_delivery(ending, monkeypatch):
    from types import SimpleNamespace

    from ksadk.a2a import runtime_approval
    from ksadk.a2a.invocation_policy import A2AInvocationPolicy, A2AInvocationPolicyDecision
    from ksadk.a2a.runtime_approval import invoke_with_approval
    from tests.a2a.test_invocation_service_r85 import service, trigger

    svc, _, wire = await service(
        policy=A2AInvocationPolicy(
            evaluator=lambda *_: A2AInvocationPolicyDecision("approval_required")
        )
    )
    req = request()
    stream = invoke_with_approval(svc, req, trigger_ref=trigger())
    event = await anext(stream)
    record = make_record(approval_id(req)).model_copy(
        update={
            "run_id": req.handle.run_id,
            "session_id": req.handle.session_id,
            "native_target": approval_target(req),
            "provider_id": "managed-a2a",
        }
    )
    assert event._waiter.accepts(record)
    if ending == "timeout":
        monkeypatch.setattr(
            runtime_approval, "time", SimpleNamespace(time=lambda: req.deadline_unix_seconds + 1)
        )
        with pytest.raises(TimeoutError, match="A2A_OVERALL_TIMEOUT"):
            await anext(stream)
    else:
        await stream.aclose()
    assert not event._waiter.accepts(record)
    assert not wire.calls
