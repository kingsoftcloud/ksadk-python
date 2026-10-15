"""Mainline receipt/audit features retain the bounded database contract."""

import asyncio
import os
import time
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from ksadk.sessions.base import SessionEvent
from ksadk.sessions.db_budget import DatabaseBudget
from ksadk.sessions.errors import HistoryProjectionBudgetExceeded
from ksadk.sessions.postgres_service import PostgresSessionService


@pytest_asyncio.fixture
async def service():
    dsn = os.environ.get("KSADK_TEST_POSTGRES_DSN")
    assert dsn, "Required real PostgreSQL DSN missing"
    backend = PostgresSessionService(
        dsn=dsn, namespace="merge_" + uuid4().hex,
        db_budget=DatabaseBudget(lock_seconds=.15, write_operation_seconds=2),
    )
    await backend.create_session("agent", "owner", "session")
    try:
        yield backend
    finally:
        # Unknown claims intentionally fence normal session deletion.
        async with backend._connection(write=True) as conn:
            await conn.execute("DELETE FROM ksadk_tool_receipts WHERE namespace=$1", backend.namespace)
        await backend.delete_session("session")
        await backend.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_receipt_parent_lock_is_bounded_and_connection_recovers(service, cancel):
    blocker = await asyncpg.connect(service.dsn)
    transaction = blocker.transaction()
    await transaction.start()
    await blocker.fetchval(
        "SELECT 1 FROM ksadk_sessions WHERE namespace=$1 AND id='session' FOR UPDATE",
        service.namespace,
    )
    args = dict(idempotency_key="receipt", tool_name="write_file", arguments_digest="args", claim_id="claim")
    task = asyncio.create_task(service.claim_tool_receipt("session", **args))
    started = time.monotonic()
    try:
        if cancel:
            # Wait for a genuine blocked PG statement, rather than cancelling
            # before acquire/BEGIN has exercised rollback.
            for _ in range(100):
                waiting = await blocker.fetchval(
                    "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock' "
                    "AND query LIKE '%FOR KEY SHARE%' AND pid<>pg_backend_pid()"
                )
                if waiting:
                    break
                await asyncio.sleep(.005)
            assert waiting
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(asyncpg.LockNotAvailableError):
                await task
        assert time.monotonic() - started < 1.5
    finally:
        await transaction.rollback()
        await blocker.close()
    assert (await service.claim_tool_receipt("session", **args)).acquired
    assert service._pool.get_idle_size() == service._pool.get_size()
    async with service._connection() as connection:
        assert not connection.is_in_transaction()
        assert await connection.fetchval("SELECT 42") == 42


@pytest.mark.asyncio
async def test_audit_aggregate_rejects_large_carriers_before_fetch(service):
    await service.append_event("session", SessionEvent(
        id="audit", session_id="session", event_type="runtime_resume",
        content={"detail": "x" * (9 * 1024 * 1024)},
        metadata={"run_id": "run", "checkpoint_id": "cp"},
    ))
    # One carrier would be repeated for both requested keys by the aggregate.
    with pytest.raises(HistoryProjectionBudgetExceeded):
        await service.get_checkpoint_stats([("session", "run", "cp"), ("session", "run", "other")])
    assert service._pool.get_idle_size() == service._pool.get_size()
    assert (await service.get_checkpoint_stats([("session", "run", "cp")]))["audits"][("session", "run", "cp")]["resume_count"] == 1


@pytest.mark.asyncio
async def test_new_resume_alias_is_in_fixed_related_scan(service):
    await service.append_event("session", SessionEvent(
        id="resume", session_id="session", event_type="runtime_resume",
        content={}, metadata={"run_id": "run", "checkpoint_id": "cp"},
    ))
    await service.append_event("session", SessionEvent(
        id="tool", session_id="session", event_type="tool_result", content={"text": "x" * 1024},
    ))
    pages = [page async for page in service.iter_resume_related_event_pages("session")]
    assert [event.id for page in pages for event in page] == ["resume"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["dirty", "deleted"])
async def test_receipt_operations_do_not_cross_a_diverged_session_source(service, state):
    from ksadk.sessions.resilient import ResilientSessionService
    from ksadk.sessions.errors import SessionBackendUnavailable

    wrapper = ResilientSessionService(service)
    getattr(wrapper, "_dirty_session_ids" if state == "dirty" else "_deleted_session_ids").add("session")
    args = dict(idempotency_key="receipt", tool_name="write_file", arguments_digest="args", claim_id="claim")
    # Simulate a different owner's newly created durable incarnation while
    # the process still owns only the old divergent in-memory history.
    async with service._connection(write=True) as connection:
        await connection.execute("UPDATE ksadk_sessions SET user_id='another-owner' WHERE namespace=$1 AND id='session'", service.namespace)
    with pytest.raises(SessionBackendUnavailable):
        await wrapper.claim_tool_receipt("session", **args)
    with pytest.raises(SessionBackendUnavailable):
        await wrapper.settle_tool_receipt("session", **args, state="completed", output={"ok": True})
    with pytest.raises(SessionBackendUnavailable):
        await wrapper.get_tool_receipt_claim("session", idempotency_key="receipt")
    assert await service.get_tool_receipt_claim("session", idempotency_key="receipt") is None
