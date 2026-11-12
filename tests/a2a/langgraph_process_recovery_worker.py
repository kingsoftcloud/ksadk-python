"""Real LangGraph/PostgreSQL restart fixture; remote transport is synthetic."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import START, END, StateGraph, MessagesState
from sqlalchemy.ext.asyncio import create_async_engine

from ksadk.a2a.invocation_checkpoint import PostgresA2AInvocationCheckpointStore
from ksadk.a2a.invocation_policy import A2AInvocationPolicy, A2AInvocationPolicyDecision
from ksadk.a2a.invocation_service import A2AInvocationService
from ksadk.a2a.langgraph_managed import remote_node
from ksadk.events.run_authority import RunEventAuthority, bind_run_event_authority
from ksadk.events.session_event import SessionServiceEventStore
from ksadk.kernel.contracts import ActivationWriteGuard
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.sessions import bind_session_service
from ksadk.sessions.local_service import LocalSessionService
from tests.a2a.test_invocation_service_r85 import Wire, request


async def main():
    root = Path(os.environ['R86_LG_DIRECTORY'])
    stage = os.environ['R86_LG_STAGE']
    schema = os.environ['R86_LG_SCHEMA']
    dsn = os.environ['KSADK_A2A_TEST_DATABASE_URL']
    sessions = LocalSessionService(root / 'canonical.sqlite')
    if stage == 'start':
        await sessions.create_session('caller', 'user', 's')
    authority = RunEventAuthority(
        SessionServiceEventStore(sessions), session_id='s', run_id='public-run',
        guard=ActivationWriteGuard(activation_id=stage, fencing_token=1 if stage == 'start' else 2),
        interaction_ledger=object(), tenant_id='tenant', agent_instance_id='caller',
    )
    engine = create_async_engine(dsn, connect_args={'server_settings': {'search_path': schema}})
    checkpoints = PostgresA2AInvocationCheckpointStore(
        engine, account_id='fixture', tenant_id='tenant', runtime_id='caller',
    )
    await checkpoints.initialize()
    wire = Wire()
    service = A2AInvocationService(
        space_client=wire, event_store=authority, checkpoint_store=checkpoints,
        policy=A2AInvocationPolicy(evaluator=lambda *_: A2AInvocationPolicyDecision(
            'approval_required' if stage == 'start' else 'allow'
        )),
    )
    invoked = []
    original_invoke = service.invoke
    async def capture_invoke(value, **kwargs):
        invoked.append(value.model_dump(mode='json'))
        async for event in original_invoke(value, **kwargs):
            yield event
    service.invoke = capture_invoke
    plain = dsn.replace('postgresql+asyncpg://', 'postgresql://', 1)
    plain += '?options=-csearch_path%3D' + schema
    async with AsyncPostgresSaver.from_conn_string(plain) as saver:
        await saver.setup()
        builder = StateGraph(MessagesState)
        builder.add_node('expert', remote_node(request().spec))
        builder.add_edge(START, 'expert')
        builder.add_edge('expert', END)
        graph = builder.compile(checkpointer=saver)
        runner = LangGraphRunner(SimpleNamespace(entry_point='agent.py', agent_variable='root_agent'), '.')
        runner._agent = graph
        runner._managed_a2a_bootstrap = SimpleNamespace(
            outbound_enabled=True, runtime_metadata=SimpleNamespace(agent_id='caller'),
            invocation_service_for_space=lambda _space, **_deps: service,
        )
        payload = {'input': 'hello'}
        if stage == 'resume':
            previous = json.loads((root / 'state.json').read_text())
            payload = {'input': None, 'checkpoint_resume': True, '_ksadk_managed_takeover': True,
                       'framework_ref': {'langgraph': previous['checkpoint']}}
        output = []
        with bind_session_service(sessions), bind_run_event_authority(authority):
            async for event in runner.stream_canonical_events({
                'run_id': 'public-run', 'session_id': 's', **payload,
            }):
                # Production RunnerRuntimeAdapter owns the root run.started.
                if event.event_type == 'run.started':
                    continue
                try:
                    await authority.persist_one('s', event)
                except ValueError:
                    print('Collision candidate:', event.model_dump(mode='json'), flush=True)
                    raise
                if stage == 'start' and event.event_type == 'interaction.requested':
                    config = await runner._get_session_config('s')
                    state = await graph.aget_state(config)
                    ref = dict(state.config['configurable'])
                    assert ref.get('checkpoint_id') and state.next == ('expert',)
                    assert not wire.calls
                    (root / 'state.json').write_text(json.dumps({
                        'checkpoint': ref, 'request': event._invocation.model_dump(mode='json'),
                        'tasks': [str(t.id) for t in state.tasks],
                    }))
                    os._exit(23)
                output.append(event.model_dump(mode='json'))
        from ksadk.a2a.invocation_types import A2AInvocationRequest
        original = A2AInvocationRequest.model_validate(previous['request'])
        recovered = await service.original_request(original.handle)
        (root / 'recovered.json').write_text(json.dumps({
            'request': recovered.model_dump(mode='json'), 'invoked': invoked,
            'sends': [message.message_id for kind, message in wire.calls if kind == 'send'],
            'output': output,
        }))
    await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
