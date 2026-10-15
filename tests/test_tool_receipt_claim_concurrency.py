"""Force real database claim/delete and conflicting-insert interleavings."""

import asyncio
import threading
from contextlib import asynccontextmanager, contextmanager

import pytest

from ksadk.sessions._local_tables import tool_receipts_table
from ksadk.sessions._postgres_tables import pg_tool_receipts_table
from ksadk.sessions.local_service import LocalSessionService
from ksadk.sessions.postgres_service import PostgresSessionService
from tests.server.test_checkpoint_resume_attempt_audit import (
    audit_service as _audit_service_fixture,
)

audit_service = _audit_service_fixture


class _Cursor:
    def __init__(self, cursor, after_read):
        self.cursor, self.after_read = cursor, after_read

    def fetchone(self):
        row = self.cursor.fetchone()
        self.after_read(row)
        return row


class _SQLiteConnection:
    def __init__(self, connection, predicate, after_read):
        self.connection, self.predicate, self.after_read = connection, predicate, after_read

    def execute(self, query, *args):
        cursor = self.connection.execute(query, *args)
        return _Cursor(cursor, self.after_read) if self.predicate(query) else cursor

    def __getattr__(self, key):
        return getattr(self.connection, key)


def _instrument_sqlite(service, predicate, after_read):
    original = service._connection

    @contextmanager
    def connection():
        with original() as value:
            yield _SQLiteConnection(value, predicate, after_read)

    service._connection = connection
    return original


class _PostgresPool:
    def __init__(self, pool, checked, release):
        self.pool, self.checked, self.release = pool, checked, release

    @asynccontextmanager
    async def acquire(self, **kwargs):
        async with self.pool(**kwargs) as connection:
            pool = self

            class Connection:
                async def fetchval(self, query, *args):
                    row = await connection.fetchval(query, *args)
                    if f"SELECT 1 FROM {pg_tool_receipts_table}" in query:
                        assert row is None
                        pool.checked.set()
                        await pool.release.wait()
                    return row

                def __getattr__(self, key):
                    return getattr(connection, key)

            yield Connection()

    def __getattr__(self, key):
        return getattr(self.pool, key)


@pytest.mark.asyncio
@pytest.mark.parametrize("audit_service", ["sqlite", "postgres"], indirect=True)
async def test_delete_cannot_erase_a_claim_inserted_after_its_check(audit_service):
    first = audit_service
    is_sqlite = isinstance(first, LocalSessionService)
    second = (LocalSessionService(first.db_path) if is_sqlite else
              PostgresSessionService(dsn=first.dsn, namespace=first.namespace))
    if is_sqlite:
        checked, release = threading.Event(), threading.Event()

        def pause(row):
            assert row is None
            checked.set()
            assert release.wait(5)

        original = _instrument_sqlite(first,
            lambda query: f"SELECT 1 FROM {tool_receipts_table}" in query, pause)
    else:
        checked, release = asyncio.Event(), asyncio.Event()
        original = first._connection
        first._connection = _PostgresPool(original, checked, release).acquire
    deletion = asyncio.create_task(first.delete_session("session-1"))
    try:
        if is_sqlite:
            assert await asyncio.to_thread(checked.wait, 5)
        else:
            await asyncio.wait_for(checked.wait(), 5)
        claim = asyncio.create_task(second.claim_tool_receipt(
            "session-1", idempotency_key="tool_receipt:delete-race", tool_name="run_command",
            arguments_digest="args", claim_id="claim-race",
        ))
        done, _ = await asyncio.wait({claim}, timeout=0.2)
        release.set()
        assert await deletion
        outcome = await asyncio.gather(claim, return_exceptions=True)
        assert not done, "claim escaped the deletion fence before DELETE"
        assert isinstance(outcome[0], Exception)
        assert await second.get_tool_receipt_claim(
            "session-1", idempotency_key="tool_receipt:delete-race"
        ) is None
    finally:
        release.set()
        await asyncio.gather(deletion, return_exceptions=True)
        if is_sqlite:
            first._connection = original
        else:
            first._connection = original
            await second.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("collision", ["tool", "arguments"])
async def test_sqlite_insert_loser_rejects_conflicting_identity(tmp_path, collision):
    first = LocalSessionService(tmp_path / "claims.sqlite")
    second = LocalSessionService(first.db_path)
    await first.create_session("agent-1", "user-1", session_id="session-1")
    barrier = threading.Barrier(2)

    def pause(row):
        # Only the initial miss is synchronized; the insert-winner read is real.
        if row is None:
            barrier.wait(5)

    for service in (first, second):
        _instrument_sqlite(service,
            lambda query: f"SELECT * FROM {tool_receipts_table}" in query, pause)
    claims = await asyncio.gather(*[
        service.claim_tool_receipt(
            "session-1", idempotency_key="tool_receipt:shared", claim_id=str(index),
            tool_name="other_tool" if collision == "tool" and index else "run_command",
            arguments_digest="other_args" if collision == "arguments" and index else "args",
        ) for index, service in enumerate((first, second))
    ], return_exceptions=True)
    assert sum(isinstance(claim, ValueError) for claim in claims) == 1
    winner = next(claim for claim in claims if not isinstance(claim, Exception))
    assert winner.acquired


@pytest.mark.asyncio
@pytest.mark.parametrize("audit_service", ["sqlite", "postgres"], indirect=True)
async def test_existing_unknown_fences_other_instance_delete_until_settlement(audit_service):
    first = audit_service
    second = (LocalSessionService(first.db_path) if isinstance(first, LocalSessionService) else
              PostgresSessionService(dsn=first.dsn, namespace=first.namespace))
    try:
        claim = await first.claim_tool_receipt(
            "session-1", idempotency_key="tool_receipt:in-flight", tool_name="run_command",
            arguments_digest="args", claim_id="claim-in-flight",
        )
        with pytest.raises(ValueError, match="durable tool receipt claims"):
            await second.delete_session("session-1")
        assert (await second.get_tool_receipt_claim(
            "session-1", idempotency_key=claim.idempotency_key
        )).state == "unknown"
        await first.settle_tool_receipt(
            "session-1", idempotency_key=claim.idempotency_key, tool_name=claim.tool_name,
            arguments_digest=claim.arguments_digest, claim_id=claim.claim_id,
            state="completed", output={"ok": True},
        )
        assert await second.delete_session("session-1")
    finally:
        if isinstance(second, PostgresSessionService):
            await second.aclose()
