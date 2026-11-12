from dataclasses import replace
import json
from uuid import uuid4
from unittest.mock import AsyncMock

import httpx
import pytest

from ksadk.a2a.control_plane import InternalA2AControlPlaneClient, A2AControlPlaneError
from ksadk.a2a.identity import A2AIngressIdentity, A2AIngressTaskBinding
from ksadk.a2a.inbound_kernel import InboundKernelSubmission, verified_inbound_message, stream_inbound_kernel_facts, submit_verified_inbound
from ksadk.kernel.contracts import AgentControlReceipt, SessionEventEnvelope
from tests.a2a.test_control_plane import _write_token


def identity(body):
    return A2AIngressIdentity(
        account_id="account", tenant_id="tenant", caller_principal_type="runtime",
        caller_principal_id="caller", caller_runtime_id="runtime-caller",
        target_agent_id="ar-target", target_runtime_id="runtime-target",
        target_a2a_agent_id="00000000000040008000000000000002", authn_mode="workload_permit",
        task_binding=A2AIngressTaskBinding("a2a-task-test", "version", "send_message"),
        verified_request_body=json.dumps(body).encode(),
    )


@pytest.mark.parametrize("rpc", [True, False])
def test_original_message_preserves_aliases_and_unknown_fields(rpc):
    message = {"message_id": "message-1", "parts": [{"text": "你好"}], "metadata": {"custom": [1, False]}}
    body = {"message": message}
    if rpc:
        body = {"jsonrpc": "2.0", "method": "message/send", "params": body}
    verified = identity(body)
    assert verified_inbound_message(verified) == message
    assert "你好" not in repr(verified)
    with pytest.raises(PermissionError):
        verified_inbound_message(replace(verified, task_binding=None))
    with pytest.raises(PermissionError):
        verified_inbound_message(replace(verified, verified_request_body=None))


@pytest.mark.asyncio
async def test_submission_uses_verified_task_and_rejects_target_drift():
    verified = identity({"message": {"message_id": "original", "parts": [{"text": "hello"}]}})
    receipt = AgentControlReceipt(command_id=uuid4(), status="accepted", message_id=uuid4(), accepted_seq=1)
    client = AsyncMock()
    client.submit_inbound_message.return_value = InboundKernelSubmission("a2a-kernel-derived", "ar-target", receipt)
    result = await submit_verified_inbound(client, verified, remote_task_id="remote", remote_context_id="context")
    assert result.agent_id == verified.target_agent_id
    assert client.submit_inbound_message.call_args.kwargs == {
        "platform_task_id": "a2a-task-test", "target_version_id": "version",
        "remote_task_id": "remote", "remote_context_id": "context",
        "message": {"message_id": "original", "parts": [{"text": "hello"}]},
    }
    client.submit_inbound_message.return_value = replace(result, agent_id="ar-other")
    with pytest.raises(PermissionError, match="target"):
        await submit_verified_inbound(client, verified, remote_task_id="remote", remote_context_id="context")
    client.reset_mock()
    with pytest.raises(PermissionError):
        await submit_verified_inbound(client, replace(verified, task_binding=None), remote_task_id="remote", remote_context_id="context")
    client.submit_inbound_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", [None, "command", "cursor", "session"])
async def test_control_client_uses_registry_and_validates_receipt(tmp_path, corrupt):
    _write_token(tmp_path, "a2a-registry")
    command_id = uuid4()
    receipt = AgentControlReceipt(command_id=command_id, status="accepted", message_id=uuid4(), accepted_seq=1)
    data = {"SessionId": "a2a-kernel-derived", "AgentId": "ar-target", "CommandId": str(command_id), "Receipt": receipt.model_dump(mode="json")}
    if corrupt == "command":
        data["CommandId"] = str(uuid4())
    elif corrupt == "cursor":
        data["Receipt"]["accepted_seq"] = None
    elif corrupt == "session":
        data["SessionId"] = "unrelated"
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"Code": 0, "Data": data})
    from ksadk.a2a.control_plane import FileWorkloadTokenProvider
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = InternalA2AControlPlaneClient("https://control", token_provider=FileWorkloadTokenProvider(tmp_path), httpx_client=http)
        async def submit():
            return await client.submit_inbound_message(platform_task_id="a2a-task-test", target_version_id="version", remote_task_id="remote", remote_context_id="context", message={"message_id": "message"})
        if corrupt:
            with pytest.raises(A2AControlPlaneError, match="INVALID_RESPONSE"):
                await submit()
        else:
            result = await submit()
            assert result.receipt.command_id == command_id
    assert len(seen) == 1
    assert seen[0].url.path.endswith("/SubmitA2AInboundMessage")
    assert seen[0].headers["authorization"].startswith("Bearer ")
    assert json.loads(seen[0].content)["Message"] == {"message_id": "message"}


def fact(seq, *, run_id="run-ours", state="pending", command_id=None, session="a2a-kernel-derived"):
    return SessionEventEnvelope(event_id=uuid4(), session_id=session, seq=seq, timestamp="2026-09-24T00:00:00Z", family="control", family_version=1, event_type="control.run_transition", payload={"run_id": run_id, "state": state}, run_id=run_id, causation_id=command_id)


class Events:
    def __init__(self, facts):
        self.facts = facts
        self.calls = []
    async def subscribe(self, session, cursor):
        self.calls.append((session, cursor))
        for event in self.facts:
            yield event


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["waiting", "completed", "failed", "cancelled", "interrupted"])
async def test_stream_selects_admitted_command_run_and_stops_at_durable_boundary(boundary):
    receipt = AgentControlReceipt(command_id=uuid4(), status="accepted", message_id=uuid4(), accepted_seq=2)
    submission = InboundKernelSubmission("a2a-kernel-derived", "ar-target", receipt)
    events = Events([fact(3, run_id="other", state="completed"), fact(4, command_id=str(receipt.command_id)), fact(5, state=boundary), fact(6, run_id="later")])
    result = [event async for event in stream_inbound_kernel_facts(submission, events)]
    assert [event.seq for event in result] == [4, 5]
    assert events.calls == [(submission.session_id, 0)]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["eof", "other_session", "conflicting_run", "rejected"])
async def test_stream_never_treats_missing_or_unrelated_facts_as_success(failure):
    receipt = AgentControlReceipt(command_id=uuid4(), status="accepted", message_id=uuid4(), accepted_seq=1)
    submission = InboundKernelSubmission("a2a-kernel-derived", "ar-target", receipt)
    facts = [fact(2, command_id=str(receipt.command_id))]
    if failure == "other_session":
        facts.append(fact(3, session="another"))
    elif failure == "conflicting_run":
        facts.append(fact(3, run_id="other", command_id=str(receipt.command_id)))
    elif failure == "rejected":
        facts.append(fact(3).model_copy(update={"event_type": "control.command_rejected", "payload": {"command_id": str(receipt.command_id)}}))
    with pytest.raises((RuntimeError, PermissionError)):
        _ = [event async for event in stream_inbound_kernel_facts(submission, Events(facts))]
