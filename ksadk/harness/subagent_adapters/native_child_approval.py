"""Route an owned child's existing pause; grant only via its original Kernel.

ZCode's child-client-ports and subagent-interaction-broker route to a parent
while retaining child origin. Local Futures own waiters only; the scoped Run
snapshot, reservation, command and original authorizer remain authoritative.
"""

import asyncio

from ksadk.harness.native.child_approval import host_id, validate_child_approval
from ksadk.harness.native.model import ModelToolCall
from ksadk.harness.native.ports import digest
from ksadk.harness.native.state import ChildApproval, RunCommand, RunPhase

from .native_child import _settle


def pause_reference(handle, kernel, snapshot):
    call = ModelToolCall.model_validate(snapshot.pending_tools[0])
    definition = next(item for item in kernel.configuration.tools if item.name == call.name)
    child = handle.request
    return ChildApproval(
        child_run_id=child.child_run_id,
        child_scope=child.scope,
        parent_operation_key=child.parent_operation_key,
        parent_tool_call_id=child.parent_tool_call_id,
        interaction_id=snapshot.pending_interaction,
        host_interaction_id=snapshot.interaction_host_id,
        tool_call=call,
        definition_digest=digest(definition),
    )


async def await_decision(handle, kernel, snapshot):
    handle.approval = pause_reference(handle, kernel, snapshot)
    handle.approval_decision = asyncio.get_running_loop().create_future()
    handle.approval_ready.set()
    try:
        await handle.approval_decision
    finally:
        handle.approval_ready.clear()
        handle.approval = None


def current_approval(handle):
    if (
        handle.approval is None
        or not handle.approval_ready.is_set()
        or handle.approval_decision is None
        or handle.approval_decision.done()
    ):
        return None
    child = handle.kernel.snapshot(handle.request.child_run_id)
    if child.phase != RunPhase.AWAITING_INTERACTION:
        return None
    reference = pause_reference(handle, handle.kernel, child)
    if reference != handle.approval:
        raise ValueError("child_approval_pause_changed")
    return reference


def answer_original_pause(owner, parent, request):
    approval = request.child_approval
    if approval is None:
        return
    if (
        not owner.approval_bridge
        or parent.child_approval != approval
        or parent.interaction != request.interaction
        or request.interaction is None
        or request.interaction.get("interaction_id") != host_id(approval)
        or request.interaction.get("decision") not in {"allow", "deny"}
    ):
        raise ValueError("child_approval_parent_decision_mismatch")
    validate_child_approval(parent, approval)
    handle = owner._handles.get(approval.child_run_id)
    if handle is None or handle.request.parent_run_id != parent.run_id:
        raise ValueError("child_approval_owner_unavailable")
    if current_approval(handle) != approval:
        raise ValueError("child_approval_pause_changed")
    intent = owner.store.get_intent(parent.scope, parent.run_id, request.operation_key)
    if (
        parent.pending_intent != request.operation_key
        or intent is None
        or intent.status != "running"
    ):
        raise ValueError("child_approval_parent_effect_changed")
    # No tool is executed here. The child consumes the durable decision and
    # dispatches through its original definition/arguments and authorizer.
    receipt = handle.kernel.submit(
        approval.child_run_id,
        RunCommand(
            command_id=request.operation_key + ":child-approval",
            kind="submit_interaction",
            payload={
                "interaction_id": approval.interaction_id,
                "decision": request.interaction["decision"],
            },
        ),
    )
    if receipt.status not in {"accepted", "duplicate"}:
        raise ValueError("child_approval_command_rejected")
    handle.approval_decision.set_result(None)
    # A resolved pause must not wake the next observation repeatedly while
    # the child consumes its command. The next original pause sets it again.
    handle.approval_ready.clear()


async def observe_until_ready(handles):
    """Wait for all terminals, or a real approval that needs the parent's UI."""
    waiters = []
    try:
        for handle in handles:
            waiters.append(asyncio.create_task(handle.approval_ready.wait()))
        while any(not handle.task.done() for handle in handles):
            if any(current_approval(handle) is not None for handle in handles):
                return
            live = [handle.task for handle in handles if not handle.task.done()]
            await asyncio.wait((*live, *waiters), return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
        if waiters:

            async def join():
                await asyncio.gather(*waiters, return_exceptions=True)

            await _settle(join(), propagate_cancel=False)
