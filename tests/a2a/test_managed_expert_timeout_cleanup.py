import asyncio
import time

import pytest
from a2a.types import Message, Part, Role, Task, TaskState, TaskStatus

from ksadk.a2a.langgraph_managed import bind_managed_langgraph, remote_node
from tests.a2a.test_invocation_service_r85 import Wire, request, service, trigger


@pytest.mark.asyncio
@pytest.mark.parametrize('framework', ['langgraph', 'codex'])
async def test_input_deadline_cancels_original_bound_task_without_claiming_success(framework):
    wire = Wire(task=Task(id='remote-1', context_id='context-1', status=TaskStatus(
        state=TaskState.TASK_STATE_INPUT_REQUIRED,
        message=Message(message_id='question', role=Role.ROLE_AGENT,
                        parts=[Part(text='Please provide input')]),
    )))
    cancelled = []

    async def cancel(prepared):
        cancelled.append(prepared.platform_task_id)
        # A cancellation request is not proof of remote cancellation.
        return wire.task

    wire.cancel_prepared_task = cancel
    req = request()
    req = req.model_copy(update={'deadline_unix_seconds': time.time() + 0.3,
                                'spec': req.spec.model_copy(update={'timeout_seconds': 0.3})})
    if framework == 'langgraph':
        svc, _, _ = await service(wire=wire)
        with bind_managed_langgraph(svc, lambda *_: (req, trigger()), lambda _: None):
            with pytest.raises(TimeoutError, match='A2A_OVERALL_TIMEOUT'):
                await asyncio.wait_for(remote_node(req.spec)({}, {}), 3)
    else:
        from ksadk.a2a.codex import CodexInvocationContextProvider, codex_expert
        from ksadk.a2a.invocation_policy import A2AInvocationPolicy, A2AInvocationPolicyDecision
        from ksadk.a2a.invocation_service import A2AInvocationService
        from ksadk.events.run_authority import bind_run_event_authority
        from tests.a2a.test_codex_expert import call, persist_call, setup_runtime
        authority, _, _, _ = await setup_runtime()
        svc = A2AInvocationService(space_client=wire, event_store=authority,
            policy=A2AInvocationPolicy(evaluator=lambda *_: A2AInvocationPolicyDecision('allow')))
        provider = CodexInvocationContextProvider(authority=authority,
            service_for_space=lambda _: svc, caller_agent_id='caller')
        with bind_run_event_authority(authority):
            await persist_call(authority)
            with pytest.raises(TimeoutError, match='A2A_OVERALL_TIMEOUT'):
                await asyncio.wait_for(codex_expert(req.spec).invoke(call(), provider=provider,
                                                                  emit=lambda _: None), 3)
    assert cancelled == ['a2a-task-00000000000040008000000000000001']
    assert sum(kind == 'send' for kind, _ in wire.calls) == 1
    assert wire.binding.remote_task_id == 'remote-1'
