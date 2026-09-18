# -*- coding: utf-8 -*-
"""PostgreSQL interaction 台账 crash-window 原子性测试（Kernel Task 5 Step 5）。

需要真实 PostgreSQL：``KSADK_TEST_POSTGRES_DSN``（未提供时明确 skip）。
可选 ``KSADK_TEST_POSTGRES_DOCKER=1`` 时尝试用 docker 起临时 postgres:16。
"""

from __future__ import annotations

import os

import pytest

from ksadk.kernel.contracts import ActivationWriteGuard
from ksadk.kernel.errors import AgentKernelError
from tests.interaction.test_ledger_conformance import (
    AGENT,
    SESSION,
    acquire_guard,
    interaction_events,
    make_record,
    make_submission,
    start_temporary_postgres,
    stop_temporary_postgres,
)


def _dsn() -> str | None:
    return os.environ.get("KSADK_TEST_POSTGRES_DSN") or None


@pytest.fixture(scope="module")
async def pg_dsn():
    dsn = _dsn()
    if not dsn and os.environ.get("KSADK_TEST_POSTGRES_DOCKER") == "1":
        dsn = await start_temporary_postgres()
    if not dsn:
        pytest.skip(
            "KSADK_TEST_POSTGRES_DSN not set (and KSADK_TEST_POSTGRES_DOCKER != 1);"
            " Postgres interaction atomicity tests require a live database"
        )
    yield dsn
    if os.environ.get("KSADK_TEST_POSTGRES_DOCKER") == "1" and _dsn() is None:
        stop_temporary_postgres()


@pytest.fixture
async def pg_store(pg_dsn):
    from ksadk.kernel.postgres_store import (
        PostgresAgentKernelStore,
        PostgresKernelEventLog,
    )
    from ksadk.sessions.postgres_service import PostgresSessionService

    service = PostgresSessionService(dsn=pg_dsn)
    existing = await service.get_session(SESSION)
    if existing is None:
        await service.create_session(
            agent_id=AGENT, user_id="kernel-user", session_id=SESSION
        )
    event_log = PostgresKernelEventLog(service._pool)
    store = PostgresAgentKernelStore(service._pool, event_log)
    await store.ensure_schema()
    yield store, event_log, service
    await store.reset_for_tests()
    await service.aclose()


async def _seed_pending(store, guard: ActivationWriteGuard, interaction_id: str) -> None:
    await store.request(make_record(interaction_id), guard=guard)


async def test_event_failure_rolls_back_terminal_row(pg_store, monkeypatch):
    """terminal 决策与 SessionEvent 同事务：事件写入失败 -> 行仍 pending。"""
    store, event_log, _ = pg_store
    guard = await acquire_guard(store, activation_id="act-int-atomic")
    await _seed_pending(store, guard, "int-atomic")

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated crash before event commit")

    monkeypatch.setattr(event_log, "append_on", boom)
    with pytest.raises(RuntimeError):
        await store.resolve(make_submission("int-atomic"), guard=guard)

    record = await store.get("int-atomic")
    assert record is not None
    assert record.status == "pending"
    assert record.revision == 1
    # 没有 terminal 事实残留。
    events = await event_log.read(SESSION, 0, 1000)
    assert interaction_events(events, "interaction.resolved") == []


async def test_crash_between_wins_leaves_no_double_terminal(pg_store, monkeypatch):
    """第二个 terminal 决策必须被 first-wins 拒绝，不能出现两个成功 terminal。"""
    store, event_log, _ = pg_store
    guard = await acquire_guard(store, activation_id="act-int-crash")
    await _seed_pending(store, guard, "int-crash")

    first = await store.resolve(
        make_submission("int-crash", idempotency_key="win-a", response={"a": 1}),
        guard=guard,
    )
    assert first.status == "resolved"

    # 即便换一个 idempotency key / action，terminal 之后一律拒绝。
    for action, key in (("reject", "win-b"), ("submit", "win-c")):
        with pytest.raises(AgentKernelError) as excinfo:
            await store.resolve(
                make_submission(
                    "int-crash", action=action, idempotency_key=key, response={"x": 1}
                ),
                guard=guard,
            )
        assert excinfo.value.details.get("reason") == "interaction_already_resolved"
    with pytest.raises(AgentKernelError):
        await store.cancel("int-crash", 1, guard=guard)

    record = await store.get("int-crash")
    assert record is not None
    assert record.status == "resolved"
    assert record.revision == 2

    events = await event_log.read(SESSION, 0, 1000)
    assert len(interaction_events(events, "interaction.resolved")) == 1
    assert interaction_events(events, "interaction.cancelled") == []
    assert interaction_events(events, "interaction.expired") == []


async def test_request_event_failure_rolls_back_pending_row(pg_store, monkeypatch):
    store, event_log, _ = pg_store
    guard = await acquire_guard(store, activation_id="act-int-reqfail")

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated crash on requested event")

    monkeypatch.setattr(event_log, "append_on", boom)
    with pytest.raises(RuntimeError):
        await store.request(make_record("int-reqfail"), guard=guard)

    assert await store.get("int-reqfail") is None
    assert await store.list_pending_interactions("tenant-1", SESSION) == []
