from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from ag_ui.core import RunAgentInput
from fastapi import HTTPException

from ksadk.agui import hosted
from ksadk.agui.agent import KsadkAGUIAgent
from ksadk.kernel.contracts import AgentControlCommand
from ksadk.runtime_context import PlatformIdentityContext


def input(**updates):
    return RunAgentInput.model_validate(
        {
            "threadId": "session",
            "runId": "wire-run",
            "messages": [{"id": "user", "role": "user", "content": "hello"}],
            "tools": [
                {"name": "display", "description": "Show a page", "parameters": {"type": "object"}}
            ],
            "context": [{"description": "business", "value": "context"}],
            **updates,
        }
    )


@pytest.fixture
def state(monkeypatch):
    actor = PlatformIdentityContext.from_payload(
        {
            "identity_namespace": "test",
            "tenant_id": "business",
            "subject_type": "user",
            "subject_id": "alice",
        }
    )
    store = SimpleNamespace(load_message=AsyncMock(), read_public_interaction=AsyncMock(),
                            load_by_idempotency=AsyncMock(return_value=None))
    events = SimpleNamespace(subscribe=AsyncMock())
    runtime = SimpleNamespace(
        config=SimpleNamespace(tenant_id="storage-partition", agent_instance_id="instance"),
        kernel_store=store,
        session_events=events,
    )
    monkeypatch.setattr(hosted, "hosted_runtime", lambda: runtime)
    agent = SimpleNamespace(
        _identity=actor,
        _authorize_session=AsyncMock(),
        _session=SimpleNamespace(agent_id="agent"),
        _json_safe=KsadkAGUIAgent._json_safe,
        _latest_user_input=KsadkAGUIAgent._latest_user_input,
        _request_config=KsadkAGUIAgent._request_config,
    )
    return agent, runtime


async def test_prepare_declares_existing_actions_without_executing_or_discovering(state):
    agent, runtime = state
    prepared = await hosted.prepare(agent, input(), tenant_id="account")
    assert prepared["schema"] == hosted.SCHEMA
    assert len(prepared["actions"]) == 1
    action = prepared["actions"][0]
    assert action["action"] == "RunAgent"
    assert action["body"]["Messages"] == [{"role": "user", "content": "hello"}]
    assert action["body"]["Background"] is True
    options = action["body"]["Extensions"]["ksadk.agui"]
    assert options["request_digest"] == hosted.request_digest(input())
    assert options["options"]["tools"][0]["name"] == "display"
    assert options["options"]["context"][0]["value"] == "context"
    assert action["body"]["Extensions"]["mcp.authorization"] == hosted.CAPABILITY
    runtime.kernel_store.load_message.assert_not_awaited()


@pytest.mark.parametrize("tamper", ["owner", "tenant", "session", "instance", "command", "digest"])
async def test_projection_requires_matching_server_admitted_command(state, tamper):
    agent, runtime = state
    request = input()
    command_id = str(uuid4())
    command = AgentControlCommand(
        command_id=command_id,
        idempotency_key=hosted.enqueue_idempotency(request.run_id),
        tenant_id="account",
        agent_instance_id="instance",
        session_id="session",
        command_type="enqueue",
        payload={
            "content": "hello",
            "invocation_identity": agent._identity.to_payload(),
            "runtime_options": {"ag-ui": {"request_digest": hosted.request_digest(request)}},
        },
        source={"kind": "agui", "ref": "request"},
        authorization_ref="server-admitted-permit",
        submitted_at="2026-11-17T00:00:00Z",
    )
    if tamper == "owner":
        command.payload["invocation_identity"]["subject_id"] = "bob"
    elif tamper == "digest":
        command.payload["runtime_options"]["ag-ui"]["request_digest"] = "0" * 64
    else:
        command = command.model_copy(
            update={
                {
                    "tenant": "tenant_id",
                    "session": "session_id",
                    "instance": "agent_instance_id",
                    "command": "command_id",
                }[tamper]: uuid4() if tamper == "command" else "other"
            }
        )
    runtime.kernel_store.load_message.return_value = SimpleNamespace(command=command)
    admission = {
        "schema": hosted.SCHEMA,
        "tenant_id": "account",
        "request_digest": hosted.request_digest(request),
        "receipts": [{"CommandId": command_id, "MessageId": str(uuid4())}],
    }
    with pytest.raises(HTTPException) as error:
        await hosted.observe(agent, request, admission)
    assert error.value.status_code == 403
    runtime.kernel_store.load_message.assert_awaited_once()
    runtime.session_events.subscribe.assert_not_called()


@pytest.mark.parametrize("answer", [False, 0, "", [], {"nested": [False, 0]}])
async def test_prepare_preserves_structured_answers_and_original_run(state, answer):
    agent, runtime = state
    runtime.kernel_store.read_public_interaction.return_value = {
        "interaction_id": "wait",
        "run_id": "original-run",
        "revision": 3,
        "status": "pending",
        "kind": "structured_input",
        "request_schema": {},
    }
    request = input(resume=[{"interruptId": "wait", "status": "resolved", "payload": answer}])
    prepared = await hosted.prepare(agent, request, tenant_id="account")
    action = prepared["actions"][0]
    assert action["action"] == "SubmitInteraction"
    assert action["body"]["RunId"] == "original-run"
    assert action["body"]["Response"] == answer
    assert action["body"]["ExpectedRevision"] == 3


async def test_prepare_cannot_complete_an_oauth_wait_as_an_ordinary_interaction(state):
    agent, runtime = state
    runtime.kernel_store.read_public_interaction.return_value = None
    with pytest.raises(HTTPException) as error:
        await hosted.prepare(
            agent,
            input(resume=[{"interruptId": "oauth-wait", "status": "resolved", "payload": True}]),
            tenant_id="account",
        )
    assert error.value.status_code == 404


async def test_projection_uses_durable_receipt_cursor_without_submitting_another_command(state):
    agent, runtime = state
    request = input()
    command = AgentControlCommand(
        command_id=uuid4(),
        idempotency_key=hosted.enqueue_idempotency(request.run_id),
        tenant_id="account",
        agent_instance_id="instance",
        session_id="session",
        command_type="enqueue",
        payload={
            "content": "hello",
            "invocation_identity": agent._identity.to_payload(),
            "runtime_options": {"ag-ui": {"request_digest": hosted.request_digest(request)}},
        },
        source={"kind": "agui", "ref": "request"},
        authorization_ref="server-permit",
        submitted_at="2026-11-17T00:00:00Z",
    )
    runtime.kernel_store.load_message.return_value = SimpleNamespace(
        command=command, accepted_seq=17
    )
    cursors = []
    envelope = SimpleNamespace(session_id="session", seq=18)

    async def subscribe(session, cursor):
        cursors.append((session, cursor))
        yield envelope

    runtime.session_events.subscribe = subscribe
    admission = {
        "schema": hosted.SCHEMA,
        "tenant_id": "account",
        "request_digest": hosted.request_digest(request),
        "receipts": [{"CommandId": str(command.command_id), "MessageId": str(uuid4())}],
    }
    for _ in range(2):
        stream = await hosted.observe(agent, request, admission)
        assert [event async for event in stream.events] == [(18, envelope)]
        assert stream.target_run == hosted.execution_grant_run_id(command)
    assert cursors == [("session", 17)] * 2


async def test_unsupported_protocol_does_not_admit_a_task(state):
    agent, runtime = state
    with pytest.raises(HTTPException) as error:
        await hosted.prepare(agent, input(protocolVersion="unknown"), tenant_id="account")
    assert error.value.status_code == 422
    runtime.kernel_store.load_message.assert_not_awaited()

def answer_command(agent, request, entry, *, revision=3):
    return AgentControlCommand(
        command_id=uuid4(),
        idempotency_key=hosted.resume_idempotency(request.run_id, entry.interrupt_id),
        tenant_id="account", agent_instance_id="instance", session_id="session",
        command_type="submit_interaction",
        payload={"run_id": "original-run", "interaction_id": entry.interrupt_id,
                 "expected_revision": revision, "action": "submit", "response": entry.payload,
                 "token_ref": "permit",
                 "invocation_identity": agent._identity.to_payload()},
        source={"kind": "studio", "ref": "request"}, authorization_ref="permit",
        submitted_at="2026-11-17T00:00:00Z",
    )


def partial_batch(runtime):
    request = input(resume=[
        {"interruptId": key, "status": "resolved", "payload": {"answer": key}}
        for key in ("first", "second")
    ])
    records = [
        {"interaction_id": key, "run_id": "original-run", "revision": revision,
         "status": status, "kind": "structured_input", "request_schema": {},
         "request": {"schema": {}, "presentation": {}}}
        for key, status, revision in (("first", "resolved", 4), ("second", "pending", 3))
    ]
    runtime.kernel_store.read_public_interaction.side_effect = records
    return request


async def test_partial_batch_retries_original_command_and_revision(state):
    agent, runtime = state
    request = partial_batch(runtime)
    command = answer_command(agent, request, request.resume[0])
    runtime.kernel_store.load_by_idempotency.side_effect = [
        SimpleNamespace(command=command), None,
    ]
    result = await hosted.prepare(agent, request, tenant_id="account")
    assert len(result["actions"]) == 2
    first, second = [item["body"] for item in result["actions"]]
    assert first["ExpectedRevision"] == 3
    assert first["IdempotencyKey"] == command.idempotency_key
    assert first["Response"] == command.payload["response"]
    assert second["InteractionId"] == "second"
    assert second["RunId"] == "original-run"


@pytest.mark.parametrize("tamper", ["answer", "owner", "missing"])
async def test_partial_batch_does_not_reuse_another_submission(state, tamper):
    agent, runtime = state
    request = partial_batch(runtime)
    command = answer_command(agent, request, request.resume[0])
    if tamper == "answer":
        command.payload["response"] = {"answer": "different"}
    if tamper == "owner":
        command.payload["invocation_identity"] = {}
    runtime.kernel_store.load_by_idempotency.return_value = (
        None if tamper == "missing" else SimpleNamespace(command=command)
    )
    with pytest.raises(HTTPException) as error:
        await hosted.prepare(agent, request, tenant_id="account")
    assert error.value.status_code == 409


async def test_observe_accepts_batch_consumed_between_submission_and_subscription(state):
    agent, runtime = state
    request = partial_batch(runtime)
    messages = [
        SimpleNamespace(command=answer_command(agent, request, entry), accepted_seq=10 + index)
        for index, entry in enumerate(request.resume)
    ]
    runtime.kernel_store.load_message.side_effect = messages
    stream = await hosted.observe(agent, request, {
        "schema": hosted.SCHEMA, "tenant_id": "account",
        "request_digest": hosted.request_digest(request),
        "receipts": [{"CommandId": str(m.command.command_id), "MessageId": str(uuid4())}
                     for m in messages],
    })
    assert stream.target_run == "original-run"
    assert stream.unconsumed == {"first", "second"}
    assert stream.command_ids == {str(m.command.command_id) for m in messages}


@pytest.mark.parametrize("readonly", [False, True])
async def test_individual_answer_and_retry_return_the_remaining_form(state, readonly):
    from ksadk.kernel.store import control_event

    agent, runtime = state
    request = input(resume=[
        {"interruptId": "first", "status": "resolved", "payload": {"answer": "first"}},
    ])
    records = {
        key: {"interaction_id": key, "run_id": "original-run", "revision": revision,
              "status": status, "kind": "structured_input", "request_schema": {},
              "request": {"request_schema": {}, "presentation": {"title": key}}}
        for key, status, revision in (("first", "resolved", 4), ("second", "pending", 3))
    }

    async def read(key, **scope):
        assert scope == {"tenant_id": "account", "agent_instance_id": "instance",
                         "session_id": "session"}
        return records.get(key)

    runtime.kernel_store.read_public_interaction.side_effect = read
    command = answer_command(agent, request, request.resume[0])
    runtime.kernel_store.load_message.return_value = SimpleNamespace(
        command=command, accepted_seq=10,
    )
    runtime.kernel_store.load_by_idempotency.return_value = SimpleNamespace(command=command)
    prepared = await hosted.prepare(agent, request, tenant_id="account")
    assert prepared["actions"] == []  # Already consumed: a retry is read-only.

    async def subscribe(session, cursor):
        assert session == "session" and cursor == (0 if readonly else 10)
        if not readonly:
            yield control_event(session_id=session, run_id="original-run",
                event_type="interaction.resolved", payload={"interaction_id": "first"}
            ).model_copy(update={"family": "interaction", "seq": 11})
        # The second request preceded the command cursor. The barrier carries
        # only its id. A read-only replay may also contain the old first id.
        yield control_event(session_id=session, run_id="original-run",
            event_type="control.interaction_wait_ready",
            payload={"interaction_ids": ["first", "second"] if readonly else ["second"]},
        ).model_copy(update={"seq": 12})
        pytest.fail("The observer must return the remaining form, not wait for another answer")

    runtime.session_events.subscribe = subscribe
    projector = object.__new__(KsadkAGUIAgent)
    projector._identity = agent._identity
    projector._authorize_session = agent._authorize_session
    events = [event async for event in projector._kernel_run(request, admission={
        "schema": hosted.SCHEMA, "tenant_id": "account",
        "request_digest": hosted.request_digest(request),
        "receipts": [] if readonly else [
            {"CommandId": str(command.command_id), "MessageId": str(uuid4())},
        ],
    })]
    assert events[-1].type == "RUN_FINISHED"
    assert events[-1].outcome.type == "interrupt"
    assert [entry.id for entry in events[-1].outcome.interrupts] == ["second"]
    assert events[-1].outcome.interrupts[0].message == "second"


@pytest.mark.parametrize("readonly,status,expected", [
    (False, "resolved", ["form"]),  # Another request won after this activation's barrier.
    (True, "resolved", []),
    (True, "cancelled", []),
    (True, "expired", []),
    (True, "pending", ["form"]),
])
async def test_wait_barrier_preserves_activation_and_excludes_foreign_and_oauth_waits(
    readonly, status, expected,
):
    from ksadk.agui.hosted import KernelStream

    async def read(key):
        if key == "oauth":
            return None
        return {"interaction_id": key, "run_id": "other" if key == "foreign" else "run",
                "revision": 1, "status": status, "kind": "structured_input",
                "request": {"request_schema": {}, "presentation": {}}}

    stream = KernelStream("run", SimpleNamespace(pending={}), None, read, readonly=readonly)
    agent = object.__new__(KsadkAGUIAgent)
    interrupts = await agent._kernel_barrier_interrupts(stream, ["oauth", "foreign", "form"])
    assert [entry.id for entry in interrupts] == expected


async def test_readonly_observer_checks_session_owner_without_receipts(state):
    agent, runtime = state
    agent._authorize_session = AsyncMock(side_effect=HTTPException(404, 'Session not found'))
    request = input(resume=[{'interruptId':'i','status':'resolved','payload':True}])
    runtime.kernel_store.read_public_interaction.return_value = {
        'run_id':'private-run','status':'resolved','kind':'structured_input','request_schema':{}, 'revision':2}
    with pytest.raises(HTTPException, match='Session not found'):
        await hosted.observe(agent, request, {'schema':hosted.SCHEMA,'tenant_id':'account',
            'request_digest':hosted.request_digest(request),'receipts':[]})
    agent._authorize_session.assert_awaited_once()


async def test_expected_admission_denial_has_actionable_error_without_error_log(monkeypatch, caplog):
    async def denied(*args):
        raise HTTPException(403, 'AG-UI command owner mismatch')
    monkeypatch.setattr(hosted, 'observe', denied)
    agent = object.__new__(KsadkAGUIAgent)
    result = [event async for event in agent._kernel_run(input(), admission={})]
    assert result[-1].code == 'AGUI_ADMISSION_403'
    assert result[-1].message == 'AG-UI command owner mismatch'
    assert not [r for r in caplog.records if r.levelname == 'ERROR']
