"""Rejecting nested approval closes the remote task without a continuation."""
import pytest
from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus
from ksadk.a2a.interaction_wire import approval_metadata
from ksadk.a2a.runtime_input import ManagedA2AInputRequested, invoke_with_input
from ksadk.events.canonical import ApprovalRequest
from ksadk.interaction.contracts import InteractionReceipt
from tests.a2a.test_invocation_service_r85 import Wire, request, service, trigger


@pytest.mark.asyncio
@pytest.mark.parametrize('supported', [True, False])
async def test_rejection_cancels_remote_without_sending_approval_or_claiming_unknown_success(supported):
    task = Task(id='remote-1', context_id='context-1', status=TaskStatus(
        state=TaskState.TASK_STATE_INPUT_REQUIRED,
        message=Message(message_id='approval', role=Role.ROLE_AGENT, parts=[Part(text='Permit?')],
            metadata=approval_metadata(ApprovalRequest(kind='tool', detail={'name': 'expert'})))))

    class CancelWire(Wire):
        async def cancel_prepared_task(self, prepared):
            self.calls.append(('cancel', prepared))
            if supported:
                self.task.status.state = TaskState.TASK_STATE_CANCELED
            return self.task

    wire = CancelWire(events=[task], task=task)
    from ksadk.a2a.invocation_policy import A2AInvocationPolicy, A2AInvocationPolicyDecision
    from ksadk.a2a.runtime_approval import approval_target
    req = request()
    def approved_exact_request(candidate, route):
        return A2AInvocationPolicyDecision(
            "allow" if approval_target(candidate) == approval_target(req) else "approval_required")
    svc, _, _ = await service(wire=wire, policy=A2AInvocationPolicy(evaluator=approved_exact_request))
    stream = invoke_with_input(svc, req, trigger_ref=trigger())
    try:
        async for event in stream:
            if isinstance(event, ManagedA2AInputRequested):
                event._managed_input_waiter.deliver(InteractionReceipt(
                    interaction_id=event.interaction_id, revision=2, status='resolved', outcome='rejected'))
                break
        with pytest.raises(PermissionError, match='A2A_INPUT_REJECTED'):
            _ = [event async for event in stream]
        assert sum(k == 'send' for k, _ in wire.calls) == 1
        assert sum(k == 'cancel' for k, _ in wire.calls) == 1
        snapshot = svc.snapshot(svc.handle(req.scope_id))
        assert snapshot.status == ('cancelled' if supported else 'input_required')
    finally:
        await stream.aclose()
