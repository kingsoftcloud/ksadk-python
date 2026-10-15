"""Resolve managed invocation approval from the Kernel's single durable log."""

import asyncio
import hashlib
import json
import time
from contextlib import aclosing

from pydantic import PrivateAttr

from ksadk.events.canonical import ApprovalRequest, InteractionRequested, SourceRef
from ksadk.events.identity import stable_event_id


async def invoke_with_approval(service, request, *, trigger_ref):
    """Pause the original invocation, then recheck durable policy before send."""
    from ksadk.a2a.invocation_policy import A2AApprovalRequired
    from ksadk.a2a.invocation_types import A2AContinuationRequest

    def operation():
        if isinstance(request, A2AContinuationRequest):
            return service.continue_task(request)
        return service.invoke(request, trigger_ref=trigger_ref)

    for attempt in range(2):
        try:
            async with aclosing(operation()) as events:
                async for event in events:
                    yield event
            return
        except A2AApprovalRequired:
            if attempt:
                raise RuntimeError("A2A_APPROVAL_NOT_COMMITTED") from None
            waiter = ManagedA2AApprovalWaiter(request)
            remaining = request.deadline_unix_seconds - time.time()
            if remaining <= 0:
                raise TimeoutError("A2A_OVERALL_TIMEOUT") from None
            try:
                yield waiter.event(timestamp=time.time())
                outcome = await asyncio.wait_for(
                    waiter.wait(), max(0, request.deadline_unix_seconds - time.time())
                )
            except TimeoutError:
                raise TimeoutError("A2A_OVERALL_TIMEOUT") from None
            finally:
                waiter.close()
            if outcome != "approved":
                raise PermissionError("A2A_APPROVAL_DENIED")


class ManagedA2AApprovalRequested(InteractionRequested):
    """An in-process binding; wire metadata cannot select a Kernel provider."""

    _invocation = PrivateAttr(default=None)
    _waiter = PrivateAttr(default=None)


class ManagedA2AApprovalWaiter:
    """One live call; only a committed Kernel receipt may wake it."""

    def __init__(self, request):
        self.request = request
        self._future = asyncio.get_running_loop().create_future()

    def event(self, *, timestamp):
        event = approval_event(self.request, timestamp=timestamp)
        event._waiter = self
        return event

    def accepts(self, record):
        return (not self._future.done()
                and record.interaction_id == approval_id(self.request)
                and record.native_target == approval_target(self.request)
                and record.run_id == self.request.handle.run_id
                and record.session_id == self.request.handle.session_id)

    async def wait(self):
        return await self._future

    def close(self):
        self._future.cancel()

    def deliver(self, receipt, response=None):
        if (receipt.interaction_id != approval_id(self.request)
                or receipt.status != "resolved"
                or receipt.outcome not in {"approved", "rejected"}):
            raise ValueError("A2A_APPROVAL_RECEIPT_INVALID")
        if not self._future.done():
            self._future.set_result(receipt.outcome)


def approval_event(request, *, timestamp):
    interaction_id = approval_id(request)
    event = ManagedA2AApprovalRequested(
        schema_version=2, seq=0, timestamp=timestamp,
        event_id=stable_event_id(
            "a2a", request.handle.parent_scope_id, interaction_id,
            "interaction.requested", "approval", interaction_id, 0,
        ),
        run_id=request.handle.run_id, scope_id=request.handle.parent_scope_id,
        source=SourceRef(framework="a2a"), interaction_id=interaction_id,
        interaction_kind="approval", request=ApprovalRequest(
            kind="tool", call_id=request.handle.call_id,
            detail={"tool_name": request.spec.local_name, "risk_level": "high",
                    "name": request.spec.local_name, "risk": "high",
                    "args": {
                        "target_agent_id": request.spec.target_agent_id,
                        "version_id": request.spec.expected_version_id,
                        "text_parts": list(request.message.text_parts),
                        "data_parts": list(request.message.data_parts),
                    },
                    "side_effects": ["a2a_remote_call"]},
        ),
    )
    event._invocation = request
    return event


def bound_approval_request(event, *, run_id, session_id):
    if not isinstance(event, ManagedA2AApprovalRequested):
        return None
    request = event._invocation
    if (request is None or request.handle.run_id != run_id
            or request.handle.session_id != session_id
            or event.run_id != run_id
            or event.scope_id != request.handle.parent_scope_id
            or event.interaction_id != approval_id(request)
            or event.request.call_id != request.handle.call_id):
        raise ValueError("A2A_APPROVAL_BINDING_CONFLICT")
    return request


def approval_target(request):
    """Bind every immutable invocation field without storing message plaintext."""
    values = request.model_dump(mode="json")
    values["message"] = {"message_id": request.message.message_id,
                         "sha256": request.message_sha256}
    # A public correlation hint is not part of the authorization identity.
    values["context"].pop("approval_request_id", None)
    digest = hashlib.sha256(json.dumps(
        values, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()).hexdigest()
    return {"schema": "a2a.approval/v1", "invocation_sha256": digest}


def approval_id(request):
    return "a2a-approval-" + approval_target(request)["invocation_sha256"]


class KernelA2AApprovalResolver:
    """Read a fully scoped record and its matching first-wins terminal fact.

    Tenant and agent-instance scope must come from the runtime composition
    root, never from model arguments. No secondary approval cache is created.
    """

    def __init__(self, ledger, event_store, *, tenant_id, agent_instance_id):
        if not tenant_id or not agent_instance_id:
            raise ValueError("A2A_APPROVAL_SCOPE_REQUIRED")
        self._ledger, self._events = ledger, event_store
        self._tenant, self._agent = tenant_id, agent_instance_id

    async def __call__(self, request):
        record = await self._ledger.get(
            approval_id(request), tenant_id=self._tenant,
            agent_instance_id=self._agent, session_id=request.handle.session_id,
            run_id=request.handle.run_id,
        )
        if record is None:
            return None
        if (record.kind != "approval" or record.provider_id != "managed-a2a"
                or record.native_target != approval_target(request)):
            raise ValueError("A2A_APPROVAL_BINDING_CONFLICT")
        if record.status in {"pending", "resolving"}:
            return None
        if record.status in {"cancelled", "expired"}:
            return "denied"
        cursor = 0
        while True:
            events = await self._events.read(record.session_id, cursor, 256)
            if not events:
                raise RuntimeError("A2A_APPROVAL_TERMINAL_FACT_MISSING")
            for event in events:
                payload = event.payload
                if (event.family == "interaction" and event.family_version == 1
                        and event.event_type == "interaction.resolved"
                        and event.run_id == record.run_id
                        and payload.get("interaction_id") == record.interaction_id
                        and payload.get("tenant_id") == self._tenant
                        and payload.get("agent_instance_id") == self._agent
                        and payload.get("session_id") == record.session_id
                        and payload.get("run_id") == record.run_id
                        and payload.get("revision") == record.revision):
                    return "approved" if payload.get("outcome") == "approved" else "denied"
            next_cursor = max(event.seq for event in events)
            if next_cursor <= cursor:
                raise RuntimeError("A2A_APPROVAL_CURSOR_INVALID")
            cursor = next_cursor
