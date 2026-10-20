"""Real PostgreSQL budgets for ADK sessions and LangGraph checkpoints.

Run with KSADK_TEST_FRAMEWORK_DSN pointing at an isolated local PostgreSQL DB.
The fixture fails rather than silently substituting SQLite or a mock.
"""

from __future__ import annotations

import asyncio
import os
import sys
from uuid import uuid4

import psycopg
import pytest
from google.adk.events.event import Event
from google.adk.sessions import DatabaseSessionService
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout
from sqlalchemy import text

from ksadk.harness.engine.postgres_checkpointer import (
    CheckpointCommitUnknown,
    bounded_postgres_saver_class,
    create_bounded_postgres_saver,
)
from ksadk.memory.adk.bounded_database_session_service import BoundedADKDatabaseSessionService
from ksadk.memory.adk.resilient_session_service import (
    ADKEventCommitUnknown,
    ResilientADKSessionService,
)
from ksadk.memory.adk.short_term_memory import _normalize_database_url
from ksadk.sessions.db_budget import DatabaseBudget


@pytest.fixture(scope="module")
def postgres_dsn() -> str:
    dsn = os.getenv("KSADK_TEST_FRAMEWORK_DSN", "").strip()
    if not dsn:
        pytest.fail("KSADK_TEST_FRAMEWORK_DSN is required for real PostgreSQL tests")
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


@pytest.mark.asyncio
async def test_adk_state_events_restart_and_lock_budget(postgres_dsn: str) -> None:
    app = "budget-adk-" + uuid4().hex
    primary = BoundedADKDatabaseSessionService(
        db_url=_normalize_database_url(postgres_dsn),
        budget=DatabaseBudget(read_sql_seconds=1.0, lock_seconds=0.5),
    )
    service = ResilientADKSessionService(primary)
    try:
        session = await service.create_session(
            app_name=app, user_id="u", session_id="s", state={"count": 1}
        )
        first = Event(author="agent", invocation_id="first")
        await service.append_event(session, first)
        assert (
            await service.reconcile_event_commit(
                app_name=app, user_id="u", session_id="s", event_id=first.id
            )
            == "committed"
        )
        await primary.db_engine.dispose()

        restarted = BoundedADKDatabaseSessionService(
            db_url=_normalize_database_url(postgres_dsn),
            budget=DatabaseBudget(read_sql_seconds=1.0, lock_seconds=0.5),
        )
        try:
            recovered = await restarted.get_session(app_name=app, user_id="u", session_id="s")
            assert recovered is not None
            assert recovered.state["count"] == 1
            assert [event.id for event in recovered.events] == [first.id]

            table = restarted._get_schema_classes().StorageSession.__tablename__
            async with await psycopg.AsyncConnection.connect(postgres_dsn) as holder:
                async with holder.transaction():
                    await holder.execute(
                        f'SELECT id FROM "{table}" WHERE app_name=%s '
                        "AND user_id=%s AND id=%s FOR UPDATE",
                        (app, "u", "s"),
                    )
                    blocked = Event(author="agent", invocation_id="blocked")
                    started = asyncio.get_running_loop().time()
                    with pytest.raises(Exception) as error:
                        await service.append_event(session, blocked)
                    assert asyncio.get_running_loop().time() - started < 2
                    assert "55P03" in str(error.value) or "lock timeout" in str(error.value).lower()
                    assert not service.degraded
                    assert (
                        await service.get_session(app_name=app, user_id="u", session_id="s")
                    ).events[-1].id == blocked.id
            # A failed/unknown write must not be automatically replayed. The
            # live dirty session remains authoritative after the lock clears.
            assert (await service.get_session(app_name=app, user_id="u", session_id="s")).events[
                -1
            ].id == blocked.id
            durable = await restarted.get_session(app_name=app, user_id="u", session_id="s")
            assert [event.id for event in durable.events] == [first.id]

            # SQL timeout cancels and rolls back this read transaction; the
            # next checkout must receive a usable connection.
            with pytest.raises(Exception):
                async with restarted._rollback_on_exception_session(read_only=True) as sql_session:
                    await sql_session.execute(text("SELECT pg_sleep(2)"))
            assert (
                await restarted.get_session(app_name=app, user_id="u", session_id="s")
            ) is not None

            async def cancelled_read() -> None:
                async with restarted._rollback_on_exception_session(read_only=True) as sql_session:
                    await sql_session.execute(text("SELECT pg_sleep(2)"))

            task = asyncio.create_task(cancelled_read())
            await asyncio.sleep(0.03)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert (
                await restarted.get_session(app_name=app, user_id="u", session_id="s")
            ) is not None
        finally:
            await restarted.db_engine.dispose()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_langgraph_checkpoint_put_get_list_writes_restart(postgres_dsn: str) -> None:
    saver, pool = await create_bounded_postgres_saver(postgres_dsn)
    thread = "budget-lg-" + uuid4().hex
    config = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
    checkpoint = empty_checkpoint()
    checkpoint["id"] = uuid4().hex
    try:
        stored = await saver.aput(config, checkpoint, {"source": "input", "step": 0}, {})
        await saver.aput_writes(stored, [("task-result", {"ok": True})], "task-1")
        current = await saver.aget_tuple(stored)
        assert current is not None and current.checkpoint["id"] == checkpoint["id"]
        assert await saver.reconcile_checkpoint_commit(stored) == "committed"
        assert current.pending_writes == [("task-1", "task-result", {"ok": True})]
        assert [item.checkpoint["id"] async for item in saver.alist(config)] == [checkpoint["id"]]
    finally:
        await pool.close()
    restarted, pool = await create_bounded_postgres_saver(postgres_dsn)
    try:
        assert (await restarted.aget_tuple(stored)).checkpoint["id"] == checkpoint["id"]
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_langgraph_checkpoint_row_lock_is_bounded(postgres_dsn: str) -> None:
    saver, pool = await create_bounded_postgres_saver(
        postgres_dsn, budget=DatabaseBudget(lock_seconds=0.5),
    )
    thread = "budget-lg-lock-" + uuid4().hex
    config = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
    checkpoint = empty_checkpoint()
    checkpoint["id"] = uuid4().hex
    try:
        stored = await saver.aput(config, checkpoint, {"source": "input", "step": 0}, {})
        async with await psycopg.AsyncConnection.connect(postgres_dsn) as holder:
            async with holder.transaction():
                await holder.execute(
                    "SELECT checkpoint_id FROM checkpoints WHERE thread_id=%s "
                    "AND checkpoint_ns=%s AND checkpoint_id=%s FOR UPDATE",
                    (thread, "", checkpoint["id"]),
                )
                started = asyncio.get_running_loop().time()
                with pytest.raises(Exception) as error:
                    await saver.aput(config, checkpoint, {"source": "input", "step": 0}, {})
                assert asyncio.get_running_loop().time() - started < 2
                assert "55P03" in str(error.value) or "lock timeout" in str(error.value).lower()
        assert (await saver.aget_tuple(stored)).checkpoint["id"] == checkpoint["id"]
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_adk_and_langgraph_survive_new_process(postgres_dsn: str) -> None:
    """A fresh interpreter must read both durable framework records."""
    app = "budget-process-" + uuid4().hex
    event = Event(author="agent", invocation_id="process-test")
    primary = BoundedADKDatabaseSessionService(db_url=_normalize_database_url(postgres_dsn))
    try:
        session = await primary.create_session(
            app_name=app, user_id="u", session_id="s", state={"answer": 42}
        )
        await primary.append_event(session, event)
    finally:
        await primary.close()

    saver, pool = await create_bounded_postgres_saver(postgres_dsn)
    thread = "budget-process-lg-" + uuid4().hex
    config = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
    checkpoint = empty_checkpoint()
    checkpoint["id"] = uuid4().hex
    try:
        stored = await saver.aput(config, checkpoint, {"source": "input", "step": 0}, {})
    finally:
        await pool.close()

    child = """
import asyncio
import os
from ksadk.harness.engine.postgres_checkpointer import create_bounded_postgres_saver
from ksadk.memory.adk.bounded_database_session_service import BoundedADKDatabaseSessionService
from ksadk.memory.adk.short_term_memory import _normalize_database_url

async def main():
    dsn = os.environ['KSADK_TEST_FRAMEWORK_DSN']
    adk = BoundedADKDatabaseSessionService(db_url=_normalize_database_url(dsn))
    try:
        session = await adk.get_session(
            app_name=os.environ['CHECK_APP'], user_id='u', session_id='s'
        )
        assert session.state['answer'] == 42
        assert [event.id for event in session.events] == [os.environ['CHECK_EVENT']]
    finally:
        await adk.close()
    plain = dsn.replace('postgresql+asyncpg://', 'postgresql://', 1)
    saver, pool = await create_bounded_postgres_saver(plain)
    try:
        config = {'configurable': {
            'thread_id': os.environ['CHECK_THREAD'],
            'checkpoint_ns': '',
            'checkpoint_id': os.environ['CHECK_CHECKPOINT'],
        }}
        assert (await saver.aget_tuple(config)).checkpoint['id'] == os.environ['CHECK_CHECKPOINT']
    finally:
        await pool.close()

asyncio.run(main())
"""
    env = {
        **os.environ,
        "KSADK_TEST_FRAMEWORK_DSN": postgres_dsn,
        "CHECK_APP": app,
        "CHECK_EVENT": event.id,
        "CHECK_THREAD": thread,
        "CHECK_CHECKPOINT": stored["configurable"]["checkpoint_id"],
        "PYTHONPATH": os.getcwd(),
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        child,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
    assert process.returncode == 0, stderr.decode()


@pytest.mark.asyncio
async def test_adk_event_commit_receipt_loss_reconciles_without_replay(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = "budget-receipt-adk-" + uuid4().hex
    primary = BoundedADKDatabaseSessionService(db_url=_normalize_database_url(postgres_dsn))
    service = ResilientADKSessionService(primary)
    try:
        session = await service.create_session(app_name=app, user_id="u", session_id="s")
        event = Event(author="agent", invocation_id="receipt-loss")
        original = DatabaseSessionService.append_event
        calls = 0

        async def committed_then_lost(db, durable, item):
            nonlocal calls
            calls += 1
            await original(db, durable, item)
            raise ConnectionError("commit acknowledgement lost")

        with monkeypatch.context() as patch:
            patch.setattr(DatabaseSessionService, "append_event", committed_then_lost)
            stored = await service.append_event(session, event)
        assert stored.id == event.id
        assert calls == 1
        assert not service.degraded
        assert not service.unknown_event_commits
        durable = await primary.get_session(app_name=app, user_id="u", session_id="s")
        assert [item.id for item in durable.events] == [event.id]

        missing = Event(author="agent", invocation_id="unknown-before-commit")

        async def unavailable_before_commit(_db, _durable, _item):
            nonlocal calls
            calls += 1
            raise ConnectionError("write outcome unavailable")

        with monkeypatch.context() as patch:
            patch.setattr(DatabaseSessionService, "append_event", unavailable_before_commit)
            await service.append_event(session, missing)
        assert calls == 2
        assert service.degraded
        assert ADKEventCommitUnknown(app, "u", "s", missing.id) in service.unknown_event_commits
        durable = await primary.get_session(app_name=app, user_id="u", session_id="s")
        assert [item.id for item in durable.events] == [event.id]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_checkpoint_commit_receipt_loss_reconciles_or_returns_typed_unknown(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    saver, pool = await create_bounded_postgres_saver(postgres_dsn)
    thread = "budget-receipt-lg-" + uuid4().hex
    config = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
    checkpoint = empty_checkpoint()
    checkpoint["id"] = uuid4().hex
    original = AsyncPostgresSaver.aput
    calls = 0

    async def committed_then_lost(db, *args, **kwargs):
        nonlocal calls
        calls += 1
        await original(db, *args, **kwargs)
        raise ConnectionError("commit acknowledgement lost")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(AsyncPostgresSaver, "aput", committed_then_lost)
            stored = await saver.aput(config, checkpoint, {"source": "input", "step": 0}, {})
        assert calls == 1
        assert stored["configurable"]["checkpoint_id"] == checkpoint["id"]
        assert [item.checkpoint["id"] async for item in saver.alist(config)] == [checkpoint["id"]]

        original_writes = AsyncPostgresSaver.aput_writes
        write_calls = 0

        async def writes_committed_then_lost(db, *args, **kwargs):
            nonlocal write_calls
            write_calls += 1
            await original_writes(db, *args, **kwargs)
            raise ConnectionError("writes acknowledgement lost")

        with monkeypatch.context() as patch:
            patch.setattr(AsyncPostgresSaver, "aput_writes", writes_committed_then_lost)
            await saver.aput_writes(stored, [("receipt-write", 7)], "task-receipt")
        assert write_calls == 1
        assert (await saver.aget_tuple(stored)).pending_writes == [
            ("task-receipt", "receipt-write", 7)
        ]

        absent = empty_checkpoint()
        absent["id"] = uuid4().hex

        async def lost_before_commit(_db, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise ConnectionError("write receipt unknown")

        with monkeypatch.context() as patch:
            patch.setattr(AsyncPostgresSaver, "aput", lost_before_commit)
            with pytest.raises(CheckpointCommitUnknown) as error:
                await saver.aput(config, absent, {"source": "input", "step": 1}, {})
        assert error.value.checkpoint_id == absent["id"]
        assert calls == 2
        assert [item.checkpoint["id"] async for item in saver.alist(config)] == [checkpoint["id"]]
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_adk_cancel_terminates_checked_out_transaction_within_cleanup_budget(
    postgres_dsn: str,
) -> None:
    service = BoundedADKDatabaseSessionService(db_url=_normalize_database_url(postgres_dsn))
    await service.create_session(app_name="budget-cleanup-" + uuid4().hex, user_id="u")
    service.db_budget = DatabaseBudget(cleanup_seconds=0.25)
    started = asyncio.Event()
    release = asyncio.Event()
    backend_pid: int | None = None

    async def stubborn_transaction() -> None:
        nonlocal backend_pid
        async with service._rollback_on_exception_session() as sql_session:
            backend_pid = (await sql_session.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()

    try:
        started_at = asyncio.get_running_loop().time()
        outer = asyncio.create_task(service._bounded(stubborn_transaction(), 0.05))
        await started.wait()
        with pytest.raises(TimeoutError):
            await outer
        assert asyncio.get_running_loop().time() - started_at < 0.45
        assert not service._active_connections
        assert backend_pid is not None
        async with await psycopg.AsyncConnection.connect(postgres_dsn) as inspector:
            result = await inspector.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE pid=%s", (backend_pid,)
            )
            assert (await result.fetchone())[0] == 0
    finally:
        release.set()
        await asyncio.gather(*tuple(service._active_connections), return_exceptions=True)
        await service.close()


@pytest.mark.asyncio
async def test_adk_first_setup_and_write_share_one_operation_budget(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = BoundedADKDatabaseSessionService(
        db_url=_normalize_database_url(postgres_dsn),
        budget=DatabaseBudget(write_operation_seconds=0.2),
    )
    prepare_name = "prepare_tables" if hasattr(DatabaseSessionService, "prepare_tables") else "_prepare_tables"
    original_prepare = getattr(DatabaseSessionService, prepare_name)
    original_create = DatabaseSessionService.create_session

    async def delayed_prepare(db):
        if not db._tables_created:
            await asyncio.sleep(0.12)
        return await original_prepare(db)

    async def delayed_create(db, **kwargs):
        await getattr(db, prepare_name)()
        await asyncio.sleep(0.12)
        return await original_create(db, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(DatabaseSessionService, prepare_name, delayed_prepare)
            patch.setattr(DatabaseSessionService, "create_session", delayed_create)
            started = asyncio.get_running_loop().time()
            with pytest.raises(TimeoutError):
                await service.create_session(app_name="budget-setup-" + uuid4().hex, user_id="u")
            assert asyncio.get_running_loop().time() - started < 0.35
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_checkpoint_list_consumer_wait_is_outside_db_budget(postgres_dsn: str) -> None:
    budget = DatabaseBudget(read_acquire_seconds=0.1, read_sql_seconds=0.1)
    saver, pool = await create_bounded_postgres_saver(postgres_dsn, budget=budget)
    thread = "budget-consumer-" + uuid4().hex
    config = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
    try:
        for _ in range(2):
            checkpoint = empty_checkpoint()
            checkpoint["id"] = uuid4().hex
            await saver.aput(config, checkpoint, {"source": "input", "step": 0}, {})
        stream = saver.alist(config)
        await anext(stream)
        async with pool.connection(timeout=0.1):
            await asyncio.sleep(0.25)
        assert (await anext(stream)).checkpoint["id"]
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_checkpoint_write_phase_and_reset_preserve_constraint_error(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget = DatabaseBudget(read_sql_seconds=0.05, write_sql_seconds=0.5)
    saver, pool = await create_bounded_postgres_saver(postgres_dsn, budget=budget)
    original_delete = AsyncPostgresSaver.adelete_thread
    original_execute = psycopg.AsyncConnection.execute

    async def slow_delete(db, thread_id):
        async with db._cursor(pipeline=False) as cur:
            await cur.execute("SELECT pg_sleep(0.15)")
        await original_delete(db, thread_id)

    async def reset_fails(conn, query, *args, **kwargs):
        if str(query).startswith("RESET"):
            raise RuntimeError("injected RESET failure")
        return await original_execute(conn, query, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(AsyncPostgresSaver, "adelete_thread", slow_delete)
            await saver.adelete_thread("budget-write-phase-" + uuid4().hex)
        with monkeypatch.context() as patch:
            patch.setattr(psycopg.AsyncConnection, "execute", reset_fails)
            with pytest.raises(psycopg.errors.UniqueViolation):
                async with saver._cursor() as cur:
                    await cur.execute("CREATE TEMP TABLE budget_unique (id int PRIMARY KEY)")
                    await cur.execute("INSERT INTO budget_unique VALUES (1)")
                    await cur.execute("INSERT INTO budget_unique VALUES (1)")
        assert await saver.aget_tuple({"configurable": {"thread_id": uuid4().hex}}) is None
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_checkpoint_cancel_closes_only_its_checked_out_connection(postgres_dsn: str) -> None:
    budget = DatabaseBudget(write_operation_seconds=0.05, cleanup_seconds=0.25)
    saver, pool = await create_bounded_postgres_saver(postgres_dsn, budget=budget)
    started = asyncio.Event()
    release = asyncio.Event()
    backend_pid: int | None = None

    async def stubborn_write() -> None:
        nonlocal backend_pid
        async with saver._cursor(pipeline=False) as cur:
            await cur.execute("SELECT pg_backend_pid()")
            backend_pid = (await cur.fetchone())["pg_backend_pid"]
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()

    try:
        start = asyncio.get_running_loop().time()
        outer = asyncio.create_task(saver._run_write(stubborn_write()))
        await started.wait()
        with pytest.raises(TimeoutError):
            await outer
        assert asyncio.get_running_loop().time() - start < 0.45
        assert not saver._active_connections
        assert backend_pid is not None
        async with await psycopg.AsyncConnection.connect(postgres_dsn) as inspector:
            result = await inspector.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE pid=%s", (backend_pid,)
            )
            assert (await result.fetchone())[0] == 0
    finally:
        release.set()
        await asyncio.gather(*tuple(saver._active_connections), return_exceptions=True)
        await pool.close()


@pytest.mark.asyncio
async def test_checkpoint_set_failure_closes_current_connection(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    saver, pool = await create_bounded_postgres_saver(postgres_dsn)
    original_execute = psycopg.AsyncConnection.execute
    touched: list[psycopg.AsyncConnection] = []

    async def set_fails(conn, query, *args, **kwargs):
        if str(query).startswith("SET statement_timeout"):
            touched.append(conn)
            raise RuntimeError("injected SET failure")
        return await original_execute(conn, query, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(psycopg.AsyncConnection, "execute", set_fails)
            with pytest.raises(RuntimeError, match="injected SET failure"):
                await saver.aget_tuple({"configurable": {"thread_id": uuid4().hex}})
        assert len(touched) == 1 and touched[0].closed
        assert await saver.aget_tuple({"configurable": {"thread_id": uuid4().hex}}) is None
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_adk_receipt_loss_after_write_deadline_stays_dirty_with_one_commit(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = "budget-deadline-adk-" + uuid4().hex
    primary = BoundedADKDatabaseSessionService(db_url=_normalize_database_url(postgres_dsn))
    service = ResilientADKSessionService(primary)
    try:
        session = await service.create_session(app_name=app, user_id="u", session_id="s")
        primary.db_budget = DatabaseBudget(write_operation_seconds=0.25, cleanup_seconds=0.1)
        event = Event(author="agent", invocation_id="late-receipt")
        original = DatabaseSessionService.append_event
        calls = 0

        async def committed_but_receipt_stalls(db, durable, item):
            nonlocal calls
            calls += 1
            await original(db, durable, item)
            await asyncio.sleep(1)
            raise ConnectionError("receipt never arrived")

        with monkeypatch.context() as patch:
            patch.setattr(DatabaseSessionService, "append_event", committed_but_receipt_stalls)
            started = asyncio.get_running_loop().time()
            stored = await service.append_event(session, event)
            elapsed = asyncio.get_running_loop().time() - started
        assert stored.id == event.id
        assert elapsed < 0.5
        assert calls == 1
        assert ADKEventCommitUnknown(app, "u", "s", event.id) in service.unknown_event_commits
        durable = await primary.get_session(app_name=app, user_id="u", session_id="s")
        assert [item.id for item in durable.events] == [event.id]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_checkpoint_receipt_loss_after_write_deadline_is_typed_unknown(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget = DatabaseBudget(write_operation_seconds=0.25, cleanup_seconds=0.1)
    saver, pool = await create_bounded_postgres_saver(postgres_dsn, budget=budget)
    thread = "budget-deadline-lg-" + uuid4().hex
    config = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
    checkpoint = empty_checkpoint()
    checkpoint["id"] = uuid4().hex
    original = AsyncPostgresSaver.aput
    calls = 0

    async def committed_but_receipt_stalls(db, *args, **kwargs):
        nonlocal calls
        calls += 1
        await original(db, *args, **kwargs)
        await asyncio.sleep(1)
        raise ConnectionError("receipt never arrived")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(AsyncPostgresSaver, "aput", committed_but_receipt_stalls)
            started = asyncio.get_running_loop().time()
            with pytest.raises(CheckpointCommitUnknown) as error:
                await saver.aput(config, checkpoint, {"source": "input", "step": 0}, {})
            elapsed = asyncio.get_running_loop().time() - started
        assert error.value.checkpoint_id == checkpoint["id"]
        assert elapsed < 0.5
        assert calls == 1
        assert [item.checkpoint["id"] async for item in saver.alist(config)] == [checkpoint["id"]]

        write_config = {
            "configurable": {**config["configurable"], "checkpoint_id": checkpoint["id"]}
        }
        original_writes = AsyncPostgresSaver.aput_writes
        write_calls = 0

        async def writes_committed_but_receipt_stalls(db, *args, **kwargs):
            nonlocal write_calls
            write_calls += 1
            await original_writes(db, *args, **kwargs)
            await asyncio.sleep(1)
            raise ConnectionError("writes receipt never arrived")

        with monkeypatch.context() as patch:
            patch.setattr(AsyncPostgresSaver, "aput_writes", writes_committed_but_receipt_stalls)
            started = asyncio.get_running_loop().time()
            with pytest.raises(CheckpointCommitUnknown) as writes_error:
                await saver.aput_writes(write_config, [("task-result", 7)], "task-deadline")
            elapsed = asyncio.get_running_loop().time() - started
        assert writes_error.value.operation == "aput_writes"
        assert elapsed < 0.5
        assert write_calls == 1
        assert ("task-deadline", "task-result", 7) in (
            await saver.aget_tuple(write_config)
        ).pending_writes
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_langgraph_sleep_pool_exhaustion_and_cancel_reuse(postgres_dsn: str) -> None:
    budget = DatabaseBudget(
        read_sql_seconds=0.15,
        read_acquire_seconds=0.15,
        write_sql_seconds=1,
        write_operation_seconds=2,
        lock_seconds=0.2,
        cleanup_seconds=0.5,
    )
    pool = AsyncConnectionPool(
        conninfo=postgres_dsn,
        min_size=1,
        max_size=1,
        open=False,
        timeout=budget.read_acquire_seconds,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open(wait=True)
    saver = bounded_postgres_saver_class()(pool, budget=budget)
    try:
        async with saver._cursor() as cur:
            with pytest.raises(psycopg.errors.QueryCanceled):
                await cur.execute("SELECT pg_sleep(2)")
        assert await saver.aget_tuple({"configurable": {"thread_id": uuid4().hex}}) is None

        async with pool.connection() as held:
            with pytest.raises((PoolTimeout, TimeoutError)):
                await saver.aget_tuple({"configurable": {"thread_id": uuid4().hex}})
            assert not held.closed

        async def slow_read() -> None:
            async with saver._cursor() as cur:
                await cur.execute("SELECT pg_sleep(2)")

        task = asyncio.create_task(slow_read())
        await asyncio.sleep(0.03)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await saver.aget_tuple({"configurable": {"thread_id": uuid4().hex}}) is None
    finally:
        await pool.close()
