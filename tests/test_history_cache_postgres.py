"""History coalescing and transport recovery against a required real PG."""
import asyncio
import json
import os
import socket
import uuid

import asyncpg
import httpx
import pytest
import pytest_asyncio
import uvicorn

from ksadk.server.history_page_cache import HistoryPageCache
from ksadk.sessions.base import SessionEvent
from ksadk.sessions.db_budget import DatabaseBudget
from ksadk.sessions.postgres_service import PostgresSessionService
from ksadk.sessions.resilient import ResilientSessionService


@pytest.fixture
def dsn():
    value = os.getenv("KSADK_TEST_POSTGRES_DSN")
    assert value, "KSADK_TEST_POSTGRES_DSN is required; no skipped database acceptance"
    return value


@pytest_asyncio.fixture
async def pg(dsn, request, monkeypatch):
    if hasattr(request, 'param'):
        monkeypatch.setenv('KSADK_HISTORY_READ_CONCURRENCY', str(request.param))
    service = PostgresSessionService(dsn=dsn, namespace="history_cache_" + uuid.uuid4().hex,
                                    min_size=1, max_size=4)
    await service.create_session("agent", "user", "session")
    yield service
    await service.delete_session("session")
    await service.aclose()


@pytest.mark.asyncio
async def test_real_pg_coalesces_and_one_waiter_cancel_does_not_cancel_other(pg):
    cache = HistoryPageCache()
    entered, release = asyncio.Event(), asyncio.Event()
    queries = 0
    async def produce():
        nonlocal queries
        queries += 1
        async with pg._connection() as conn:
            entered.set()
            await release.wait()
            value = await conn.fetchval("SELECT 41 + 1")
        return {"value": value}
    first = asyncio.create_task(cache.read("scope-snapshot", produce))
    await entered.wait()
    second = asyncio.create_task(cache.read("scope-snapshot", produce))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError): await first
    release.set()
    assert json.loads(await second) == {"value": 42}
    assert json.loads(await cache.read("scope-snapshot", produce)) == {"value": 42}
    assert queries == 1 and not cache.pending


@pytest.mark.asyncio
async def test_cancel_last_waiter_reaps_real_query_and_reuses_pool(pg):
    cache = HistoryPageCache()
    entered = asyncio.Event()
    async def produce():
        async with pg._connection() as conn:
            entered.set()
            await conn.fetchval("SELECT pg_sleep(10)")
        return {}
    task = asyncio.create_task(cache.read("scope", produce))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert not cache.pending
    async with pg._connection() as conn:
        assert await conn.fetchval("SELECT 1") == 1
    assert pg._pool.get_idle_size() == pg._pool.get_size()


@pytest.mark.asyncio
async def test_invocation_boundary_reads_only_small_adjacent_page(pg, monkeypatch):
    from ksadk.server.routes.projection import _extend_invocation_before_window
    from ksadk.sessions._postgres_tables import KSADK_PG_EVENTS_TABLE
    async with pg._connection(write=True) as connection:
        await connection.execute(f'''INSERT INTO {KSADK_PG_EVENTS_TABLE}
            (namespace, session_id, id, author, event_type, content_json, timestamp, seq_id, invocation_id)
            SELECT $1, 'session', 'event-' || i, 'agent', 'assistant_message',
                   jsonb_build_object('text', repeat('x', 1024)), extract(epoch from now()), i,
                   'run-' || ((i-1)/90) FROM generate_series(1,900) AS i''', pg.namespace)
    events = await pg.get_events('session', limit=256)
    original = pg.get_events
    reads = []
    async def read(session_id, **kwargs):
        rows = await original(session_id, **kwargs)
        reads.append((kwargs['limit'], len(rows)))
        return rows
    monkeypatch.setattr(pg, 'get_events', read)
    complete = await _extend_invocation_before_window(pg, 'session', events, after_seq_id=None)
    assert [event.seq_id for event in complete] == list(range(631,901))
    assert reads == [(64,64)]


@pytest.mark.asyncio
async def test_snapshot_page_survives_session_switch_delay_then_expires(pg, monkeypatch):
    import ksadk.server.history_page_cache as module
    clock = [100.0]
    monkeypatch.setattr(module, 'time', type('Clock', (), {'monotonic': staticmethod(lambda: clock[0])}))
    cache = HistoryPageCache()
    reads = 0
    async def produce():
        nonlocal reads
        reads += 1
        async with pg._connection() as conn:
            return {'value': await conn.fetchval('SELECT 42')}
    await cache.read(('owner', 'epoch', 1), produce)
    clock[0] += 10
    await cache.read(('owner', 'epoch', 1), produce)
    assert reads == 1
    await cache.read(('owner', 'epoch', 2), produce)
    assert reads == 2  # An append changes the watermark even inside the TTL.
    clock[0] += 21
    await cache.read(('owner', 'epoch', 1), produce)
    assert reads == 3


@pytest.mark.asyncio
async def test_terminated_read_reacquires_once_without_global_degradation(dsn):
    ready = asyncio.get_running_loop().create_future()
    class InterruptedRead(PostgresSessionService):
        calls = 0
        async def get_session_header(self, session_id):
            self.calls += 1
            if self.calls == 1:
                async with self._connection() as conn:
                    ready.set_result(await conn.fetchval("SELECT pg_backend_pid()"))
                    await conn.fetchval("SELECT pg_sleep(20)")
            return await super().get_session_header(session_id)
    primary = InterruptedRead(dsn=dsn, namespace="reconnect_" + uuid.uuid4().hex,
                              db_budget=DatabaseBudget(read_sql_seconds=3))
    await primary.create_session("agent", "user", "session")
    resilient = ResilientSessionService(primary)
    try:
        reading = asyncio.create_task(resilient.get_session_header("session"))
        pid = await ready
        terminator = await asyncpg.connect(dsn)
        try:
            assert await terminator.fetchval("SELECT pg_terminate_backend($1)", pid)
        finally: await terminator.close()
        assert (await reading).id == "session"
        assert primary.calls == 2 and resilient._primary_enabled
        assert not resilient._dirty_session_ids and resilient._probe_task is None
    finally:
        await primary.delete_session("session")
        await resilient.aclose()


@pytest.mark.asyncio
async def test_read_pool_exhaustion_does_not_disable_all_sessions(pg):
    resilient = ResilientSessionService(pg)
    holders = [await pg._pool.acquire() for _ in range(4)]
    try:
        with pytest.raises(TimeoutError):
            await resilient.get_session_header("session")
        assert resilient._primary_enabled and not resilient._dirty_session_ids
    finally:
        for conn in holders: await pg._pool.release(conn)
    assert (await resilient.get_session_header("session")).id == "session"


@pytest.mark.asyncio
async def test_projection_scope_reuses_read_lease_but_not_child_or_write(pg, monkeypatch):
    import ksadk.sessions.postgres_service as module
    original_release = module.release_pool_lease
    releases = []

    async def release(*args):
        releases.append(args[2])
        return await original_release(*args)

    monkeypatch.setattr(module, 'release_pool_lease', release)
    async with pg.history_read_scope():
        async with pg._connection() as first:
            pid = await first.fetchval('SELECT pg_backend_pid()')
        async with pg.history_read_scope():
            async with pg._connection() as second:
                assert second is first
                assert await second.fetchval('SELECT pg_backend_pid()') == pid
        assert releases == []

        async def child():
            async with pg._connection() as conn:
                return await conn.fetchval('SELECT pg_backend_pid()')
        assert await asyncio.create_task(child()) != pid
        async with pg._connection(write=True) as writer:
            assert await writer.fetchval('SELECT pg_backend_pid()') != pid
        assert len(releases) == 2
    assert len(releases) == 3
    assert pg._pool.get_idle_size() == pg._pool.get_size()


@pytest.mark.asyncio
@pytest.mark.parametrize('terminate', [False, True])
async def test_projection_scope_cancellation_or_disconnect_releases_lease(pg, dsn, terminate):
    ready = asyncio.get_running_loop().create_future()

    async def project():
        async with pg.history_read_scope():
            async with pg._connection() as conn:
                ready.set_result(await conn.fetchval('SELECT pg_backend_pid()'))
                try:
                    await conn.fetchval('SELECT pg_sleep(10)')
                except asyncpg.PostgresConnectionError:
                    if not terminate:
                        raise
                    # The existing read recovery must obtain a fresh lease.
                    async with pg._connection() as replacement:
                        assert await replacement.fetchval('SELECT 42') == 42

    task = asyncio.create_task(project())
    pid = await ready
    if terminate:
        conn = await asyncpg.connect(dsn)
        try:
            assert await conn.fetchval('SELECT pg_terminate_backend($1)', pid)
        finally:
            await conn.close()
        await task
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with pg._connection() as conn:
        assert await conn.fetchval('SELECT 1') == 1
        assert not await conn.fetchval(
            'SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=$1 AND state=\'active\' AND pid<>pg_backend_pid())', pid)
    assert pg._pool.get_idle_size() == pg._pool.get_size()
    assert pg._history_read_lease.get() is None


@pytest.mark.asyncio
async def test_cold_http_page_completes_many_windows_with_two_pool_resets(pg, monkeypatch):
    import ksadk.sessions.postgres_service as module
    from ksadk.server.composition import configure_runtime_app
    from ksadk.server.factory import RuntimeAppConfig, create_runtime_app
    from ksadk.server.routes import sessions as routes
    from ksadk.kernel import bootstrap
    async with pg._connection(write=True) as conn:
        await conn.execute('''INSERT INTO ksadk_events
            (namespace, session_id, id, author, event_type, content_json,
             timestamp, seq_id, invocation_id)
            SELECT $1, 'session', 'scope-' || i, 'agent', 'assistant_message',
                   jsonb_build_object('text', repeat('x', 20000)), i, i, 'one-run'
            FROM generate_series(1,90) i''', pg.namespace)
    releases, windows = [], []
    original_release, original_window = module.release_pool_lease, pg.get_message_event_window
    async def release(*args):
        releases.append(args[2])
        return await original_release(*args)
    async def window(*args, **kwargs):
        rows = await original_window(*args, **kwargs)
        windows.append([row.seq_id for row in rows])
        return rows
    monkeypatch.setattr(module, 'release_pool_lease', release)
    monkeypatch.setattr(pg, 'get_message_event_window', window)
    monkeypatch.setattr(routes, 'get_runtime_execution', lambda: (None, None))
    monkeypatch.setattr(bootstrap, 'get_agent_kernel_runtime', lambda: None)
    service = ResilientSessionService(pg)
    app = create_runtime_app(RuntimeAppConfig(
        route_groups={'sessions'}, session_service_provider=lambda: service,
    ), configure_runtime_app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture') as client:
        response = await client.post('/agentengine/api/v1/ListSessionMessages', json={
            'AgentId': 'agent', 'SessionId': 'session', 'UserId': 'user', 'Limit': 50,
        })
    assert response.status_code == 200 and response.json()['Code'] == 0
    assert sorted(seq for page in windows for seq in page) == list(range(1, 91))
    assert len(windows) > 20
    assert len(releases) == 2  # One ownership/header lookup, one complete page.
    assert pg._pool.get_idle_size() == pg._pool.get_size()


@pytest.mark.asyncio
@pytest.mark.parametrize('pg', [2], indirect=True)
async def test_eight_scoped_readers_bound_real_pg_transfers_without_changing_results(pg, monkeypatch):
    from ksadk.sessions.postgres_service import _TimedConnection
    await pg.append_event('session', SessionEvent(
        id='transfer-input', event_type='user_message', content={'text': 'original input'},
    ))
    fetch = _TimedConnection.fetch
    active = maximum = reads = 0
    async def delayed(connection, query, *args, **kwargs):
        nonlocal active, maximum, reads
        if 'source_exhausted' not in query:
            return await fetch(connection, query, *args, **kwargs)
        active += 1
        maximum = max(maximum, active)
        reads += 1
        try:
            await fetch(connection, 'SELECT pg_sleep(0.04)')
            return await fetch(connection, query, *args, **kwargs)
        finally:
            active -= 1
    monkeypatch.setattr(_TimedConnection, 'fetch', delayed)
    async def page():
        async with pg.history_read_scope():
            return await pg.get_message_event_window('session', limit=256)
    results = await asyncio.gather(*(page() for _ in range(8)))
    assert reads == 8 and maximum == 2 and active == 0
    assert all([row.id for row in rows] == ['transfer-input'] for rows in results)
    assert all(rows[0].content == {'text': 'original input'} for rows in results)
    assert pg._pool.get_idle_size() == pg._pool.get_size()


@pytest.mark.asyncio
@pytest.mark.parametrize('pg', [2], indirect=True)
async def test_cancel_queued_transfer_does_not_run_sql_or_leak_permit(pg, monkeypatch):
    from ksadk.sessions.postgres_service import _TimedConnection
    fetch = _TimedConnection.fetch
    pids = []
    entered = asyncio.Event()
    async def slow(connection, query, *args, **kwargs):
        if 'source_exhausted' not in query:
            return await fetch(connection, query, *args, **kwargs)
        pids.append(await connection.fetchval('SELECT pg_backend_pid()'))
        if len(pids) == 2:
            entered.set()
        return await fetch(connection, 'SELECT pg_sleep(10)')
    monkeypatch.setattr(_TimedConnection, 'fetch', slow)
    async def page():
        async with pg.history_read_scope():
            return await pg.get_message_event_window('session', limit=256)
    running = [asyncio.create_task(page()) for _ in range(2)]
    await asyncio.wait_for(entered.wait(), 1)
    queued = asyncio.create_task(page())
    for _ in range(100):
        if pg._pool.get_size() - pg._pool.get_idle_size() == 3:
            break
        await asyncio.sleep(.001)
    assert pg._pool.get_size() - pg._pool.get_idle_size() == 3
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert len(pids) == 2
    for task in running:
        task.cancel()
    await asyncio.gather(*running, return_exceptions=True)
    monkeypatch.setattr(_TimedConnection, 'fetch', fetch)
    assert await page() == []
    async with pg._connection() as conn:
        active = await conn.fetch(
            "SELECT pid,query FROM pg_stat_activity WHERE pid=ANY($1::int[]) AND state='active' AND pid<>pg_backend_pid()", pids)
        assert not active
    assert pg._pool.get_idle_size() == pg._pool.get_size()


@pytest.mark.asyncio
async def test_real_http_cache_still_authenticates_and_invalidates_append(pg, monkeypatch):
    from ksadk.server.composition import configure_runtime_app
    from ksadk.server.factory import RuntimeAppConfig, create_runtime_app
    from ksadk.server.routes import sessions as routes
    from ksadk.kernel import bootstrap
    from ksadk.server.history_page_cache import cache_for
    service = ResilientSessionService(pg)
    original_header = service.get_session_header_with_watermark
    header_reads = 0
    async def slow_header(session_id):
        nonlocal header_reads
        header_reads += 1
        async with pg._connection() as conn:
            await conn.fetchval("SELECT pg_sleep(0.1)")
        return await original_header(session_id)
    monkeypatch.setattr(service, "get_session_header_with_watermark", slow_header)
    await pg.append_event("session", SessionEvent(id="first", event_type="user_message", content={"text": "first"}))
    monkeypatch.setattr(routes, "get_runtime_execution", lambda: (None, None))
    monkeypatch.setattr(bootstrap, "get_agent_kernel_runtime", lambda: None)
    app = create_runtime_app(RuntimeAppConfig(route_groups={"sessions"}, session_service_provider=lambda: service), configure_runtime_app)
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); sock.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started: await asyncio.sleep(.01)
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", trust_env=False) as h:
            path = "/agentengine/api/v1/ListSessionMessages"
            body = {"AgentId": "agent", "SessionId": "session", "UserId": "user"}
            replies = await asyncio.gather(*[h.post(path, json=body) for _ in range(8)])
            assert all(r.status_code == 200 and r.json()["Code"] == 0 for r in replies)
            assert len({r.json()["RequestId"] for r in replies}) == 8
            assert header_reads == 1
            assert len(cache_for(service).pages) == 1
            mixed = await asyncio.gather(
                h.post(path, json=body),
                h.post(path, json={**body, "UserId": "foreign"}),
                h.post(path, json={**body, "AgentId": "foreign"}),
            )
            assert mixed[0].status_code == 200
            assert all(r.status_code in (403, 404) for r in mixed[1:])
            assert header_reads == 4  # Different callers never share authorization.
            await pg.append_event("session", SessionEvent(id="second", event_type="user_message", content={"text": "second"}))
            response = (await h.post(path, json=body)).json()
            assert [m["Content"]["text"] for m in response["Data"]["Messages"]] == ["first", "second"]
            # No completed authorization is reused even though a page is cached.
            async with pg._connection(write=True) as conn:
                await conn.execute("UPDATE ksadk_sessions SET user_id='new-owner' WHERE namespace=$1 AND id='session'", pg.namespace)
            revoked = await h.post(path, json=body)
            assert revoked.status_code in (403, 404)
            assert not cache_for(service).lookups
    finally:
        server.should_exit = True
        await serving


@pytest.mark.asyncio
async def test_metadata_lookup_only_coalesces_inflight_and_copies_results(pg):
    cache = HistoryPageCache()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def load():
        nonlocal calls
        calls += 1
        async with pg._connection() as conn:
            entered.set()
            await release.wait()
            title = await conn.fetchval("SELECT title FROM ksadk_sessions WHERE namespace=$1 AND id='session'", pg.namespace)
        return {"title": title}
    first = asyncio.create_task(cache.lookup(('tenant', 'owner', 'session'), load))
    await entered.wait()
    second = asyncio.create_task(cache.lookup(('tenant', 'owner', 'session'), load))
    third = asyncio.create_task(cache.lookup(('tenant', 'owner', 'session'), load))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError): await first
    release.set()
    a, b = await asyncio.gather(second, third)
    a['title'] = 'mutated'
    assert b['title'] != 'mutated' and calls == 1
    await cache.lookup(('tenant', 'owner', 'session'), load)
    assert calls == 2 and not cache.lookups and not cache.lookup_waiters


@pytest.mark.asyncio
async def test_metadata_lookup_cancel_last_waiter_reaps_query(pg):
    cache = HistoryPageCache()
    entered = asyncio.Event()
    async def load():
        async with pg._connection() as conn:
            entered.set()
            await conn.fetchval("SELECT pg_sleep(10)")
    task = asyncio.create_task(cache.lookup('scope', load))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert not cache.lookups and not cache.lookup_waiters
    async with pg._connection() as conn:
        assert await conn.fetchval("SELECT 42") == 42
    assert pg._pool.get_idle_size() == pg._pool.get_size()


@pytest.mark.asyncio
async def test_full_replay_hydrates_without_recopying_existing_history(pg, monkeypatch):
    async with pg._connection(write=True) as conn:
        await conn.execute("""INSERT INTO ksadk_events
            (namespace, session_id, id, author, event_type, content_json, timestamp, seq_id)
            SELECT $1, 'session', 'event-' || i, 'agent', 'assistant_message',
                   jsonb_build_object('text', repeat('x', 4096)), i, i
            FROM generate_series(1,1000) AS i""", pg.namespace)
    await pg.update_state(agent_id='agent', user_id='user', session_id='session', scope='session',
                          state_delta={'retained': {'value': 42}})
    service = ResilientSessionService(pg)
    first = await service.get_session('session')
    assert len(first.events) == 1000
    original = service.fallback.get_session
    full_copies = 0
    async def count_full(session_id):
        nonlocal full_copies
        full_copies += 1
        return await original(session_id)
    async def forbidden(*args, **kwargs):
        raise AssertionError('Hydration membership must not copy all event bodies')
    monkeypatch.setattr(service.fallback, 'get_session', count_full)
    monkeypatch.setattr(service.fallback, 'get_events', forbidden)
    again = await service.get_session('session')
    assert full_copies == 1
    assert [(e.id, e.seq_id, e.content) for e in again.events] == [(e.id, e.seq_id, e.content) for e in first.events]
    assert again.state == first.state == {'retained': {'value': 42}}
    again.events[0].content['text'] = 'caller mutation'
    again.state['retained']['value'] = 0
    isolated = await original('session')
    assert isolated.events[0].content['text'] == 'x' * 4096
    assert isolated.state['retained']['value'] == 42


@pytest.mark.asyncio
async def test_boundary_probe_uses_no_event_bodies_and_preserves_scope(pg, monkeypatch):
    from ksadk.sessions.history_budget import CURRENT_HISTORY_READ_BUDGET, HistoryReadBudget
    await pg.append_event('session', SessionEvent(id='large', content={'text': 'x' * 200000}))
    service = ResilientSessionService(pg)
    async def forbidden(*args, **kwargs):
        raise AssertionError('Boundary existence must not read event bodies')
    monkeypatch.setattr(pg, 'get_events', forbidden)
    monkeypatch.setattr(service, '_hydrate', forbidden)
    budget = HistoryReadBudget(maximum=1)
    token = CURRENT_HISTORY_READ_BUDGET.set(budget)
    try:
        assert await service.has_events('session', after_seq_id=0, before_seq_id=2)
        assert not await service.has_events('session', before_seq_id=1)
        assert not await service.has_events('session', after_seq_id=1)
        assert not await service.has_events('other', before_seq_id=2)
        assert budget.used == 0
    finally:
        CURRENT_HISTORY_READ_BUDGET.reset(token)
    await service.fallback.create_session('agent', 'user', 'session')
    await service.fallback.append_event('session', SessionEvent(id='live-1'))
    await service.fallback.append_event('session', SessionEvent(id='live-2'))
    service._dirty_session_ids.add('session')
    assert not await service.has_events('session', before_seq_id=1)
    assert await service.has_events('session', after_seq_id=1)
    service._deleted_session_ids.add('session')
    assert not await service.has_events('session')


@pytest.mark.asyncio
async def test_real_pg_small_history_transfers_complete_under_command_budget(pg):
    from ksadk.sessions.postgres_service import _TimedConnection
    query = 'SELECT i, pg_sleep(0.02) FROM generate_series(1,24) i'
    # The former transfer size fails the unchanged command deadline. This is
    # real PostgreSQL execution, not a sleep patched into the driver.
    with pytest.raises(TimeoutError):
        async with pg._connection() as conn:
            async with conn.transaction():
                timed = _TimedConnection(conn._connection, sql_seconds=0.4)
                await timed.fetch_history_page(query, batch_size=32)
    async with pg._connection() as conn:
        async with conn.transaction():
            timed = _TimedConnection(conn._connection, sql_seconds=0.4)
            rows = await timed.fetch_history_page(query)
        assert [r['i'] for r in rows] == list(range(1,25))
        assert await conn.fetchval('SELECT 42') == 42
    assert pg._pool.get_idle_size() == pg._pool.get_size()


@pytest.mark.asyncio
async def test_boundary_timeout_does_not_switch_clean_history_source(pg, monkeypatch):
    pg.db_budget = DatabaseBudget(read_sql_seconds=.05)
    async def slow_probe(*args, **kwargs):
        async with pg._connection() as conn:
            return await conn.fetchval('SELECT pg_sleep(5)')
    monkeypatch.setattr(pg, 'has_events', slow_probe)
    service = ResilientSessionService(pg)
    with pytest.raises(TimeoutError):
        await service.has_events('session')
    assert not service.degraded and not service._dirty_session_ids
    async with pg._connection() as conn:
        assert await conn.fetchval('SELECT 42') == 42


@pytest.mark.asyncio
@pytest.mark.parametrize('query_api', ['get_events', 'query_events'])
async def test_small_prechecked_pages_avoid_cursor_roundtrips(pg, monkeypatch, query_api):
    from ksadk.sessions.base import SessionEventQuery
    from ksadk.sessions.history_budget import CURRENT_HISTORY_READ_BUDGET, HistoryReadBudget
    import ksadk.sessions.postgres_service as module

    payload = '汉字🙂' * 100
    for index in range(24):
        await pg.append_event('session', SessionEvent(
            id=f'payload-{index}', content={'text': payload},
        ))
    original = module.bounded_driver_call
    cursor_calls = 0

    async def counted(awaitable, connection, seconds, cleanup_seconds=1.0):
        nonlocal cursor_calls
        if getattr(getattr(awaitable, 'cr_code', None), 'co_qualname', '') == 'Cursor.fetch':
            cursor_calls += 1
        return await original(awaitable, connection, seconds, cleanup_seconds)

    monkeypatch.setattr(module, 'bounded_driver_call', counted)
    budget = HistoryReadBudget()
    token = CURRENT_HISTORY_READ_BUDGET.set(budget)
    try:
        rows = (await pg.get_events('session', limit=24) if query_api == 'get_events'
                else await pg.query_events(SessionEventQuery(
                    session_ids=['session'], limit=24, order_by_seq=True)))
        assert [row.seq_id for row in rows] == list(range(1, 25))
        assert all(row.content == {'text': payload} for row in rows)
        assert cursor_calls == 0, 'A small prechecked body needs no cursor protocol round trips'
        assert budget.used >= 24 * (1024 + len(payload.encode('utf-8')))
    finally:
        CURRENT_HISTORY_READ_BUDGET.reset(token)


@pytest.mark.asyncio
async def test_large_history_cursor_grows_without_losing_rows_or_payload(pg, monkeypatch):
    from ksadk.sessions.postgres_service import _TimedConnection
    import ksadk.sessions.postgres_service as module

    original = module.bounded_driver_call
    cursor_calls = 0

    async def counted(awaitable, connection, seconds, cleanup_seconds=1.0):
        nonlocal cursor_calls
        if getattr(getattr(awaitable, 'cr_code', None), 'co_qualname', '') == 'Cursor.fetch':
            cursor_calls += 1
        return await original(awaitable, connection, seconds, cleanup_seconds)

    monkeypatch.setattr(module, 'bounded_driver_call', counted)
    async with pg._connection() as connection:
        async with connection.transaction(isolation='repeatable_read', readonly=True):
            timed = _TimedConnection(connection._connection, sql_seconds=1)
            rows = await timed.fetch_history_page(
                "SELECT i, repeat('abc', 500) AS body FROM generate_series(1,256) AS i",
            )
        assert [row['i'] for row in rows] == list(range(1, 257))
        assert all(row['body'] == 'abc' * 500 for row in rows)
        assert cursor_calls < 16, 'Fixed eight-row transfers require 33 round trips'
        assert await connection.fetchval('SELECT 42') == 42
    assert pg._pool.get_idle_size() == pg._pool.get_size()


@pytest.mark.asyncio
@pytest.mark.parametrize('forward', [False, True])
async def test_invocation_boundary_stops_after_exhausting_snapshot(pg, monkeypatch, forward):
    from ksadk.server.routes.projection import (
        _extend_invocation_before_window, _extend_invocation_after_window,
    )
    from ksadk.sessions.base import SessionEventQuery
    async with pg._connection(write=True) as connection:
        await connection.execute('''INSERT INTO ksadk_events
            (namespace, session_id, id, author, event_type, content_json,
             timestamp, seq_id, invocation_id)
            SELECT $1, 'session', 'boundary-' || i, 'agent', 'assistant_message',
                   '{}'::jsonb, i, i, 'same-run' FROM generate_series(1,90) i''', pg.namespace)
    method = 'query_events' if forward else 'get_events'
    original = getattr(pg, method)
    reads = []
    initial = (await pg.query_events(SessionEventQuery(
        session_ids=['session'], from_start=True, order_by_seq=True, limit=64,
    )) if forward else await pg.get_events('session', limit=64))

    async def read(*args, **kwargs):
        rows = await original(*args, **kwargs)
        reads.append(len(rows))
        return rows

    monkeypatch.setattr(pg, method, read)
    complete = (await _extend_invocation_after_window(
        pg, 'session', initial, before_seq_id=91,
    ) if forward else await _extend_invocation_before_window(
        pg, 'session', initial, after_seq_id=None,
    ))
    assert [row.seq_id for row in complete] == list(range(1, 91))
    assert reads == [26], 'The short terminal page proves no further query is needed'


@pytest.mark.asyncio
@pytest.mark.parametrize('forward', [False, True])
async def test_message_window_bounds_bytes_sequence_and_namespace(pg, forward):
    from ksadk.sessions.history_budget import CURRENT_HISTORY_READ_BUDGET, HistoryReadBudget
    async with pg._connection(write=True) as connection:
        await connection.execute('''INSERT INTO ksadk_events
            (namespace, session_id, id, author, event_type, content_json,
             timestamp, seq_id, invocation_id)
            SELECT $1, 'session', 'bytes-' || i, 'agent', 'assistant_message',
                   jsonb_build_object('text', repeat('x', 8192)), i, i, 'run-' || i
            FROM generate_series(1,256) i''', pg.namespace)
    token = CURRENT_HISTORY_READ_BUDGET.set(HistoryReadBudget())
    try:
        window = await pg.get_message_event_window(
            'session', limit=256, from_start=forward, after_seq_id=40, before_seq_id=180,
        )
        sequences = [row.seq_id for row in window]
        assert 1 < len(sequences) < 32
        assert sequences == list(range(sequences[0], sequences[-1] + 1))
        assert sequences[0] == 41 if forward else sequences[-1] == 179
        assert all(row.content == {'text': 'x' * 8192} for row in window)
        assert await pg.get_message_event_window('missing', limit=256) == []
        other = PostgresSessionService(dsn=pg.dsn, namespace=pg.namespace + '_other')
        try:
            assert await other.get_message_event_window('session', limit=256) == []
        finally:
            await other.aclose()
    finally:
        CURRENT_HISTORY_READ_BUDGET.reset(token)
    # Exact event APIs must retain the requested row count.
    assert len(await pg.get_events('session', limit=256)) == 256


@pytest.mark.asyncio
async def test_message_window_checks_and_transfers_in_one_sql_snapshot(pg, monkeypatch):
    from ksadk.sessions.history_budget import CURRENT_HISTORY_READ_BUDGET, HistoryReadBudget
    from ksadk.sessions.errors import HistoryProjectionBudgetExceeded
    import ksadk.sessions.postgres_service as module

    await pg.append_event('session', SessionEvent(content={'text': '汉字' * 120000}))
    original = module.bounded_driver_call
    phases = []
    transferred = []

    async def counted(awaitable, connection, seconds, cleanup_seconds=1.0):
        name = getattr(getattr(awaitable, 'cr_code', None), 'co_qualname', '')
        phases.append(name)
        result = await original(awaitable, connection, seconds, cleanup_seconds)
        if name == 'Connection.fetch':
            transferred.extend(result)
        return result

    monkeypatch.setattr(module, 'bounded_driver_call', counted)
    budget = HistoryReadBudget(maximum=4096)
    token = CURRENT_HISTORY_READ_BUDGET.set(budget)
    try:
        with pytest.raises(HistoryProjectionBudgetExceeded):
            await pg.get_message_event_window('session', limit=256)
        assert transferred and all(row['content_json'] is None for row in transferred)
        assert phases == ['Connection.fetch'], 'No separate preflight or explicit transaction round trips'
        phases.clear()
        budget.maximum = 1024 * 1024
        rows = await pg.get_message_event_window('session', limit=256)
        assert rows[0].content == {'text': '汉字' * 120000}
        assert 720000 < budget.used < 730000
        assert phases == ['Connection.fetch']
    finally:
        CURRENT_HISTORY_READ_BUDGET.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize('forward', [False, True])
async def test_byte_short_boundary_page_does_not_truncate_invocation(pg, monkeypatch, forward):
    from ksadk.sessions.history_budget import CURRENT_HISTORY_READ_BUDGET, HistoryReadBudget
    from ksadk.server.routes.projection import (
        _extend_invocation_before_window, _extend_invocation_after_window,
    )
    async with pg._connection(write=True) as connection:
        await connection.execute('''INSERT INTO ksadk_events
            (namespace, session_id, id, author, event_type, content_json,
             timestamp, seq_id, invocation_id)
            SELECT $1, 'session', 'large-boundary-' || i, 'agent', 'assistant_message',
                   jsonb_build_object('text', repeat('x', 20000)), i, i,
                   CASE WHEN i BETWEEN 2 AND 91 THEN 'current' ELSE 'adjacent' END
            FROM generate_series(1,92) i''', pg.namespace)
    original = pg.get_message_event_window
    pages = []
    async def recorded(*args, **kwargs):
        page = await original(*args, **kwargs)
        assert sum(len(json.dumps(event.content).encode()) for event in page) <= 64 * 1024
        pages.append((len(page), page.source_exhausted))
        return page
    token = CURRENT_HISTORY_READ_BUDGET.set(HistoryReadBudget())
    try:
        initial = await original('session', limit=8, from_start=forward,
                                 after_seq_id=1, before_seq_id=92)
        monkeypatch.setattr(pg, 'get_message_event_window', recorded)
        hint = {}
        complete = (await _extend_invocation_after_window(
            pg, 'session', initial, before_seq_id=None,
        ) if forward else await _extend_invocation_before_window(
            pg, 'session', initial, after_seq_id=None, boundary_hint=hint,
        ))
        assert [row.seq_id for row in complete] == list(range(2, 92))
        assert all(row.content == {'text': 'x' * 20000} for row in complete)
        # Never add a fourth 20 KiB body merely to cross the 64 KiB ceiling.
        # Both directions still complete all 90 events across short pages.
        assert len(pages) >= 29 and all(size <= 3 for size, _ in pages)
        assert pages[0][1] is False, 'Byte-limited short pages do not mean EOF'
        if not forward:
            assert hint == {'has_more': True}
    finally:
        CURRENT_HISTORY_READ_BUDGET.reset(token)


@pytest.mark.asyncio
async def test_message_window_dirty_deleted_and_read_timeout_keep_source(pg, monkeypatch):
    service = ResilientSessionService(pg)
    await service.get_session('session')
    await service.fallback.append_event('session', SessionEvent(id='memory-only'))
    service._dirty_session_ids.add('session')
    async def forbidden(*args, **kwargs):
        raise AssertionError('Dirty memory must not read the durable sequence space')
    monkeypatch.setattr(pg, 'get_message_event_window', forbidden)
    assert [row.id for row in await service.get_message_event_window('session', limit=256)] == ['memory-only']
    service._deleted_session_ids.add('session')
    assert await service.get_message_event_window('session', limit=256) == []
    service._deleted_session_ids.clear()
    service._dirty_session_ids.clear()
    async def timeout(*args, **kwargs):
        raise TimeoutError()
    monkeypatch.setattr(pg, 'get_message_event_window', timeout)
    with pytest.raises(TimeoutError):
        await service.get_message_event_window('session', limit=256)
    assert not service.degraded


@pytest.mark.asyncio
async def test_combined_header_reads_no_state_or_body_and_preserves_dirty_watermark(pg, monkeypatch):
    await pg.append_event('session', SessionEvent(id='durable', content={'text': 'x' * 100000}))
    await pg.update_state(agent_id='agent', user_id='user', session_id='session',
                          scope='session', state_delta={'big': 'x' * 1000000})
    header, watermark = await pg.get_session_header_with_watermark('session')
    assert header.id == 'session' and header.agent_id == 'agent' and header.user_id == 'user'
    assert header.state == {} and header.events == [] and watermark == 1
    assert await pg.get_session_header_with_watermark('missing') == (None, 0)
    service = ResilientSessionService(pg)
    await service.get_session('session')
    await service.fallback.append_event('session', SessionEvent(id='memory-only'))
    service._dirty_session_ids.add('session')
    async def forbidden(*args, **kwargs):
        raise AssertionError('Do not replace a dirty watermark with PostgreSQL')
    monkeypatch.setattr(pg, 'get_session_header_with_watermark', forbidden)
    _, watermark = await service.get_session_header_with_watermark('session')
    assert watermark == 2
    service._deleted_session_ids.add('session')
    assert await service.get_session_header_with_watermark('session') == (None, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('earlier_run', [False, True])
async def test_boundary_hint_uses_existing_rows_without_losing_older_history(pg, earlier_run):
    from ksadk.server.routes.projection import _extend_invocation_before_window
    for index in range(30):
        await pg.append_event('session', SessionEvent(
            id=f'hint-{index}', invocation_id='older' if earlier_run and index < 10 else 'current',
        ))
    initial = await pg.get_events('session', limit=10)
    hint = {}
    complete = await _extend_invocation_before_window(
        pg, 'session', initial, after_seq_id=None, boundary_hint=hint,
    )
    assert hint == {'has_more': earlier_run}
    assert [row.seq_id for row in complete] == list(range(11 if earlier_run else 1, 31))


@pytest.mark.asyncio
@pytest.mark.parametrize('forward', [False, True])
@pytest.mark.parametrize('dirty', [False, True])
async def test_single_source_deep_event_page_transfers_only_requested_rows(pg, monkeypatch, forward, dirty):
    from ksadk.sessions.base import SessionEventQuery
    async with pg._connection(write=True) as connection:
        await connection.execute('''INSERT INTO ksadk_events
            (namespace, session_id, id, author, event_type, content_json, timestamp, seq_id)
            SELECT $1, 'session', 'deep-' || i, 'agent', 'diagnostic',
                   '{}'::jsonb, i, i FROM generate_series(1,512) i''', pg.namespace)
    service = ResilientSessionService(pg)
    if dirty:
        await service.get_session('session')
        service._dirty_session_ids.add('session')
    backend = service.fallback if dirty else pg
    original = backend.query_events
    reads = []
    async def counted(query):
        result = await original(query)
        reads.append((query.offset, query.limit, len(result)))
        return result
    monkeypatch.setattr(backend, 'query_events', counted)
    query = SessionEventQuery(session_ids=['session'], agent_id='agent',
                              offset=400, limit=64, from_start=forward, order_by_seq=True)
    rows = await service.query_events(query)
    assert reads == [(400, 64, 64)]
    assert [row.seq_id for row in rows] == list(range(401, 465) if forward else range(49, 113))
    assert await service.query_events(SessionEventQuery(
        session_ids=['session'], agent_id='different-agent', offset=400, limit=64)) == []
    service._deleted_session_ids.add('session')
    assert await service.query_events(query) == []
