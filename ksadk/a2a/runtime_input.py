"""Keep the original remote invocation alive across Kernel-owned input waits."""

import asyncio
import time
from collections.abc import Mapping
from contextlib import aclosing

from pydantic import PrivateAttr

from ksadk.a2a.invocation_types import A2AContinuationRequest, A2AInvocationMessage
from ksadk.a2a.runtime_approval import ManagedA2AApprovalRequested, invoke_with_approval
from ksadk.events.canonical import InteractionRequested
from ksadk.events.identity import stable_item_id


def _structured_input_value(event, response):
    """Remove the UI result envelope only for non-object input schemas."""
    schema = getattr(getattr(event, "request", None), "schema_", None)
    normalized = dict(response) if isinstance(response, Mapping) else response
    if (
        isinstance(normalized, dict)
        and set(normalized) == {"result"}
        and isinstance(schema, dict)
        and schema.get("type") != "object"
    ):
        return normalized["result"]
    return normalized


class ManagedA2AInputRequested(InteractionRequested):
    _managed_input_waiter = PrivateAttr(default=None)


class ManagedA2AInputWaiter:
    def __init__(self, event, snapshot):
        self.handle = snapshot.handle
        self.interaction_id = event.interaction_id
        self.target = {
            "schema": ("a2a.remote-approval/v1" if event.interaction_kind == "approval"
                       else "a2a.input/v1"),
            "handle": self.handle.model_dump(mode="json"),
            "continuation_id": snapshot.continuation_id,
            "interaction_id": event.interaction_id,
        }
        self.event = ManagedA2AInputRequested.model_validate(event.model_dump(by_alias=True))
        self.event._managed_input_waiter = self
        self._future = asyncio.get_running_loop().create_future()

    def accepts(self, record):
        return (
            not self._future.done()
            and record.interaction_id == self.interaction_id
            and record.native_target == self.target
            and record.run_id == self.handle.run_id
            and record.session_id == self.handle.session_id
        )

    def deliver(self, receipt, response=None):
        outcomes = ({"approved", "rejected"} if self.event.interaction_kind == "approval"
                    else {"submitted", "rejected"})
        if (
            receipt.interaction_id != self.interaction_id
            or receipt.status != "resolved"
            or receipt.outcome not in outcomes
        ):
            raise ValueError("A2A_INPUT_RECEIPT_INVALID")
        if not self._future.done():
            self._future.set_result((receipt.outcome, response))

    async def wait(self):
        return await self._future

    def close(self):
        self._future.cancel()


def bound_input_waiter(event, *, run_id, session_id):
    if not isinstance(event, ManagedA2AInputRequested):
        return None
    waiter = event._managed_input_waiter
    if (
        waiter is None
        or event is not waiter.event
        or event.run_id != run_id
        or waiter.handle.run_id != run_id
        or waiter.handle.session_id != session_id
        or event.scope_id != waiter.handle.scope_id
        or event.interaction_id != waiter.interaction_id
    ):
        raise ValueError("A2A_INPUT_BINDING_CONFLICT")
    return waiter


async def invoke_with_input(service, request, *, trigger_ref):
    """Wait on the same task/scope; supplemental messages get their own approval."""
    frozen = await service.pending_continuation(request.handle)
    if frozen is not None:
        request = frozen
    while True:
        pending = None
        async with aclosing(
            invoke_with_approval(service, request, trigger_ref=trigger_ref)
        ) as events:
            async for event in events:
                if (
                    isinstance(event, InteractionRequested)
                    and event.interaction_kind in {"structured_input", "approval"}
                    and not isinstance(event, ManagedA2AApprovalRequested)
                ):
                    pending = event
                else:
                    yield event
        snapshot = service.snapshot(service.handle(request.scope_id))
        if snapshot is None or snapshot.status not in {"input_required", "auth_required"}:
            return
        if pending is None:
            pending = await service.pending_input_request(snapshot.handle)
        if pending is None or snapshot.continuation_id is None:
            raise ValueError("A2A_INPUT_CONTINUATION_REQUIRED")
        waiter = ManagedA2AInputWaiter(pending, snapshot)
        try:
            yield waiter.event
            outcome, response = await asyncio.wait_for(
                waiter.wait(), max(0, request.deadline_unix_seconds - time.time())
            )
        except TimeoutError:
            raise TimeoutError("A2A_OVERALL_TIMEOUT") from None
        finally:
            waiter.close()
        is_approval = pending.interaction_kind == "approval"
        if outcome != ("approved" if is_approval else "submitted"):
            if is_approval:
                # Rejection never sends a supplemental message. Retire the
                # remote wait through the normal authorized cancellation path;
                # that path records unknown outcomes without inventing success.
                await service.cancel(snapshot.handle)
            raise PermissionError("A2A_INPUT_REJECTED")
        response_value = "approve" if is_approval else _structured_input_value(pending, response)
        request = A2AContinuationRequest(
            spec=request.spec,
            handle=snapshot.handle,
            message=A2AInvocationMessage(
                message_id=stable_item_id("a2a", request.scope_id, pending.interaction_id, "input"),
                data_parts=(response_value,),
            ),
            context=request.context,
            deadline_unix_seconds=request.deadline_unix_seconds,
            idempotency_token=stable_item_id(
                "a2a", request.scope_id, pending.interaction_id, "continue"
            ),
        )
