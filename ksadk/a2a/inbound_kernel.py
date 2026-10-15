"""Managed A2A input admitted by Server, never by a local self-signed permit."""

from __future__ import annotations

import json
from dataclasses import dataclass
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from ksadk.a2a.identity import A2AIngressIdentity
from ksadk.kernel.contracts import AgentControlReceipt, SessionEventEnvelope


class ManagedKernelRequestError(RuntimeError):
    """Admission failed or its outcome is unknown; preserve the durable Task."""


@dataclass(frozen=True)
class InboundKernelSubmission:
    session_id: str
    agent_id: str
    receipt: AgentControlReceipt

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> InboundKernelSubmission:
        session_id = data.get("SessionId")
        agent_id = data.get("AgentId")
        if not isinstance(session_id, str) or not session_id.startswith("a2a-kernel-") or len(session_id) > 128:
            raise ValueError("invalid Server-derived A2A session")
        if not isinstance(agent_id, str) or not agent_id.startswith("ar-") or len(agent_id) > 64:
            raise ValueError("invalid Server A2A target")
        receipt = AgentControlReceipt.model_validate(data.get("Receipt"))
        if receipt.command_id != UUID(data.get("CommandId", "")):
            raise ValueError("Server receipt command identity mismatch")
        if receipt.status in {"accepted", "duplicate"} and (
            receipt.accepted_seq is None or receipt.accepted_seq < 1
        ):
            raise ValueError("accepted A2A submission has no durable command sequence")
        return cls(session_id=session_id, agent_id=agent_id, receipt=receipt)


def verified_inbound_message(identity: A2AIngressIdentity) -> dict[str, Any]:
    """Extract the original Message without protobuf alias/default rewriting."""
    identity.validate()
    binding = identity.task_binding
    if binding is None or binding.operation != "send_message" or identity.verified_request_body is None:
        raise PermissionError("managed Kernel send requires verified Gateway message provenance")
    try:
        body = json.loads(identity.verified_request_body)
        if not isinstance(body, dict):
            raise ValueError("request must be an object")
        if "jsonrpc" in body:
            if body.get("jsonrpc") != "2.0" or body.get("method") not in {
                "message/send", "message/stream", "SendMessage", "SendStreamingMessage"
            }:
                raise ValueError("request is not a send")
            body = body.get("params")
        message = body.get("message") if isinstance(body, dict) else None
        if not isinstance(message, dict):
            raise ValueError("request has no Message")
        return message
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise PermissionError("verified Gateway message cannot be decoded") from exc


async def submit_verified_inbound(
    client: Any, identity: A2AIngressIdentity, *, remote_task_id: str, remote_context_id: str,
    continuation: dict[str, Any] | None = None,
) -> InboundKernelSubmission:
    message = verified_inbound_message(identity)
    result = await client.submit_inbound_message(
        platform_task_id=identity.task_binding.platform_task_id,
        target_version_id=identity.task_binding.target_version_id,
        remote_task_id=remote_task_id, remote_context_id=remote_context_id,
        message=message,
        **({"continuation": continuation} if continuation is not None else {}),
    )
    if result.agent_id != identity.target_agent_id:
        raise PermissionError("Server admission target differs from verified Gateway target")
    return result


async def stream_inbound_kernel_facts(
    submission: InboundKernelSubmission, events: Any, *, expected_run_id: str | None = None,
    after_seq: int = 0,
    stop_at_waiting: bool = True,
) -> AsyncIterator[SessionEventEnvelope]:
    """Read only this admitted command's run from the local durable event log.

    This is an internal projection after Server authorization, not a public
    subscription endpoint. Never infer completion from EOF or another run.
    """
    receipt = submission.receipt
    if receipt.status not in {"accepted", "duplicate"}:
        raise RuntimeError("Server did not accept the A2A Kernel submission")
    if receipt.accepted_seq is None or receipt.accepted_seq < 1:
        raise RuntimeError("Server admission has no durable command sequence")
    if expected_run_id and receipt.run_id and expected_run_id != receipt.run_id:
        raise RuntimeError("Server receipt changed the resumed run")
    run_id = receipt.run_id or expected_run_id
    # accepted_seq is the inbox order, not a SessionEvent cursor. Only a
    # previously observed durable event seq may advance this subscription.
    if type(after_seq) is not int or after_seq < 0:
        raise ValueError("invalid persisted Kernel event cursor")
    cursor = after_seq
    command_id = str(receipt.command_id)
    async for fact in events.subscribe(submission.session_id, cursor):
        if fact.session_id != submission.session_id:
            raise PermissionError("Kernel event store returned another session")
        if fact.seq <= cursor:
            continue
        cursor = fact.seq
        if fact.event_type == "control.command_rejected" and fact.payload.get("command_id") == command_id:
            raise RuntimeError("Kernel rejected the admitted A2A command")
        if fact.family == "control" and fact.event_type == "control.run_transition" and fact.causation_id == command_id:
            if not fact.run_id or fact.payload.get("run_id") != fact.run_id:
                raise RuntimeError("Kernel command has an invalid run binding")
            if run_id is not None and run_id != fact.run_id:
                raise RuntimeError("Kernel command maps to conflicting runs")
            run_id = fact.run_id
        if not run_id or fact.run_id != run_id:
            continue
        yield fact
        # The worker persists interaction.requested before WAITING. Return all
        # facts through the state transition so the caller can persist both the
        # pending interaction and its exact replay cursor before replying.
        boundaries = {"completed", "failed", "cancelled", "interrupted"}
        if stop_at_waiting:
            boundaries.add("waiting")
        if fact.family == "control" and fact.event_type == "control.run_transition" and fact.payload.get("state") in boundaries:
            return
    raise ManagedKernelRequestError("Kernel event stream ended without a durable run boundary")
