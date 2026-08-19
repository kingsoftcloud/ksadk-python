# -*- coding: utf-8 -*-
"""InteractionLedger 跨后端 conformance（Phase 1 Task 5 Step 2）。

任何 InteractionLedger 实现（InMemory / SQLite / PostgreSQL AgentKernelStore）
都必须通过这里的全部行为断言：request 幂等、revision 递增、terminal
first-wins、重复相同 submission 返回既有 receipt、冲突 submission 抛
``interaction_already_resolved``、expire/cancel、tenant-session 绑定、
stale-fence 拒绝，以及公共事件省略内部 provider/native/continuation 字段。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from ksadk.events.session_event import SessionServiceEventStore
from ksadk.interaction.contracts import (
    InteractionRecord,
    InteractionSubmission,
)
from ksadk.interaction.ledger import (
    ALREADY_RESOLVED,
    REVISION_MISMATCH,
    REQUEST_CONFLICT,
)
from ksadk.kernel.contracts import ActivationWriteGuard
from ksadk.kernel.errors import AgentKernelError, InvalidCommandError, StaleFenceError
from ksadk.kernel.store import ActivationLeaseRequest

TENANT = "tenant-1"
AGENT = "agent-1"
SESSION = "s1"
OTHER_SESSION = "s2"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def make_record(
    interaction_id: str,
    *,
    session_id: str = SESSION,
    kind: str = "approval",
    request_schema: dict | None = None,
    expires_at: str | None = None,
) -> InteractionRecord:
    return InteractionRecord(
        interaction_id=interaction_id,
        tenant_id=TENANT,
        agent_instance_id=AGENT,
        session_id=session_id,
        run_id="run-1",
        kind=kind,  # type: ignore[arg-type]
        request_schema=request_schema or {"type": "object"},
        created_at=now_iso(),
        expires_at=expires_at,
        provider_id="codex",
        native_target={"call_id": "call_1"},
        continuation_metadata={"opaque": "encrypted-blob"},
    )


def make_submission(
    interaction_id: str,
    *,
    expected_revision: int = 1,
    action: str = "approve",
    response=None,
    idempotency_key: str = "idem-1",
) -> InteractionSubmission:
    return InteractionSubmission(
        interaction_id=interaction_id,
        expected_revision=expected_revision,
        action=action,  # type: ignore[arg-type]
        response=response if response is not None else {"approved": True},
        idempotency_key=idempotency_key,
    )


async def acquire_guard(store, activation_id: str = "act-int-1", session_id: str = SESSION):
    lease = await store.acquire_activation(
        ActivationLeaseRequest(
            agent_instance_id=AGENT,
            session_id=session_id,
            activation_id=activation_id,
            lease_ttl_seconds=60.0,
        )
    )
    return ActivationWriteGuard(
        activation_id=lease.activation_id, fencing_token=lease.fencing_token
    )


async def read_events(event_store, session_id: str = SESSION):
    return await event_store.read(session_id, 0, 1000)


def interaction_events(events, event_type: str):
    return [
        e
        for e in events
        if e.family == "interaction" and e.event_type == event_type
    ]


# ---------------------------------------------------------------- conformance


async def assert_request_is_idempotent(store, event_store):
    guard = await acquire_guard(store)
    record = make_record("int-1")
    first = await store.request(record, guard=guard)
    assert first.status == "pending"
    assert first.revision == 1

    replay = await store.request(make_record("int-1"), guard=guard)
    assert replay.interaction_id == "int-1"
    assert replay.revision == 1
    assert replay.status == "pending"
    # 幂等 replay 不产生第二条 requested 事件。
    events = await read_events(event_store)
    assert len(interaction_events(events, "interaction.requested")) == 1

    fetched = await store.get("int-1")
    assert fetched is not None
    assert fetched.provider_id == "codex"
    assert fetched.native_target == {"call_id": "call_1"}


async def assert_request_conflict_is_rejected(store, event_store):
    guard = await acquire_guard(store)
    await store.request(make_record("int-conflict"), guard=guard)
    conflicting = make_record("int-conflict", kind="structured_input")
    with pytest.raises(InvalidCommandError) as excinfo:
        await store.request(conflicting, guard=guard)
    assert excinfo.value.details.get("reason") == REQUEST_CONFLICT


async def assert_resolve_first_wins_with_revision_increment(store, event_store):
    guard = await acquire_guard(store)
    await store.request(make_record("int-fw"), guard=guard)

    receipt = await store.resolve(make_submission("int-fw"), guard=guard)
    assert receipt.status == "resolved"
    assert receipt.outcome == "approved"
    assert receipt.revision == 2
    assert receipt.event_id is not None

    record = await store.get("int-fw")
    assert record is not None
    assert record.status == "resolved"
    assert record.revision == 2

    # first-wins：第二个不同 submission 冲突。
    with pytest.raises(AgentKernelError) as excinfo:
        await store.resolve(
            make_submission("int-fw", idempotency_key="idem-2", response={"other": True}),
            guard=guard,
        )
    assert excinfo.value.details.get("reason") == ALREADY_RESOLVED

    events = await read_events(event_store)
    resolved = interaction_events(events, "interaction.resolved")
    assert len(resolved) == 1
    assert resolved[0].payload["outcome"] == "approved"
    assert resolved[0].payload["revision"] == 2


async def assert_repeated_identical_submission_returns_existing_receipt(store, event_store):
    guard = await acquire_guard(store)
    await store.request(make_record("int-idem"), guard=guard)
    submission = make_submission("int-idem", idempotency_key="idem-key")
    first = await store.resolve(submission, guard=guard)
    replay = await store.resolve(
        make_submission("int-idem", idempotency_key="idem-key"), guard=guard
    )
    assert replay.revision == first.revision
    assert str(replay.event_id) == str(first.event_id)
    assert replay.outcome == first.outcome

    events = await read_events(event_store)
    assert len(interaction_events(events, "interaction.resolved")) == 1


async def assert_stale_revision_is_rejected(store, event_store):
    guard = await acquire_guard(store)
    await store.request(make_record("int-stale"), guard=guard)
    with pytest.raises(AgentKernelError) as excinfo:
        await store.resolve(
            make_submission("int-stale", expected_revision=99), guard=guard
        )
    assert excinfo.value.details.get("reason") == REVISION_MISMATCH
    record = await store.get("int-stale")
    assert record is not None and record.status == "pending"


async def assert_cancel_is_terminal_and_fenced_by_revision(store, event_store):
    guard = await acquire_guard(store)
    await store.request(make_record("int-cancel"), guard=guard)
    with pytest.raises(AgentKernelError) as excinfo:
        await store.cancel("int-cancel", 99, guard=guard)
    assert excinfo.value.details.get("reason") == REVISION_MISMATCH

    receipt = await store.cancel("int-cancel", 1, guard=guard)
    assert receipt.status == "cancelled"
    assert receipt.outcome == "cancelled"
    assert receipt.revision == 2

    # 已取消的 interaction 不能再被 resolve。
    with pytest.raises(AgentKernelError) as excinfo:
        await store.resolve(make_submission("int-cancel"), guard=guard)
    assert excinfo.value.details.get("reason") == ALREADY_RESOLVED

    events = await read_events(event_store)
    cancelled = interaction_events(events, "interaction.cancelled")
    assert len(cancelled) == 1
    assert cancelled[0].payload["reason"]


async def assert_expire_is_terminal(store, event_store):
    guard = await acquire_guard(store)
    await store.request(make_record("int-exp"), guard=guard)
    receipt = await store.expire("int-exp", 1, guard=guard)
    assert receipt.status == "expired"
    assert receipt.outcome == "expired"
    assert receipt.revision == 2

    with pytest.raises(AgentKernelError):
        await store.expire("int-exp", 1, guard=guard)  # 已终态
    events = await read_events(event_store)
    assert len(interaction_events(events, "interaction.expired")) == 1


async def assert_tenant_session_binding(store, event_store):
    guard = await acquire_guard(store, session_id=SESSION)
    other_guard = await acquire_guard(store, activation_id="act-int-2", session_id=OTHER_SESSION)
    await store.request(make_record("int-a"), guard=guard)
    await store.request(make_record("int-b"), guard=guard)
    await store.request(make_record("int-other", session_id=OTHER_SESSION), guard=other_guard)

    pending = await store.list_pending_interactions(TENANT, SESSION)
    assert sorted(r.interaction_id for r in pending) == ["int-a", "int-b"]
    other_pending = await store.list_pending_interactions(TENANT, OTHER_SESSION)
    assert [r.interaction_id for r in other_pending] == ["int-other"]

    # resolve 后退出 pending 列表。
    await store.resolve(make_submission("int-a"), guard=guard)
    pending = await store.list_pending_interactions(TENANT, SESSION)
    assert [r.interaction_id for r in pending] == ["int-b"]

    assert await store.get("int-unknown-xyz") is None


async def assert_stale_fence_is_rejected(store, event_store):
    guard = await acquire_guard(store, activation_id="act-fence")
    await store.request(make_record("int-fence"), guard=guard)
    stale_guard = ActivationWriteGuard(
        activation_id=guard.activation_id, fencing_token=guard.fencing_token + 5
    )
    with pytest.raises(StaleFenceError):
        await store.request(make_record("int-fence-2"), guard=stale_guard)
    with pytest.raises(StaleFenceError):
        await store.resolve(make_submission("int-fence"), guard=stale_guard)
    with pytest.raises(StaleFenceError):
        await store.cancel("int-fence", 1, guard=stale_guard)
    with pytest.raises(StaleFenceError):
        await store.expire("int-fence", 1, guard=stale_guard)
    record = await store.get("int-fence")
    assert record is not None and record.status == "pending"
    assert await store.get("int-fence-2") is None


async def assert_public_events_omit_internal_fields(store, event_store):
    guard = await acquire_guard(store)
    await store.request(make_record("int-priv"), guard=guard)
    await store.resolve(make_submission("int-priv"), guard=guard)
    events = await read_events(event_store)
    for envelope in events:
        if envelope.family != "interaction":
            continue
        dumped = envelope.payload
        assert "provider_id" not in dumped
        assert "native_target" not in dumped
        assert "continuation_metadata" not in dumped


async def assert_concurrent_resolves_have_single_winner(store, event_store):
    guard = await acquire_guard(store)
    await store.request(make_record("int-race"), guard=guard)

    async def submit(key: str, response):
        return await store.resolve(
            make_submission("int-race", idempotency_key=key, response=response),
            guard=guard,
        )

    results = await asyncio.gather(
        submit("race-a", {"choice": "a"}),
        submit("race-b", {"choice": "b"}),
        return_exceptions=True,
    )
    winners = [r for r in results if not isinstance(r, BaseException)]
    losers = [r for r in results if isinstance(r, BaseException)]
    assert len(winners) == 1
    assert len(losers) == 1
    assert losers[0].details.get("reason") == ALREADY_RESOLVED  # type: ignore[attr-defined]

    record = await store.get("int-race")
    assert record is not None and record.status == "resolved"
    events = await read_events(event_store)
    assert len(interaction_events(events, "interaction.resolved")) == 1


LEDGER_CONFORMANCE_CHECKS = (
    assert_request_is_idempotent,
    assert_request_conflict_is_rejected,
    assert_resolve_first_wins_with_revision_increment,
    assert_repeated_identical_submission_returns_existing_receipt,
    assert_stale_revision_is_rejected,
    assert_cancel_is_terminal_and_fenced_by_revision,
    assert_expire_is_terminal,
    assert_tenant_session_binding,
    assert_stale_fence_is_rejected,
    assert_public_events_omit_internal_fields,
    assert_concurrent_resolves_have_single_winner,
)


# ------------------------------------------------------------------ fixtures


async def _seed(service, session_id: str):
    existing = await service.get_session(session_id)
    if existing is None:
        await service.create_session(
            agent_id=AGENT, user_id="kernel-user", session_id=session_id
        )


@pytest.fixture
async def memory_backend():
    from ksadk.kernel.memory_store import InMemoryAgentKernelStore
    from ksadk.sessions.in_memory import InMemorySessionService

    service = InMemorySessionService()
    await _seed(service, SESSION)
    await _seed(service, OTHER_SESSION)
    event_store = SessionServiceEventStore(service)
    yield InMemoryAgentKernelStore(event_store), event_store


@pytest.fixture
async def sqlite_backend(tmp_path):
    from ksadk.kernel.sqlite_store import SQLiteAgentKernelStore
    from ksadk.sessions.local_service import LocalSessionService

    service = LocalSessionService(db_path=tmp_path / "sessions.sqlite")
    await _seed(service, SESSION)
    await _seed(service, OTHER_SESSION)
    event_store = SessionServiceEventStore(service)
    store = SQLiteAgentKernelStore(tmp_path / "kernel.sqlite", event_store)
    await store.ensure_schema()
    yield store, event_store
    await store.close()


@pytest.fixture
async def postgres_backend():
    import os

    dsn = os.environ.get("KSADK_TEST_POSTGRES_DSN")
    if not dsn and os.environ.get("KSADK_TEST_POSTGRES_DOCKER") == "1":
        dsn = await start_temporary_postgres()
    if not dsn:
        pytest.skip(
            "KSADK_TEST_POSTGRES_DSN not set; Postgres ledger tests require a live database"
        )
    from ksadk.kernel.postgres_store import (
        PostgresAgentKernelStore,
        PostgresKernelEventLog,
    )
    from ksadk.sessions.postgres_service import PostgresSessionService

    service = PostgresSessionService(dsn=dsn)
    await _seed(service, SESSION)
    await _seed(service, OTHER_SESSION)
    event_log = PostgresKernelEventLog(service._pool)
    store = PostgresAgentKernelStore(service._pool, event_log)
    await store.ensure_schema()
    yield store, event_log
    await store.reset_for_tests()
    await service.aclose()


_TEMP_CONTAINER = [""]


async def start_temporary_postgres() -> str | None:
    """``KSADK_TEST_POSTGRES_DOCKER=1`` 时用 docker 起临时 postgres:16。"""

    import asyncio
    import socket
    import subprocess
    import time

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    container = f"ksadk-test-pg-{port}"
    _TEMP_CONTAINER[0] = container
    result = subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", container,
            "-e", "POSTGRES_PASSWORD=postgres",
            "-p", f"{port}:5432",
            "postgres:16",
        ],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        pytest.skip(f"docker unavailable: {result.stderr.decode()[:200]}")
    dsn = f"postgresql://postgres:postgres@127.0.0.1:{port}/postgres"
    import asyncpg

    for _ in range(120):
        try:
            conn = await asyncpg.connect(dsn)
            await conn.close()
            return dsn
        except Exception:
            await asyncio.sleep(0.5)
    subprocess.run(["docker", "kill", container], check=False, capture_output=True)
    pytest.fail("temporary postgres:16 container did not become ready")


BACKENDS = ["memory", "sqlite", "postgres"]


@pytest.mark.parametrize("check", LEDGER_CONFORMANCE_CHECKS)
async def test_memory_ledger_conformance(memory_backend, check):
    store, event_store = memory_backend
    await check(store, event_store)


@pytest.mark.parametrize("check", LEDGER_CONFORMANCE_CHECKS)
async def test_sqlite_ledger_conformance(sqlite_backend, check):
    store, event_store = sqlite_backend
    await check(store, event_store)


@pytest.mark.parametrize("check", LEDGER_CONFORMANCE_CHECKS)
async def test_postgres_ledger_conformance(postgres_backend, check):
    store, event_store = postgres_backend
    await check(store, event_store)


async def test_memory_store_satisfies_ledger_protocol(memory_backend):
    from ksadk.interaction.ledger import InteractionLedger

    store, _ = memory_backend
    assert isinstance(store, InteractionLedger)


async def test_sqlite_store_satisfies_ledger_protocol(sqlite_backend):
    from ksadk.interaction.ledger import InteractionLedger

    store, _ = sqlite_backend
    assert isinstance(store, InteractionLedger)
