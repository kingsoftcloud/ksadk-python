"""Same-session A2A/plain LangGraph turns and history after a transient read fault.

Uses actual LangGraph/tool/approval/Kernel execution and real PostgreSQL, with a
local deterministic model and A2A wire; no cloud resource or model is contacted.
"""
import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

from ksadk.a2a.invocation_service import A2AInvocationService
from ksadk.kernel.bootstrap import build_agent_kernel_runtime
from ksadk.kernel.contract_fingerprints import runtime_capability_matrix_digest
from ksadk.kernel.postgres_store import PostgresFencedSessionEventStore
from ksadk.kernel.state import RunState
from ksadk.runners.langgraph_runner import LangGraphRunner
from ksadk.runtime.framework_adapters import LangGraphRuntimeAdapter
from ksadk.server.routes.models import ListSessionMessagesActionRequest
from ksadk.server.routes.sessions import _project_session_message_page
from ksadk.sessions.invocation_identity import PlatformIdentityContext
from ksadk.sessions.postgres_service import PostgresSessionService
from ksadk.toolsets.a2a import get_a2a_tools
from tests.a2a.test_invocation_service_r85 import Wire, request
from tests.kernel.control_harness import AGENT, TENANT, command, kernel_stack
from tests.kernel.test_postgres_store import pg_dsn, pg_store  # noqa: F401
from tests.kernel.test_production_bootstrap import _runtime_config
from tests.runners.test_langchain_hitl_langgraph import SeqToolModel
from tests.kernel.test_postgres_store import kernel_postgres_server_dsn  # noqa: F401


class RecordingModel(SeqToolModel):
    seen: list = []

    def _stream(self, messages, *args, **kwargs):
        self.seen.append(list(messages))
        yield from super()._stream(messages, *args, **kwargs)


async def until(read, check):
    async with asyncio.timeout(8):
        while True:
            value = await read()
            if check(value):
                return value
            await asyncio.sleep(.01)


@pytest.fixture
def postgres_case(backend, request):
    if backend == 'postgres':
        return request.getfixturevalue('pg_store'), request.getfixturevalue('pg_dsn')
    return None, None


@pytest.mark.parametrize('managed', [False, True], ids=['plain', 'a2a'])
@pytest.mark.parametrize('backend', ['memory', 'postgres'])
async def test_followup_after_transient_read_keeps_context_and_history(
    managed, backend, postgres_case, monkeypatch,
):
    monkeypatch.setenv('KSADK_TOOL_APPROVAL_MODE', 'full')
    spec = request().spec
    monkeypatch.setenv('KSADK_A2A_SPACE_ID', spec.space_id)
    first = AIMessage(content='', tool_calls=[{
        'name': 'call_a2a_agent', 'id': 'call-1',
        'args': {'agent': 'expert', 'message': 'remember-123'},
    }]) if managed else AIMessage(content='first-answer')
    model = RecordingModel(msgs=[first, AIMessage(content='first-answer'),
                                AIMessage(content='followup-answer')]
                           if managed else [first, AIMessage(content='followup-answer')])
    runner = LangGraphRunner(SimpleNamespace(entry_point='agent.py', agent_variable='root_agent'), '.')
    runner._agent = create_react_agent(model, get_a2a_tools() if managed else [],
                                     checkpointer=MemorySaver())
    adapter = LangGraphRuntimeAdapter(runner)
    wire = Wire()
    if managed:
        async def discover():
            return [SimpleNamespace(agent_id=spec.target_agent_id,
                                    version_id=spec.expected_version_id,
                                    agent_card=SimpleNamespace(name='expert', description='local'))]
        adapter.configure_managed_a2a(SimpleNamespace(
            outbound_enabled=True, runtime_metadata=SimpleNamespace(agent_id='caller'),
            client_for_space=lambda _: SimpleNamespace(discover=discover),
            invocation_service_for_space=lambda _, **deps: A2AInvocationService(space_client=wire, **deps),
        ))
    stack = await kernel_stack(adapter=adapter)
    service = None
    if backend == 'postgres':
        pg_store, pg_dsn = postgres_case
        service = PostgresSessionService(dsn=pg_dsn)
        stack.store, stack.events, stack.session_service = (
            pg_store, PostgresFencedSessionEventStore(pg_store), service,
        )
    runtime = build_agent_kernel_runtime(_runtime_config(
        stack, capability_digest=runtime_capability_matrix_digest(adapter.capabilities()),
    ))
    monkeypatch.setattr('ksadk.kernel.bootstrap.get_agent_kernel_runtime', lambda: runtime)
    try:
        await runtime.start()
        receipt = await runtime.kernel.submit(command(idempotency_key='turn-1', payload={
            'content': 'remember-123', 'runtime_options': {'tool_approval_mode': 'ask'},
        }), permit=stack.permit('enqueue'))
        if managed:
            records = await until(lambda: stack.store.list_pending_interactions(TENANT, 's1'), bool)
            record = records[0]
            approved = await runtime.kernel.submit(command('submit_interaction', idempotency_key='approval', payload={
                'run_id': record.run_id, 'interaction_id': record.interaction_id,
                'expected_revision': record.revision, 'token_ref': 'local-test',
                'action': 'approve', 'response': {},
            }), permit=stack.permit('submit_interaction'))
            assert approved.status == 'accepted'
        async def runs():
            messages = await stack.store.list_messages(AGENT, 's1')
            records = []
            for message in messages:
                if message.command.command_type == 'enqueue':
                    run = await stack.store.find_command_run(AGENT, 's1', str(message.command.command_id))
                    if run is not None:
                        records.append(run)
            return records
        await until(runs, lambda rows: len(rows) == 1 and rows[0].state is RunState.COMPLETED)
        original = runtime.worker._session_continuation_metadata
        reads = 0
        async def flaky(*args, **kwargs):
            nonlocal reads
            reads += 1
            if reads == 1:
                raise TimeoutError('database command deadline exceeded')
            return await original(*args, **kwargs)
        monkeypatch.setattr(runtime.worker, '_session_continuation_metadata', flaky)
        await runtime.kernel.submit(command(idempotency_key='turn-2', payload={'content': 'recall-123'}),
                                    permit=stack.permit('enqueue'))
        completed = await until(runs, lambda rows: len(rows) == 2 and all(r.state is RunState.COMPLETED for r in rows))
        assert reads >= 2 and not runtime.quarantined_sessions()
        assert len(completed) == 2  # retry did not create an extra provider run
        assert 'remember-123' in str(model.seen[-1]) and 'recall-123' in str(model.seen[-1])
        assert sum(kind == 'send' for kind, _ in wire.calls) == int(managed)
        result = await _project_session_message_page(
            ListSessionMessagesActionRequest(SessionId='s1', AgentId=AGENT, Limit=50),
            stack.session_service, PlatformIdentityContext(tenant_id=TENANT), 'test-epoch', False,
            await stack.session_service.get_max_seq_id('s1'),
        )
        visible = str(result['Data']['Messages'])
        assert all(text in visible for text in ['remember-123', 'recall-123', 'followup-answer'])
    finally:
        await runtime.close()
        if service is not None:
            await service.aclose()
