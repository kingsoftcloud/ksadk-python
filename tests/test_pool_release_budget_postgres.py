"""A real driver reset must use cleanup, not the earlier acquire deadline."""
import asyncio
import os

import asyncpg
import pytest

from ksadk.sessions.db_budget import DatabaseBudget, budgeted_pool_connection
from ksadk.sessions.postgres_service import PostgresSessionService


@pytest.mark.asyncio
@pytest.mark.parametrize('helper', ['driver', 'cleanup'])
async def test_detached_proxy_still_reaps_cancellation_resistant_task(helper):
    from ksadk.sessions.db_budget import bounded_cleanup, bounded_driver_call, _cleanup_deadline
    dsn = os.environ.get('KSADK_TEST_POSTGRES_DSN')
    assert dsn, 'Required real PostgreSQL DSN missing'
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    unhandled = []
    loop.set_exception_handler(lambda loop, context: unhandled.append(context))
    token = _cleanup_deadline.set(None)
    try:
        async with pool.acquire() as released:
            assert await released.fetchval('SELECT 42') == 42
        finished = asyncio.Event()

        async def late_failure():
            try:
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    pass
                # This is an actual asyncpg detached-proxy failure, not a mock.
                await released.fetchval('SELECT 1')
            finally:
                finished.set()

        call = bounded_driver_call if helper == 'driver' else bounded_cleanup
        with pytest.raises(TimeoutError):
            await call(late_failure(), released, .01)
        await asyncio.wait_for(finished.wait(), 1)
        await asyncio.sleep(0)
        import gc
        gc.collect()
        await asyncio.sleep(0)
        assert not unhandled
        async with pool.acquire() as usable:
            assert await usable.fetchval('SELECT 42') == 42
        assert pool.get_idle_size() == 1
    finally:
        _cleanup_deadline.reset(token)
        loop.set_exception_handler(previous)
        await pool.close()


@pytest.mark.asyncio
async def test_history_page_streams_batches_with_ordinary_command_budget():
    from ksadk.sessions.postgres_service import _TimedConnection
    dsn = os.environ.get('KSADK_TEST_POSTGRES_DSN')
    assert dsn, 'Required real PostgreSQL DSN missing'
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
    try:
        async with pool.acquire() as raw:
            connection = _TimedConnection(raw, 0.15, replay_sql_seconds=5)
            async with asyncio.timeout(3), connection.transaction(isolation='repeatable_read', readonly=True):
                rows = await connection.fetch_history_page(
                    'SELECT i, pg_sleep(0.003) FROM generate_series(1, 120) AS i', batch_size=20)
                assert [r['i'] for r in rows] == list(range(1, 121))
            assert not raw.is_in_transaction()
        with pytest.raises(TimeoutError):
            async with pool.acquire() as raw:
                connection = _TimedConnection(raw, 0.15, replay_sql_seconds=5)
                async with connection.transaction():
                    await connection.fetch_history_page('SELECT pg_sleep(0.4)')
        async with pool.acquire() as raw:
            assert await raw.fetchval('SELECT 42') == 42
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_history_page_respects_outer_deadline_and_releases_connection():
    from ksadk.sessions.postgres_service import _TimedConnection
    dsn = os.environ.get('KSADK_TEST_POSTGRES_DSN')
    assert dsn, 'Required real PostgreSQL DSN missing'
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
    try:
        with pytest.raises(TimeoutError):
            async with pool.acquire() as raw:
                connection = _TimedConnection(raw, 1)
                async with asyncio.timeout(0.2), connection.transaction():
                    await connection.fetch_history_page(
                        'SELECT i, pg_sleep(0.01) FROM generate_series(1, 200) AS i', batch_size=10)
        async with pool.acquire() as raw:
            assert not raw.is_in_transaction()
            assert await raw.fetchval('SELECT 42') == 42
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_replay_command_budget_is_independent_of_page_reads():
    from ksadk.sessions.postgres_service import _TimedConnection
    dsn = os.environ.get('KSADK_TEST_POSTGRES_DSN')
    assert dsn, 'Required real PostgreSQL DSN missing'
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
    try:
        async with pool.acquire() as raw:
            connection = _TimedConnection(raw, 0.04, replay_sql_seconds=0.6)
            rows = await connection.fetch_replay(
                'SELECT i, pg_sleep(0.02) FROM generate_series(1, 6) AS i')
            assert [r['i'] for r in rows] == list(range(1, 7))
            assert not raw.is_in_transaction()
            with pytest.raises(TimeoutError):
                await connection.fetch('SELECT pg_sleep(0.2)')
        async with pool.acquire() as raw:
            assert await raw.fetchval('SELECT 42') == 42
    finally:
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('backend', ['session', 'shared'])
async def test_release_has_independent_cleanup_budget(backend):
    dsn = os.environ.get('KSADK_TEST_POSTGRES_DSN')
    assert dsn, 'Required real PostgreSQL DSN missing'
    resets = []
    async def reset(connection):
        await connection.execute('SELECT pg_sleep(0.12)')
        resets.append(connection.get_server_pid())
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1, reset=reset)
    budget = DatabaseBudget(read_acquire_seconds=0.03, cleanup_seconds=0.6)
    service = PostgresSessionService(dsn=dsn, db_budget=budget)
    service._pool = pool
    try:
        for _ in range(2):
            cm = service._connection() if backend == 'session' else budgeted_pool_connection(pool, budget)
            async with cm as connection:
                assert await connection.fetchval('SELECT 42') == 42
            assert pool.get_idle_size() == 1
        assert len(resets) == 2
        assert resets[0] == resets[1], 'Healthy connection should be reused'
    finally:
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('backend', ['session', 'shared'])
async def test_slow_reset_is_terminated_and_pool_can_recover(backend):
    import gc
    dsn = os.environ.get('KSADK_TEST_POSTGRES_DSN')
    assert dsn, 'Required real PostgreSQL DSN missing'
    slow = True
    async def reset(connection):
        if slow:
            await connection.execute('SELECT pg_sleep(2)')
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1, reset=reset)
    budget = DatabaseBudget(read_acquire_seconds=0.5, cleanup_seconds=0.12)
    service = PostgresSessionService(dsn=dsn, db_budget=budget)
    service._pool = pool
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    unhandled = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        cm = service._connection() if backend == 'session' else budgeted_pool_connection(pool, budget)
        started = asyncio.get_running_loop().time()
        async with cm as connection:
            assert await connection.fetchval('SELECT 42') == 42
        assert asyncio.get_running_loop().time() - started < 0.7
        slow = False
        cm = service._connection() if backend == 'session' else budgeted_pool_connection(pool, budget)
        async with cm as connection:
            assert await connection.fetchval('SELECT 43') == 43
        assert pool.get_idle_size() == 1
    finally:
        await pool.close()
        # Let the driver's shielded reset finish, then force collection so a
        # lost task exception cannot silently pass this connection-reuse test.
        await asyncio.sleep(0.15)
        gc.collect()
        await asyncio.sleep(0)
        loop.set_exception_handler(previous_handler)
    assert not unhandled, unhandled


@pytest.mark.asyncio
@pytest.mark.parametrize('backend', ['session', 'shared'])
async def test_reset_failure_preserves_committed_write_and_original_sql_error(backend):
    import uuid
    dsn = os.environ.get('KSADK_TEST_POSTGRES_DSN')
    assert dsn, 'Required real PostgreSQL DSN missing'
    table = 'release_' + uuid.uuid4().hex
    observer = await asyncpg.connect(dsn)

    async def reset(connection):
        await connection.execute('SELECT pg_sleep(2)')

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1, reset=reset)
    budget = DatabaseBudget(write_acquire_seconds=1, cleanup_seconds=0.12)
    service = PostgresSessionService(dsn=dsn, db_budget=budget)
    service._pool = pool
    def lease():
        return service._connection(write=True) if backend == 'session' else budgeted_pool_connection(pool, budget, write=True)
    try:
        await observer.execute(f'CREATE TABLE {table}(id int PRIMARY KEY)')
        async with lease() as connection:
            first_pid = await connection.fetchval('SELECT pg_backend_pid()')
            async with connection.transaction():
                await connection.execute(f'INSERT INTO {table} VALUES(1)')
        assert await observer.fetchval(f'SELECT count(*) FROM {table}') == 1
        with pytest.raises(asyncpg.UniqueViolationError):
            async with lease() as connection:
                assert await connection.fetchval('SELECT pg_backend_pid()') != first_pid
                async with connection.transaction():
                    await connection.execute(f'INSERT INTO {table} VALUES(1)')
        assert await observer.fetchval(f'SELECT count(*) FROM {table}') == 1
        async with lease() as connection:
            assert not connection.is_in_transaction()
            assert await connection.fetchval('SELECT 42') == 42
    finally:
        await pool.close()
        await observer.execute(f'DROP TABLE IF EXISTS {table}')
        await observer.close()


@pytest.mark.asyncio
async def test_cancellation_during_reset_still_propagates_and_releases_pool():
    dsn = os.environ.get('KSADK_TEST_POSTGRES_DSN')
    assert dsn, 'Required real PostgreSQL DSN missing'
    resetting = asyncio.Event()
    slow = True
    async def reset(connection):
        if slow:
            resetting.set()
            await connection.execute('SELECT pg_sleep(2)')
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1, reset=reset)
    budget = DatabaseBudget(cleanup_seconds=0.2)
    async def read():
        async with budgeted_pool_connection(pool, budget) as connection:
            return await connection.fetchval('SELECT 42')
    try:
        task = asyncio.create_task(read())
        await asyncio.wait_for(resetting.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        slow = False
        assert await read() == 42
        assert pool.get_idle_size() == 1
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_full_replay_batches_driver_reads_without_truncation():
    from ksadk.sessions.postgres_service import _TimedConnection
    dsn = os.environ.get("KSADK_TEST_POSTGRES_DSN")
    assert dsn, "Required real PostgreSQL DSN missing"
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
    try:
        async with pool.acquire() as raw:
            connection = _TimedConnection(raw, sql_seconds=0.15, replay_seconds=3)
            rows = await connection.fetch_replay(
                "SELECT i, pg_sleep(0.003) FROM generate_series(1, 120) AS i", batch_size=20)
            assert [r["i"] for r in rows] == list(range(1, 121))
            assert not raw.is_in_transaction()
        assert pool.get_idle_size() == 1
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_full_replay_total_deadline_cancels_and_returns_pool_connection():
    from ksadk.sessions.postgres_service import _TimedConnection
    dsn = os.environ.get("KSADK_TEST_POSTGRES_DSN")
    assert dsn, "Required real PostgreSQL DSN missing"
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
    try:
        started = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError):
            async with pool.acquire() as raw:
                await _TimedConnection(raw, 0.2, replay_seconds=0.25).fetch_replay(
                    "SELECT i, pg_sleep(0.01) FROM generate_series(1, 200) AS i", batch_size=10)
        assert asyncio.get_running_loop().time() - started < 1.5
        async with pool.acquire() as raw:
            assert await raw.fetchval("SELECT 42") == 42
            assert not raw.is_in_transaction()
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_full_replay_cursor_keeps_snapshot_during_concurrent_append(monkeypatch):
    import uuid
    import ksadk.sessions.postgres_service as module
    dsn = os.environ.get("KSADK_TEST_POSTGRES_DSN")
    assert dsn, "Required real PostgreSQL DSN missing"
    reader, writer = await asyncpg.connect(dsn), await asyncpg.connect(dsn)
    table = "replay_" + uuid.uuid4().hex
    driver_call = module.bounded_driver_call
    sizes = []
    async def observe(awaitable, connection, seconds, cleanup_seconds=1.0):
        result = await driver_call(awaitable, connection, seconds, cleanup_seconds)
        if isinstance(result, list):
            sizes.append(len(result))
            if len(sizes) == 1:
                await writer.execute(f"INSERT INTO {table} VALUES (301)")
        return result
    try:
        await reader.execute(f"CREATE TABLE {table} (seq bigint PRIMARY KEY)")
        await reader.execute(f"INSERT INTO {table} SELECT generate_series(1, 300)")
        monkeypatch.setattr(module, "bounded_driver_call", observe)
        rows = await module._TimedConnection(reader, 1).fetch_replay(
            f"SELECT seq FROM {table} ORDER BY seq", batch_size=64)
        assert [r["seq"] for r in rows] == list(range(1, 301))
        assert max(sizes) == 64 and sum(sizes) == 300
        assert await writer.fetchval(f"SELECT count(*) FROM {table}") == 301
        assert not reader.is_in_transaction()
    finally:
        await writer.execute(f"DROP TABLE IF EXISTS {table}")
        await reader.close()
        await writer.close()
