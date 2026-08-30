# -*- coding: utf-8 -*-
"""SQLiteAgentKernelStore conformance（Phase 1 Task 3 Step 5/6）。"""

from __future__ import annotations

import pytest

from ksadk.events.session_event import SessionServiceEventStore
from ksadk.kernel.sqlite_store import SCHEMA_VERSION, SQLiteAgentKernelStore
from ksadk.kernel.store import AgentKernelStore
from ksadk.sessions.local_service import LocalSessionService
from tests.kernel.store_conformance import (
    CONFORMANCE_CHECKS,
    assert_concurrent_accepts_have_no_loss_or_duplicate,
    seed_session,
)


@pytest.fixture
async def store(tmp_path):
    service = LocalSessionService(db_path=tmp_path / "sessions.sqlite")
    await seed_session(service)()
    kernel_store = SQLiteAgentKernelStore(
        tmp_path / "kernel.sqlite", SessionServiceEventStore(service)
    )
    await kernel_store.ensure_schema()
    yield kernel_store
    await kernel_store.close()


@pytest.mark.parametrize("check", CONFORMANCE_CHECKS)
async def test_sqlite_store_conformance(store, check):
    await check(store)


async def test_sqlite_store_concurrency(store):
    await assert_concurrent_accepts_have_no_loss_or_duplicate(store)


async def test_sqlite_store_satisfies_port_protocol(store):
    assert isinstance(store, AgentKernelStore)


async def test_sqlite_schema_migration_is_idempotent(tmp_path):
    service = LocalSessionService(db_path=tmp_path / "sessions.sqlite")
    await seed_session(service)()
    db_path = tmp_path / "kernel.sqlite"
    first = SQLiteAgentKernelStore(db_path, SessionServiceEventStore(service))
    await first.ensure_schema()
    await first.ensure_schema()
    await first.close()
    second = SQLiteAgentKernelStore(db_path, SessionServiceEventStore(service))
    await second.ensure_schema()

    import sqlite3

    with sqlite3.connect(db_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert version == SCHEMA_VERSION
    assert journal == "wal"
    assert {"kernel_inbox", "kernel_runs", "kernel_activations"} <= tables
    await second.close()


async def test_sqlite_store_control_events_share_session_log(store):
    from tests.kernel.store_conformance import SESSION, command, lease_request

    await store.accept_command(command("k1", "hello"), queue_limit=4)
    lease = await store.acquire_activation(lease_request("act-log"))
    message = await store.claim_next("agent-1", SESSION, lease.fencing_token)
    assert message is not None
    events = await store._events.read(SESSION, 0, 20)
    types = [event.event_type for event in events]
    assert "control.command_accepted" in types
    assert "control.message_claimed" in types
    assert all(event.family == "control" and event.family_version == 1 for event in events)


class _FlakyEventStore:
    """第一步成功、之后 append 全部抛错的 SessionEventStore 包装。"""

    def __init__(self, inner, fail_on: int = 0):
        self._inner = inner
        self._fail_on = fail_on
        self.calls = 0

    async def append(self, envelope, *, guard=None):
        if self.calls >= self._fail_on:
            raise RuntimeError("session event store crashed mid-flight")
        self.calls += 1
        return await self._inner.append(envelope, guard=guard)

    async def read(self, session_id, after_seq, limit):
        return await self._inner.read(session_id, after_seq, limit)


async def test_accept_command_rolls_back_inbox_when_event_append_fails(tmp_path):
    """事件写入失败时 Inbox 必须回滚：不允许 persisted-but-untracked 半状态。"""

    from tests.kernel.store_conformance import command

    service = LocalSessionService(db_path=tmp_path / "sessions.sqlite")
    await seed_session(service)()
    flaky = _FlakyEventStore(SessionServiceEventStore(service), fail_on=0)
    store = SQLiteAgentKernelStore(tmp_path / "kernel.sqlite", flaky)
    await store.ensure_schema()

    with pytest.raises(RuntimeError):
        await store.accept_command(command("rb-1", "hello"), queue_limit=4)

    import sqlite3

    with sqlite3.connect(tmp_path / "kernel.sqlite") as connection:
        rows = connection.execute("SELECT COUNT(*) FROM kernel_inbox").fetchone()[0]
        seq = connection.execute(
            "SELECT COUNT(*) FROM kernel_accepted_seq"
        ).fetchone()[0]
    assert rows == 0
    assert seq == 0

    # 重试（事件存储恢复后）能正常接受，无残留半状态。
    good = SQLiteAgentKernelStore(tmp_path / "kernel.sqlite", SessionServiceEventStore(service))
    await good.ensure_schema()
    receipt = await good.accept_command(command("rb-1", "hello"), queue_limit=4)
    assert receipt.status == "accepted"
    assert receipt.accepted_seq == 1
    await good.close()
    await store.close()
