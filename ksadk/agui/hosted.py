"""Prepare platform Actions and observe their admitted tasks; never execute a graph."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from ksadk.agui._agent_kernel import public_interrupt
from ksadk.agui._agent_resume import AguiRootNullUnsupported
from ksadk.agui._agent_types import _PendingInterrupt, _ThreadRun
from ksadk.interaction.form_values import normalize_form_answer
from ksadk.kernel.execution_grants import execution_grant_run_id
from ksadk.mcp_runtime.oauth.contract import CAPABILITY
from ksadk.runtime.adapter import RunHandle

PREPARE_PATH = "/agentengine/internal/agui/prepare"
ADMISSION_HEADER = "X-AgentEngine-AGUI-Admission"
VERSION_HEADER = "X-KsADK-AGUI-Preparation"
SCHEMA = "ksadk.agui.preparation/v1"


def request_digest(input):
    return hashlib.sha256(
        json.dumps(
            input.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def enqueue_idempotency(run_id):
    return "agui.enqueue:" + hashlib.sha256(run_id.encode()).hexdigest()


def resume_idempotency(run_id, interaction_id):
    return (
        "agui.resume:"
        + hashlib.sha256(
            json.dumps(
                [run_id, interaction_id],
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
    )


def hosted_runtime():
    from ksadk.kernel.bootstrap import get_agent_kernel_runtime
    from ksadk.kernel.ingress import authority_mode

    runtime = get_agent_kernel_runtime()
    if runtime is None or authority_mode() != "hosted":
        raise HTTPException(409, "Hosted AG-UI task admission is unavailable")
    return runtime


async def interaction(runtime, session_id, interaction_id, tenant_id):
    return await runtime.kernel_store.read_public_interaction(
        interaction_id,
        tenant_id=tenant_id,
        agent_instance_id=runtime.config.agent_instance_id,
        session_id=session_id,
    )


def decision(entry, record):
    from ksadk.agui._agent_helpers import approval_decision_for_audit

    if entry.status == "cancelled":
        return "cancel", None
    if "payload" not in entry.model_fields_set:
        raise HTTPException(422, "An explicit answer is required")
    if entry.payload is None:
        raise AguiRootNullUnsupported()
    if record["kind"] == "approval":
        action = (
            "approve"
            if approval_decision_for_audit(entry.status, entry.payload) == "approved"
            else "reject"
        )
    else:
        normalize_form_answer(record["request_schema"], entry.payload)
        action = "submit"
    return action, entry.payload


async def resume_records(runtime, input, tenant_id):
    entries = input.resume or []
    if len(entries) > 16:
        raise HTTPException(422, "At most 16 answers may be submitted together")
    if len({entry.interrupt_id for entry in entries}) != len(entries):
        raise HTTPException(422, "Duplicate interruptId")
    records = [
        await interaction(runtime, input.thread_id, entry.interrupt_id, tenant_id)
        for entry in entries
    ]
    if not records or any(record is None for record in records):
        raise HTTPException(404, "Interaction not found")
    if len({record["run_id"] for record in records}) != 1:
        raise HTTPException(422, "A resume batch must address one original run")
    terminal = [
        record for record in records if record["status"] in {"resolved", "cancelled", "expired"}
    ]
    if any(r["status"] == "expired" for r in terminal):
        raise HTTPException(409, "Interaction is already terminal")
    # Validate the complete batch before admitting its first command.
    for entry, record in zip(entries, records):
        decision(entry, record)
    return records, len(terminal) == len(records)


def matches_answer(command, *, agent, runtime, input, tenant_id, entry, record):
    action, response = decision(entry, record)
    return (
        command.command_type == "submit_interaction"
        and command.tenant_id == tenant_id
        and command.agent_instance_id == runtime.config.agent_instance_id
        and command.session_id == input.thread_id
        and command.payload.get("invocation_identity") == agent._identity.to_payload()
        and command.idempotency_key == resume_idempotency(input.run_id, entry.interrupt_id)
        and command.payload.get("run_id") == record["run_id"]
        and command.payload.get("interaction_id") == entry.interrupt_id
        and command.payload.get("action") == action
        and command.payload.get("response") == response
    )


async def prepare(agent, input, *, tenant_id):
    from ksadk.agui.config import AGUI_PROTOCOL_VERSION

    if input.protocol_version not in {None, AGUI_PROTOCOL_VERSION, "1.0.0"}:
        raise HTTPException(422, "Unsupported AG-UI protocol version")
    runtime = hosted_runtime()
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise HTTPException(401, "Verified platform account is required")
    if agent._identity.is_empty or agent._session is None:
        raise HTTPException(401, "Verified session identity is required")
    actions = []
    if input.resume:
        records, readonly = await resume_records(runtime, input, tenant_id)
        revisions = []
        # Reuse the original admitted command on retries, including its revision.
        # A worker may have consumed one answer while another submission failed.
        # Validate the whole batch before Gateway submits any further answers.
        for entry, record in zip(input.resume, records):
            message = await runtime.kernel_store.load_by_idempotency(
                input.thread_id, resume_idempotency(input.run_id, entry.interrupt_id)
            )
            if message is not None:
                if not matches_answer(message.command, agent=agent, runtime=runtime,
                                      input=input, tenant_id=tenant_id, entry=entry, record=record):
                    raise HTTPException(409, "Answer differs from the admitted submission")
                revisions.append(message.command.payload["expected_revision"])
            else:
                if not readonly and record["status"] != "pending":
                    raise HTTPException(409, "Interaction was completed by another submission")
                revisions.append(record["revision"])
        if not readonly:
            for entry, record, revision in zip(input.resume, records, revisions):
                action, response = decision(entry, record)
                actions.append(
                    {
                        "action": "SubmitInteraction",
                        "body": {
                            "RunId": record["run_id"],
                            "InteractionId": entry.interrupt_id,
                            "ExpectedRevision": revision,
                            "InteractionAction": action,
                            "Response": response,
                            "IdempotencyKey": resume_idempotency(input.run_id, entry.interrupt_id),
                        },
                    }
                )
    else:
        forwarded = input.forwarded_props or {}
        actions.append(
            {
                "action": "RunAgent",
                "body": {
                    "InvocationId": input.run_id,
                    "Background": True,
                    "Stream": False,
                    "Messages": [
                        {
                            "role": "user",
                            "content": agent._json_safe(agent._latest_user_input(input)),
                        }
                    ],
                    "Model": forwarded.get("model"),
                    "ModelMetadata": forwarded.get("modelMetadata"),
                    "ModelOptions": forwarded.get("modelOptions"),
                    "Metadata": {
                        "agentengine": {"tool_approval_mode": forwarded.get("toolApprovalMode")}
                    },
                    "Extensions": {
                        "mcp.authorization": CAPABILITY,
                        "ksadk.agui": {
                            "schema": "ksadk.agui/v1",
                            "request_digest": request_digest(input),
                            "options": agent._request_config(input)["ag-ui"],
                        },
                    },
                },
            }
        )
    for item in actions:
        item["body"].update(AgentId=agent._session.agent_id, SessionId=input.thread_id)
    return {
        "schema": SCHEMA,
        "agent_id": agent._session.agent_id,
        "session_id": input.thread_id,
        "tenant_id": tenant_id,
        "request_digest": request_digest(input),
        "actions": actions,
    }


@dataclass
class KernelStream:
    target_run: str
    run: Any
    events: Any
    read_interaction: Any
    readonly: bool = False
    command_ids: set[str] = field(default_factory=set)
    submissions: dict[str, str] = field(default_factory=dict)
    unconsumed: set[str] = field(default_factory=set)
    ready: list = field(default_factory=list)


async def observe(agent, input, admission):
    """Authorize the read against persisted Server-admitted commands, not header claims."""
    runtime = hosted_runtime()
    if (
        not isinstance(admission, dict)
        or admission.get("schema") != SCHEMA
        or admission.get("request_digest") != request_digest(input)
        or not isinstance(admission.get("receipts"), list)
        or not isinstance(admission.get("tenant_id"), str)
        or not admission["tenant_id"].strip()
    ):
        raise HTTPException(403, "Invalid AG-UI admission binding")
    receipts = admission["receipts"]
    if not receipts:
        # No persisted command is available to bind this read-only replay.
        # Revalidate the session owner before exposing any interaction/events.
        if agent._identity.is_empty:
            raise HTTPException(401, "Verified session identity is required")
        await agent._authorize_session(input)
    tenant_id = admission["tenant_id"]
    records, readonly = (
        await resume_records(runtime, input, tenant_id) if input.resume else ([], False)
    )
    if (
        len(receipts) > 16
        or (not receipts and not readonly)
        or (receipts and len(receipts) != len(input.resume or [None]))
    ):
        raise HTTPException(403, "Invalid AG-UI receipt count")
    target = records[0]["run_id"] if records else None
    command_ids, submissions = set(), {}
    cursors = []
    for index, receipt in enumerate(receipts):
        if not isinstance(receipt, dict) or any(
            not isinstance(receipt.get(key), str) or not receipt[key]
            for key in ("MessageId", "CommandId")
        ):
            raise HTTPException(403, "Invalid AG-UI receipt")
        try:
            UUID(receipt["MessageId"])
            UUID(receipt["CommandId"])
        except ValueError:
            raise HTTPException(403, "Invalid AG-UI receipt") from None
        message = await runtime.kernel_store.load_message(str(receipt.get("MessageId") or ""))
        command = message.command if message is not None else None
        if (
            command is None
            or str(command.command_id) != receipt.get("CommandId")
            or command.agent_instance_id != runtime.config.agent_instance_id
            # The Session storage partition may be "default"; it is not the
            # platform tenant signed into the admitted command.
            or command.tenant_id != tenant_id
            or command.session_id != input.thread_id
            or command.payload.get("invocation_identity") != agent._identity.to_payload()
        ):
            raise HTTPException(403, "AG-UI command owner mismatch")
        if input.resume:
            entry = input.resume[index]
            if not matches_answer(command, agent=agent, runtime=runtime, input=input,
                                  tenant_id=tenant_id, entry=entry, record=records[index]):
                raise HTTPException(403, "AG-UI answer binding mismatch")
            submissions[str(command.command_id)] = entry.interrupt_id
        else:
            options = (command.payload.get("runtime_options") or {}).get("ag-ui") or {}
            if (
                command.command_type != "enqueue"
                or command.source.kind != "agui"
                or command.idempotency_key != enqueue_idempotency(input.run_id)
                or options.get("request_digest") != request_digest(input)
            ):
                raise HTTPException(403, "AG-UI request binding mismatch")
            target = execution_grant_run_id(command)
        command_ids.add(str(command.command_id))
        cursors.append(message.accepted_seq)
    if not target:
        raise HTTPException(403, "AG-UI original run is missing")
    run = _ThreadRun(RunHandle(run_id=target, session_id=input.thread_id, runtime_type="ksadk"))
    for record in records:
        if record["status"] == "pending":
            interrupt = public_interrupt(record)
            run.pending[interrupt.id] = _PendingInterrupt(interrupt, "", record["kind"])

    async def events():
        # Same admitted, read-only observer used by managed A2A. The immutable
        # command and session owner above authorize this internal store read;
        # all mutations still require the existing Server-issued permits.
        subscribe = getattr(
            runtime.session_events, "subscribe_recovering", runtime.session_events.subscribe
        )
        async for envelope in subscribe(input.thread_id, min(cursors, default=0)):
            if envelope.session_id != input.thread_id:
                raise PermissionError("AG-UI event session mismatch")
            yield envelope.seq, envelope

    return KernelStream(
        target,
        run,
        events(),
        lambda key: interaction(runtime, input.thread_id, key, tenant_id),
        readonly=not receipts,
        command_ids=command_ids,
        submissions=submissions,
        unconsumed={entry.interrupt_id for entry in input.resume or []} if receipts else set(),
    )
