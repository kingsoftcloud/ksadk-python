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
