"""Project the existing Kernel log onto AG-UI; never execute a second graph."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from ag_ui.core import (
    CustomEvent,
    Interrupt,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunStartedEvent,
)

from ksadk.agui._agent_helpers import approval_decision_for_audit
from ksadk.agui._agent_resume import AguiRootNullUnsupported
from ksadk.agui._agent_types import _PendingInterrupt, _ThreadRun, _WireState
from ksadk.agui.config import AGUI_PROTOCOL_VERSION
from ksadk.events.canonical import parse_runtime_event
from ksadk.interaction.form_values import normalize_form_answer
from ksadk.interaction.schema import InputValidationError
from ksadk.kernel import ingress
from ksadk.kernel.contracts import AgentStatusQuery
from ksadk.kernel.execution_grants import execution_grant_run_id
from ksadk.runtime.adapter import RunHandle

logger = logging.getLogger(__name__)
SESSION_EVENT_NAME = "ksadk.session_event"


class AguiInteractionAlreadyTerminal(ValueError):
    """A stale or mixed answer batch must not submit another command."""


def public_interrupt(payload: dict[str, Any]) -> Interrupt:
    request = payload.get("request") or payload
    presentation = request.get("presentation") or {}
    return Interrupt(
        id=payload["interaction_id"],
        reason=payload.get("kind", "structured_input"),
        message=presentation.get("title") or presentation.get("description"),
        response_schema=request.get("request_schema"),
        expires_at=request.get("expires_at"),
        metadata={
            "interaction_v1": True,
            "revision": payload["revision"],
            "run_id": payload["run_id"],
        },
    )


class AGUIKernelMixin:
    async def _kernel_ready_interrupts(self, ready, run, trusted, input):
        current = await ingress.get_agent_kernel().pending_interactions(
            AgentStatusQuery(
                tenant_id=trusted.tenant_id, agent_instance_id=trusted.agent_instance_id,
                session_id=input.thread_id, authorization_ref=trusted.permit.permit_id,
            ), permit=trusted.permit,
        )
        live_ids = {item["interaction_id"] for item in current if item["status"] == "pending"}
        return [run.pending[key].interrupt for key in ready
                if key in live_ids and key in run.pending]

    async def _kernel_owns_resume(self, input):
        trusted = ingress.trusted_context(
            source_kind="agui",
            source_ref=input.run_id,
            session_id=input.thread_id,
            operations=("get_status",),
            launch_context=self._shared.launch_context,
        )
        query = AgentStatusQuery(
            tenant_id=trusted.tenant_id,
            agent_instance_id=trusted.agent_instance_id,
            session_id=input.thread_id,
            authorization_ref=trusted.permit.permit_id,
        )
        kernel = ingress.get_agent_kernel()
        known = [
            await kernel.public_interaction(query, entry.interrupt_id, permit=trusted.permit)
            for entry in input.resume
        ]
        return any(item is not None for item in known)

    async def _kernel_run(self, input):
        yield RunStartedEvent(
            thread_id=input.thread_id,
            run_id=input.run_id,
            protocol_version=AGUI_PROTOCOL_VERSION,
            parent_run_id=input.parent_run_id,
            input=input,
        )
        wire = _WireState(
            input.thread_id, input.run_id, f"{input.run_id}:assistant", f"{input.run_id}:reasoning"
        )
        try:
            trusted = ingress.trusted_context(
                source_kind="agui",
                source_ref=input.run_id,
                session_id=input.thread_id,
                operations=(
                    "enqueue",
                    "submit_interaction",
                    "interrupt",
                    "get_status",
                    "subscribe_events",
                ),
                launch_context=self._shared.launch_context,
            )
            initial_waits = []
            unconsumed = {entry.interrupt_id for entry in input.resume or []}
            target_run = None
            if input.resume:
                initial_waits, commands, target_run = await self._kernel_resumes(input, trusted)
            else:
                commands = [
                    ingress.map_agui_request(
                        session_id=input.thread_id,
                        idempotency_key=input.run_id,
                        content=self._latest_user_input(input),
                        run_id=input.run_id,
                        trusted=trusted,
                        runtime_options={
                            "model": (input.forwarded_props or {}).get("model"),
                            "ag-ui": self._request_config(input)["ag-ui"],
                            "conversation": {
                                "model_metadata": (input.forwarded_props or {}).get(
                                    "modelMetadata"
                                ),
                                "model_options": (input.forwarded_props or {}).get("modelOptions"),
                                "request_metadata": {
                                    "transport": "ag-ui",
                                    "tool_approval_mode": (input.forwarded_props or {}).get(
                                        "toolApprovalMode"
                                    ),
                                },
                            },
                        },
                    )
                ]
            readonly_resume = bool(input.resume) and not commands
            if readonly_resume:
                unconsumed.clear()
            receipts = []
            for command in commands:
                # Mutation permits are single-command nonces. A parallel
                # answer batch must not reuse one nonce for distinct commands.
                admission = ingress.trusted_context(
                    source_kind="agui",
                    source_ref=input.run_id,
                    session_id=input.thread_id,
                    operations=(command.command_type,),
                    launch_context=self._shared.launch_context,
                )
                if (
                    admission.tenant_id != trusted.tenant_id
                    or admission.agent_instance_id != trusted.agent_instance_id
                ):
                    raise ValueError("Trusted admission scope changed")
                payload = dict(command.payload)
                if command.command_type == "submit_interaction":
                    payload["token_ref"] = admission.permit.permit_id
                command = command.model_copy(
                    update={
                        "authorization_ref": admission.permit.permit_id,
                        "payload": payload,
                    }
                )
                receipt = await ingress.submit_command(command, permit=admission.permit)
                if receipt.status not in {"accepted", "duplicate"}:
                    yield RunErrorEvent(
                        message="Agent kernel rejected command", code=receipt.status.upper()
                    )
                    return
                receipts.append(receipt)
            if target_run is None:
                target_run = execution_grant_run_id(
                    commands[0].model_copy(update={"command_id": receipts[0].command_id})
                )
            # Receipts and subsequent facts are read from the same physical
            # Session cursor. A duplicate uses its original accepted cursor.
            # Terminal interactions already have an authoritative original Run.
            # Replay its public facts without admitting a new answer command.
            after_seq = min((int(item.accepted_seq or 0) for item in receipts), default=0)
            run = _ThreadRun(
                RunHandle(
                    run_id=target_run or input.run_id,
                    session_id=input.thread_id,
                    runtime_type="ksadk",
                )
            )
            for payload in initial_waits:
                if payload["run_id"] != target_run:
                    continue
                interrupt = public_interrupt(payload)
                run.pending[interrupt.id] = _PendingInterrupt(interrupt, "", payload["kind"])
            if readonly_resume:
                interrupts = await self._kernel_ready_interrupts(
                    list(run.pending), run, trusted, input,
                )
                if interrupts:
                    yield RunFinishedEvent(
                        thread_id=input.thread_id, run_id=input.run_id,
                        outcome=RunFinishedInterruptOutcome(interrupts=interrupts),
                    )
                    return
            command_ids = {str(item.command_id) for item in receipts}
            submitted_interactions = {
                str(receipt.command_id): command.payload["interaction_id"]
                for command, receipt in zip(commands, receipts)
                if command.command_type == "submit_interaction"
            }
            async for _, envelope in ingress.subscribe_projected(
                input.thread_id,
                trusted=trusted,
                after_seq=after_seq,
            ):
                if envelope.family == "control":
                    if (envelope.event_type.startswith("mcp.authorization.")
                            and envelope.run_id == target_run):
                        # OAuth waits use an independent control extension,
                        # not the ordinary interaction ledger. Keep streaming
                        # so its signed completion can resume this same run.
                        yield CustomEvent(
                            name=SESSION_EVENT_NAME, value=envelope.model_dump(mode="json")
                        )
                        continue
                    if (
                        envelope.event_type == "control.recovery_decided"
                        and envelope.run_id == target_run
                        and envelope.payload.get("reason")
                        == "native_submission_requires_reconciliation"
                    ):
                        yield RunErrorEvent(
                            message="Submission outcome requires reconciliation before retry",
                            code="SUBMISSION_RECONCILIATION_REQUIRED",
                        )
                        return
                    if (
                        envelope.event_type == "control.command_rejected"
                        and str(envelope.payload.get("command_id")) in command_ids
                    ):
                        interaction_id = submitted_interactions.get(
                            str(envelope.payload.get("command_id"))
                        )
                        if interaction_id is not None:
                            # Another submission may win after the pending
                            # preflight. Classify only this admitted occurrence,
                            # using its authorized public state, never its answer.
                            current = await ingress.get_agent_kernel().public_interaction(
                                AgentStatusQuery(
                                    tenant_id=trusted.tenant_id,
                                    agent_instance_id=trusted.agent_instance_id,
                                    session_id=input.thread_id,
                                    authorization_ref=trusted.permit.permit_id,
                                ),
                                interaction_id, permit=trusted.permit,
                            )
                            if (current is not None and current["run_id"] == target_run
                                    and current["status"] in {"resolved", "cancelled", "expired"}):
                                raise AguiInteractionAlreadyTerminal()
                        yield RunErrorEvent(
                            message="Answer was not consumed; correct and retry",
                            code="INPUT_RESPONSE_INVALID",
                        )
                        return
                    if (envelope.event_type == "control.interaction_wait_ready"
                            and envelope.run_id == target_run):
                        # This cursor marks the end of this activation. A fast
                        # answer may already have resolved the durable wait;
                        # querying live state here would stream its resumed
                        # output twice (also owned by the resume subscriber).
                        interrupts = [
                            run.pending[key].interrupt
                            for key in envelope.payload.get("interaction_ids", [])
                            if key in run.pending
                        ]
                        if interrupts and not unconsumed and not readonly_resume:
                            async for close in self._close_open_messages(wire):
                                yield close
                            yield RunFinishedEvent(
                                thread_id=input.thread_id, run_id=input.run_id,
                                outcome=RunFinishedInterruptOutcome(interrupts=interrupts),
                            )
                            return
                    continue
                # Session subscriptions may contain unrelated runs. Bind the
                # new run to its persisted source correlation, not arrival order.
                if envelope.run_id:
                    if envelope.run_id != target_run:
                        continue
                if envelope.family == "interaction":
                    yield CustomEvent(
                        name=SESSION_EVENT_NAME, value=envelope.model_dump(mode="json")
                    )
                    payload = envelope.payload
                    if envelope.event_type == "interaction.requested":
                        interrupt = public_interrupt(payload)
                        run.pending[interrupt.id] = _PendingInterrupt(
                            interrupt, "", payload["kind"]
                        )
                    else:
                        run.pending.pop(str(payload.get("interaction_id")), None)
                        unconsumed.discard(str(payload.get("interaction_id")))
                    continue
                if envelope.family != "runtime" or envelope.family_version != 2:
                    continue
                event = parse_runtime_event(envelope.payload)
                if event.source.metadata.get("native_reconciliation_required"):
                    yield RunErrorEvent(
                        message="Submission outcome requires reconciliation before retry",
                        code="SUBMISSION_RECONCILIATION_REQUIRED",
                    )
                    return
                ready = event.source.metadata.get("interaction_wait_ready")
                if isinstance(ready, list):
                    # Read old private-candidate logs without producing new
                    # synthetic RunProgress facts for this transport barrier.
                    interrupts = [run.pending[key].interrupt for key in ready if key in run.pending]
                    if interrupts and not unconsumed and not readonly_resume:
                        async for close in self._close_open_messages(wire):
                            yield close
                        yield RunFinishedEvent(
                            thread_id=input.thread_id,
                            run_id=input.run_id,
                            outcome=RunFinishedInterruptOutcome(interrupts=interrupts),
                        )
                        return
                async for projected in self._project(event, wire, run):
                    yield projected
                if wire.terminal:
                    if not readonly_resume:
                        await self._update_session_metadata_after_assistant_turn(input, wire)
                    return
        except AguiInteractionAlreadyTerminal:
            yield RunErrorEvent(
                message="Interaction is already terminal; refresh pending inputs before submitting",
                code="INTERACTION_ALREADY_TERMINAL",
            )
        except AguiRootNullUnsupported:
            yield RunErrorEvent(
                message="Use Interaction/v1 to submit a root null answer",
                code="INPUT_TRANSPORT_UNSUPPORTED",
            )
        except InputValidationError:
            yield RunErrorEvent(
                message="Response does not satisfy the input schema", code="INPUT_RESPONSE_INVALID"
            )
        except Exception:
            logger.exception("AG-UI Kernel projection failed")
            yield RunErrorEvent(message="Agent kernel ingress failed", code="KERNEL_ERROR")

    async def _kernel_resumes(self, input, trusted):
        """Official resume entries become real Inbox commands on the original Run."""
        ids = [entry.interrupt_id for entry in input.resume]
        if len(ids) != len(set(ids)):
            raise ValueError("resume contains duplicate interruptId values")
        kernel = ingress.get_agent_kernel()
        waits = await kernel.pending_interactions(
            AgentStatusQuery(
                tenant_id=trusted.tenant_id,
                agent_instance_id=trusted.agent_instance_id,
                session_id=input.thread_id,
                authorization_ref=trusted.permit.permit_id,
            ),
            permit=trusted.permit,
        )
        by_id = {item["interaction_id"]: item for item in waits}
        for entry in input.resume:
            if entry.interrupt_id not in by_id:
                known = await kernel.public_interaction(
                    AgentStatusQuery(
                        tenant_id=trusted.tenant_id,
                        agent_instance_id=trusted.agent_instance_id,
                        session_id=input.thread_id,
                        authorization_ref=trusted.permit.permit_id,
                    ),
                    entry.interrupt_id,
                    permit=trusted.permit,
                )
                if known is not None:
                    by_id[entry.interrupt_id] = known
        records = []
        run_ids = set()
        for entry in input.resume:
            record = by_id.get(entry.interrupt_id)
            if record is None:
                raise ValueError("Unknown pending interaction")
            run_ids.add(record["run_id"])
            if len(run_ids) != 1:
                raise ValueError("A resume batch must address a single original Run")
            records.append(record)
        terminal = [record for record in records
                    if record["status"] in {"resolved", "cancelled", "expired"}]
        if terminal:
            # Complete the whole preflight before any Inbox admission. Never
            # partially execute fresh answers alongside an old terminal wait.
            if len(terminal) != len(records) or any(
                record["status"] == "expired" for record in terminal
            ):
                raise AguiInteractionAlreadyTerminal()
            return waits, [], next(iter(run_ids))
        commands = []
        for entry, record in zip(input.resume, records):
            expected_revision = record["revision"]
            key = (
                "agui:"
                + hashlib.sha256(
                    json.dumps(
                        [input.run_id, entry.interrupt_id],
                        ensure_ascii=False,
                    ).encode()
                ).hexdigest()
            )
            if entry.status == "cancelled":
                commands.append(
                    ingress.map_control_request(
                        command_type="submit_interaction",
                        session_id=input.thread_id,
                        idempotency_key=key,
                        payload={
                            "run_id": record["run_id"],
                            "interaction_id": entry.interrupt_id,
                            "expected_revision": expected_revision,
                            "action": "cancel",
                            "response": None,
                            "token_ref": trusted.permit.permit_id,
                        },
                        trusted=trusted,
                        run_id=record["run_id"],
                    )
                )
                continue
            if "payload" in entry.model_fields_set and entry.payload is None:
                raise AguiRootNullUnsupported()
            if "payload" not in entry.model_fields_set:
                raise InputValidationError(
                    [
                        {
                            "path": "",
                            "keyword": "required",
                            "message": "An explicit answer is required",
                        }
                    ]
                )
            approval = record["kind"] == "approval"
            action = (
                (
                    "approve"
                    if approval_decision_for_audit(entry.status, entry.payload) == "approved"
                    else "reject"
                )
                if approval
                else "submit"
            )
            if not approval:
                # Keep the raw command/fingerprint; the Worker delivers the
                # typed value after validating against this saved Schema.
                normalize_form_answer(record["request_schema"], entry.payload)
            commands.append(
                ingress.map_control_request(
                    command_type="submit_interaction",
                    session_id=input.thread_id,
                    idempotency_key=key,
                    trusted=trusted,
                    run_id=record["run_id"],
                    payload={
                        "run_id": record["run_id"],
                        "interaction_id": entry.interrupt_id,
                        "expected_revision": expected_revision,
                        "action": action,
                        "response": entry.payload,
                        "token_ref": trusted.permit.permit_id,
                    },
                )
            )
        return waits, commands, next(iter(run_ids))
