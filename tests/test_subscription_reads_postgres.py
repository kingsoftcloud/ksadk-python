"""Real HTTP reconnect over real PG must not scan unrelated session history."""
import asyncio
import json
import os
import socket
import uuid

import httpx
import pytest
import uvicorn

from ksadk.sessions.postgres_service import PostgresSessionService
from ksadk.sessions.resilient import ResilientSessionService


@pytest.mark.asyncio
async def test_subscription_uses_scoped_keyset_pages_and_preserves_owner(monkeypatch):
    from ksadk.kernel import bootstrap
    from ksadk.server.composition import configure_runtime_app
    from ksadk.server.factory import RuntimeAppConfig, create_runtime_app
    from ksadk.server.routes.projection import _latest_invocation_status
    dsn = os.getenv('KSADK_TEST_POSTGRES_DSN')
    assert dsn, 'Required real PostgreSQL DSN missing'
    pg = PostgresSessionService(dsn=dsn, namespace='subscribe_' + uuid.uuid4().hex)
    await pg.create_session('agent', 'owner', 'session')
    async with pg._connection(write=True) as conn:
        await conn.execute('''INSERT INTO ksadk_events
            (namespace, session_id, id, author, event_type, content_json, timestamp, seq_id, invocation_id)
            SELECT $1, 'session', 'event-' || i, 'agent',
                   CASE WHEN i=100 THEN 'run_status' ELSE 'assistant_message' END,
                   CASE WHEN i=100 THEN '{"status":"completed"}'::jsonb
                        ELSE jsonb_build_object('text',repeat('x',1024)) END,
                   10001-i, i, CASE WHEN i<=100 THEN 'target' ELSE 'unrelated' END
            FROM generate_series(1,10000) AS i''', pg.namespace)
    service = ResilientSessionService(pg)
    async def forbidden(*args, **kwargs):
        raise AssertionError('Subscription must not count or restore the complete session')
    monkeypatch.setattr(service, 'get_session', forbidden)
    monkeypatch.setattr(service, 'get_events', forbidden)
    monkeypatch.setattr(service, 'count_events', forbidden)
    query = service.query_events
    fetched = []
    async def observe(request):
        assert request.session_ids == ['session'] and request.invocation_id
        assert request.offset == 0 and request.order_by_seq
        rows = await query(request)
        fetched.append(len(rows))
        return rows
    monkeypatch.setattr(service, 'query_events', observe)
    monkeypatch.setattr(bootstrap, 'get_agent_kernel_runtime', lambda: None)
    app = create_runtime_app(RuntimeAppConfig(route_groups={'run'}, session_service_provider=lambda: service), configure_runtime_app)
    sock = socket.socket(); sock.bind(('127.0.0.1', 0)); sock.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started: await asyncio.sleep(.01)
    params = {'SessionId':'session', 'InvocationId':'target', 'AgentId':'agent', 'UserId':'owner'}
    try:
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{sock.getsockname()[1]}', trust_env=False, timeout=5) as h:
            path = '/agentengine/api/v1/SubscribeRunEvents'
            for wrong in [{'UserId':'foreign'}, {'AgentId':'foreign'}]:
                assert (await h.get(path, params={**params, **wrong})).status_code in (403,404)
            assert (await h.get(path, params={**params,'InvocationId':'absent'})).status_code == 409
            first = []
            async with h.stream('GET', path, params=params) as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if line.startswith('data: ') and line != 'data: [DONE]':
                        first.append(json.loads(line[6:]))
                    if len(first) == 6: break
            resumed = await h.get(path, params={**params, 'AfterSeqId':first[-1]['SeqId']})
            assert resumed.status_code == 200 and 'data: [DONE]' in resumed.text
            tail = [json.loads(line[6:]) for line in resumed.text.splitlines()
                    if line.startswith('data: ') and line != 'data: [DONE]']
            assert [e['SeqId'] for e in first + tail] == list(range(1,101))
            assert all(e['InvocationId']=='target' and e['SessionId']=='session' for e in first + tail)
        assert await _latest_invocation_status(service, 'session', 'target') == 'completed'
        assert not service._hydrated_session_ids
        assert max(fetched) <= 32 and sum(fetched) < 200
    finally:
        server.should_exit = True
        await serving
        await pg.delete_session('session')
        await service.aclose()
