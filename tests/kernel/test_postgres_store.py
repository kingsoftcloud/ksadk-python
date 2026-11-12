# -*- coding: utf-8 -*-
"""PostgresAgentKernelStore fencing / 并发 / 故障注入测试（Kernel Task 4）。

需要真实 PostgreSQL：``KSADK_TEST_POSTGRES_DSN``。未提供 DSN 时所有用例
明确 skip（skip 不等于通过；CI/预发门禁必须提供 DSN）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from datetime import UTC, datetime

import pytest

from ksadk.kernel.contracts import SessionEventEnvelope
from ksadk.kernel.errors import AgentKernelError, InvalidCommandError, StaleFenceError
from ksadk.kernel.state import InboxState
from tests.interaction.test_ledger_conformance import postgres_backend, postgres_dsn  # noqa: F401
from tests.postgres_isolation import isolated_postgres_dsn
from tests.kernel.store_conformance import (
    AGENT,
    CONFORMANCE_CHECKS,
    SESSION,
    assert_concurrent_accepts_have_no_loss_or_duplicate,
    command,
    lease_request,
)
from tests.interaction.test_ledger_conformance import ledger_postgres_server_dsn  # noqa: F401


def _dsn() -> str | None:
    return os.environ.get("KSADK_TEST_POSTGRES_DSN") or None


@pytest.fixture(scope="module")
def kernel_postgres_server_dsn():
    dsn = _dsn()
    if not dsn:
        pytest.skip(
        "KSADK_TEST_POSTGRES_DSN not set; Postgres kernel tests require a live database"
    )
    return dsn


@pytest.fixture
async def pg_dsn(kernel_postgres_server_dsn):
    async with isolated_postgres_dsn(kernel_postgres_server_dsn) as dsn:
        yield dsn


@pytest.fixture
async def pg_store(pg_dsn):
    from ksadk.kernel.postgres_store import (
        PostgresAgentKernelStore,
        PostgresKernelEventLog,
    )
    from ksadk.sessions.postgres_service import PostgresSessionService

    service = PostgresSessionService(dsn=pg_dsn)
    await service.create_session(agent_id=AGENT, user_id="kernel-user", session_id=SESSION)
    event_log = PostgresKernelEventLog(service._pool)
    store = PostgresAgentKernelStore(service._pool, event_log)
    await store.ensure_schema()
    yield store
    await store.reset_for_tests()
    await service.aclose()


async def expire_lease(store, lease):
    """直接把 lease 过期时间回拨，模拟 TTL 耗尽（不经 sleep）。"""
    await store._pool.execute(
        "UPDATE kernel_activations SET lease_expires_at = now() - interval '1 second' "
        "WHERE activation_id = $1",
        lease.activation_id,
    )


async def test_kernel_and_interaction_fixtures_have_independent_reset_tables(pg_store, postgres_backend):
    other, _ = postgres_backend
    schema = await pg_store._pool.fetchval("SELECT current_schema()")
    other_schema = await other._pool.fetchval("SELECT current_schema()")
    # Check the boundary before either fixture performs a destructive reset.
    assert schema != other_schema, "independent fixture resets must use different schemas"
    receipt = await pg_store.accept_command(command("schema-isolation", "preserved"), queue_limit=4)
    await other.reset_for_tests()
    assert await pg_store.load_message(str(receipt.message_id)) is not None


async def test_multiple_instances_serialize_kernel_schema_initialization(pg_store, pg_dsn):
    import asyncpg

    from ksadk.kernel.postgres_store import PostgresAgentKernelStore, PostgresKernelEventLog

    # A database-wide migration gate must coordinate independent processes,
    # not merely serialize coroutines using the same Python store/pool.
    key = 0x4B5341444B45524E
    application = "schema-gate-" + uuid.uuid4().hex
    held = await asyncpg.connect(pg_dsn)
    first_pool = await asyncpg.create_pool(
        pg_dsn, min_size=1, max_size=2, server_settings={"application_name": application}
    )
    pool = await asyncpg.create_pool(
        pg_dsn, min_size=1, max_size=2, server_settings={"application_name": application}
    )
    first = PostgresAgentKernelStore(first_pool, PostgresKernelEventLog(first_pool))
    second = PostgresAgentKernelStore(pool, PostgresKernelEventLog(pool))
    transaction = held.transaction()
    await transaction.start()
    await held.execute("SELECT pg_advisory_xact_lock($1::bigint)", key)
    tasks = [asyncio.create_task(store.ensure_schema()) for store in (first, second)]
    try:
        async with asyncio.timeout(2):
            while True:
                waiters = await held.fetchval(
                    """SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid
                    WHERE l.locktype='advisory' AND NOT l.granted
                      AND l.classid=$1::oid AND l.objid=$2::oid
                      AND l.database=(SELECT oid FROM pg_database WHERE datname=current_database())
                      AND a.application_name=$3""",
                    key >> 32,
                    key & 0xFFFFFFFF,
                    application,
                )
                if waiters == 2:
                    break
                if any(task.done() for task in tasks):
                    pytest.fail("A schema initializer bypassed the shared migration gate")
                await asyncio.sleep(0.01)
        assert not any(task.done() for task in tasks)
        await transaction.rollback()
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
        assert (
            await held.fetchval("SELECT to_regclass('kernel_interactions')")
            == "kernel_interactions"
        )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if held.is_in_transaction():
            await transaction.rollback()
        await held.close()
        await first_pool.close()
        await pool.close()


async def test_legacy_admission_cursor_migration_preserves_history_and_fifo(pg_store):
    from tests.kernel.store_conformance import (
        assert_admission_receipt_uses_session_cursor_and_original_command,
    )

    await assert_admission_receipt_uses_session_cursor_and_original_command(pg_store)
    rows = await pg_store.list_messages(AGENT)
    seqs = [row.accepted_seq for row in rows]
    facts = await pg_store._events.read(SESSION, 0, 100)
    async with pg_store._pool.acquire() as connection:
        for ordinal, row in enumerate(rows, 1):
            await connection.execute(
                "UPDATE kernel_inbox SET accepted_seq=$1 WHERE message_id=$2::uuid",
                ordinal, str(row.message_id),
            )
    await pg_store.ensure_schema()
    await pg_store.ensure_schema()
    assert [row.accepted_seq for row in await pg_store.list_messages(AGENT)] == seqs
    assert await pg_store._events.read(SESSION, 0, 100) == facts


def runtime_envelope(session_id: str = SESSION) -> SessionEventEnvelope:
    return SessionEventEnvelope(
        event_id=uuid.uuid4(),
        session_id=session_id,
        seq=0,
        timestamp=datetime.now(UTC).isoformat(),
        family="runtime",
        family_version=2,
        event_type="run.started",
        payload={
            "schema_version": 2,
            "event_id": f"evt_{uuid.uuid4().hex}",
            "seq": 0,
            "timestamp": time.time(),
            "run_id": "run-1",
            "run_seq": 1,
            "scope_id": "scope-1",
            "source": {"framework": "ksadk"},
            "status": "running",
        },
    )


# --------------------------------------------------------------- Step 1 tests


async def test_canonical_lookup_uses_index_without_weakening_duplicate_fence(pg_store, monkeypatch):
    from ksadk.events.canonical import RunStarted, SourceRef
    from ksadk.events.canonical_store import RuntimeEventStore, runtime_event_envelope_id
    from ksadk.events.run_authority import RunEventAuthority, bind_run_event_authority
    from ksadk.kernel.contracts import ActivationWriteGuard
    from ksadk.kernel.postgres_store import PostgresFencedSessionEventStore, PostgresKernelEventLog

    lease = await pg_store.acquire_activation(lease_request("lookup-owner"))
    guard = ActivationWriteGuard(
        activation_id=lease.activation_id, fencing_token=lease.fencing_token
    )
    events = PostgresFencedSessionEventStore(pg_store)
    authority = RunEventAuthority(events, session_id=SESSION, run_id="lookup-run", guard=guard)
    fact = RunStarted(schema_version=2, event_id="lookup-freeform-id", seq=0,
                      timestamp=time.time(), run_id="lookup-run", scope_id="lookup-scope",
                      source=SourceRef(framework="langgraph"), status="running")

    async def no_history_scan(*args, **kwargs):
        pytest.fail("single-event idempotency must not scan session history")

    monkeypatch.setattr(events, "read", no_history_scan)
    with bind_run_event_authority(authority):
        stored, created = await authority.persist_one(SESSION, fact)
        assert created
        duplicate, created = await authority.persist_one(SESSION, fact)
        assert not created
        assert duplicate == stored
        view = RuntimeEventStore(events, session_id=SESSION)
        assert await view.event_by_id(SESSION, "missing-event") is None
        assert await view.event_by_id("another-session", fact.event_id) is None
        other_namespace = PostgresKernelEventLog(pg_store._pool, namespace="other-namespace")
        assert await other_namespace.get_by_id(
            SESSION, runtime_event_envelope_id(SESSION, fact.event_id)
        ) is None
        with pytest.raises(ValueError):
            await authority.persist_one(
                SESSION, fact.model_copy(update={"timestamp": fact.timestamp + 1})
            )
        await expire_lease(pg_store, lease)
        await pg_store.acquire_activation(lease_request("replacement-owner"))
        # Finding an existing event never allows a stale owner to publish it.
        with pytest.raises(StaleFenceError):
            await authority.persist_one(SESSION, fact)


async def test_takeover_fences_old_owner(pg_store):
    old = await pg_store.acquire_activation(lease_request("pod-a"))
    await expire_lease(pg_store, old)
    new = await pg_store.acquire_activation(lease_request("pod-b"))
    assert new.fencing_token == old.fencing_token + 1
    with pytest.raises(StaleFenceError):
        await pg_store.append_event(runtime_envelope(), expected_fence=old.fencing_token)
    # 旧 owner 的 renew / claim / complete / run transition 全部被 fence 挡住。
    with pytest.raises(AgentKernelError):
        await pg_store.renew_activation(
            old.activation_id, expected_fence=old.fencing_token, lease_ttl_seconds=30
        )
    with pytest.raises(StaleFenceError):
        await pg_store.claim_next(AGENT, SESSION, old.fencing_token)
    # 新 owner 正常写。
    persisted = await pg_store.append_event(
        runtime_envelope(), expected_fence=new.fencing_token
    )
    assert persisted.seq >= 1


async def test_shared_event_log_uses_the_session_service_scope(pg_dsn):
    """Kernel accepted/runtime events must share the deployed session scope.

    A default-scoped event log lets bootstrap succeed but rejects the first
    command from an AgentInstance-scoped SessionService.  Exercise the real
    Postgres tables rather than asserting constructor arguments.
    """
    from ksadk.kernel.postgres_store import PostgresKernelEventLog
    from ksadk.sessions.postgres_service import PostgresSessionService

    namespace = f"pytest_kernel_scope_{uuid.uuid4().hex}"
    session_id = "scope-session"
    service = PostgresSessionService(
        dsn=pg_dsn,
        namespace=namespace,
        tenant_id="tenant-scope",
        workspace_id="workspace-scope",
    )
    try:
        await service.create_session(
            agent_id=AGENT, user_id="kernel-user", session_id=session_id
        )
        event_log = PostgresKernelEventLog(
            service._pool,
            namespace=service.namespace,
            tenant_id=service.tenant_id,
            workspace_id=service.workspace_id,
        )
        stored = await event_log.append(runtime_envelope(session_id))
        assert stored.session_id == session_id
        recovered = await event_log.read(session_id, after_seq=0, limit=10)
        assert recovered[0].event_id == stored.event_id
        row = await service._pool.fetchrow(
            "SELECT tenant_id, workspace_id FROM ksadk_events "
            "WHERE namespace=$1 AND session_id=$2",
            namespace,
            session_id,
        )
        assert dict(row) == {"tenant_id": "tenant-scope", "workspace_id": "workspace-scope"}
    finally:
        if service._pool is not None:
            await service._pool.execute("DELETE FROM ksadk_events WHERE namespace=$1", namespace)
            await service._pool.execute("DELETE FROM ksadk_sessions WHERE namespace=$1", namespace)
        await service.aclose()


async def test_append_with_stale_fence_writes_no_event(pg_store):
    lease = await pg_store.acquire_activation(lease_request("pod-guard"))
    before = await pg_store._events.read(SESSION, 0, 1000)
    with pytest.raises(StaleFenceError):
        await pg_store.append_event(runtime_envelope(), expected_fence=lease.fencing_token + 5)
    after = await pg_store._events.read(SESSION, 0, 1000)
    assert [e.event_id for e in after] == [e.event_id for e in before]


async def test_concurrent_acquire_single_winner(pg_store):
    a = await pg_store.acquire_activation(lease_request("pod-race-a", ttl=30.0))
    with pytest.raises(InvalidCommandError):
        await pg_store.acquire_activation(lease_request("pod-race-b", ttl=30.0))
    same = await pg_store.acquire_activation(lease_request("pod-race-a", ttl=30.0))
    assert same.fencing_token == a.fencing_token


async def test_release_then_reacquire_bumps_token(pg_store):
    first = await pg_store.acquire_activation(lease_request("pod-rel"))
    await pg_store.release_activation(first.activation_id, expected_fence=first.fencing_token)
    with pytest.raises(StaleFenceError):
        await pg_store.renew_activation(
            first.activation_id, expected_fence=first.fencing_token, lease_ttl_seconds=5
        )
    second = await pg_store.acquire_activation(lease_request("pod-rel-2"))
    assert second.fencing_token == first.fencing_token + 1


# ------------------------------------------------------------ conformance


@pytest.mark.parametrize("check", CONFORMANCE_CHECKS)
async def test_postgres_store_conformance(pg_store, check):
    await check(pg_store)


async def test_postgres_store_concurrency(pg_store):
    # The configured acquire/lock budgets intentionally reject
    # 100 simultaneous waiters on one session. Keep 100 durable accepts while
    # exercising bounded contention in small concurrent groups.
    await assert_concurrent_accepts_have_no_loss_or_duplicate(
        pg_store, parallelism=4,
    )


@pytest.mark.asyncio
async def test_postgres_kernel_pool_exhaustion_is_bounded_and_recovers(pg_store, monkeypatch):
    from contextlib import AsyncExitStack
    monkeypatch.setenv("KSADK_DB_READ_ACQUIRE_SECONDS", ".3")

    held = []
    async with AsyncExitStack() as stack:
        for _ in range(pg_store._pool.get_max_size()):
            held.append(await stack.enter_async_context(pg_store._pool.acquire()))
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await pg_store.load_runs_for_history(
                ["missing"], tenant_id=pg_store.tenant_id,
                agent_instance_id=AGENT, session_id=SESSION,
            )
        assert time.monotonic() - started < 1.0
        assert len(held) == pg_store._pool.get_max_size()
    recovered = await pg_store.load_runs_for_history(
        ["missing"], tenant_id=pg_store.tenant_id,
        agent_instance_id=AGENT, session_id=SESSION,
    )
    assert recovered == {}


async def test_postgres_claim_message_rejects_fifo_skip(pg_store):
    first = await pg_store.accept_command(command("fifo-1", "first"), queue_limit=4)
    second = await pg_store.accept_command(command("fifo-2", "second"), queue_limit=4)
    lease = await pg_store.acquire_activation(lease_request("act-fifo"))

    with pytest.raises(InvalidCommandError, match="FIFO enqueue head"):
        await pg_store.claim_message(str(second.message_id), lease.fencing_token)

    claimed = await pg_store.claim_message(str(first.message_id), lease.fencing_token)
    assert claimed.accepted_seq == 1
    await pg_store.complete_claim(str(first.message_id), expected_fence=lease.fencing_token)
    claimed_second = await pg_store.claim_message(
        str(second.message_id), lease.fencing_token
    )
    assert claimed_second.accepted_seq == 2


async def test_postgres_claim_message_reclaims_stale_fence_head(pg_store):
    receipt = await pg_store.accept_command(command("reclaim-1", "first"), queue_limit=4)
    old = await pg_store.acquire_activation(lease_request("old-owner"))
    claimed = await pg_store.claim_message(str(receipt.message_id), old.fencing_token)
    assert claimed.claimed_fence == old.fencing_token

    async with pg_store._connection() as connection:
        await connection.execute(
            "UPDATE kernel_activations SET lease_expires_at=now()-interval '1 second'"
            " WHERE agent_instance_id=$1 AND session_id=$2",
            AGENT,
            SESSION,
        )
    new = await pg_store.acquire_activation(lease_request("new-owner"))
    pending = await pg_store.list_pending(
        AGENT, SESSION, fencing_token=new.fencing_token
    )
    assert [message.message_id for message in pending] == [str(receipt.message_id)]
    reclaimed = await pg_store.claim_message(
        str(receipt.message_id), new.fencing_token
    )
    assert reclaimed.claimed_fence == new.fencing_token
    await pg_store.complete_claim(
        str(receipt.message_id), expected_fence=new.fencing_token
    )


async def test_postgres_store_control_events_share_session_log(pg_store):
    await pg_store.accept_command(command("k1", "hello"), queue_limit=4)
    lease = await pg_store.acquire_activation(lease_request("act-log"))
    message = await pg_store.claim_next(AGENT, SESSION, lease.fencing_token)
    assert message is not None
    await pg_store.complete_claim(message.message_id, expected_fence=lease.fencing_token)
    events = await pg_store._events.read(SESSION, 0, 50)
    types = [event.event_type for event in events]
    assert "control.command_accepted" in types
    assert "control.message_claimed" in types
    assert "control.message_completed" in types
    assert all(event.family == "control" and event.family_version == 1 for event in events)


async def test_postgres_store_satisfies_port_protocol(pg_store):
    from ksadk.kernel.store import AgentKernelStore

    assert isinstance(pg_store, AgentKernelStore)


# --------------------------------------- Step 5: 进程级并发 / 故障注入


WORKER_SOURCE = r"""
import asyncio, json, os, sys

sys.path.insert(0, os.getcwd())

from ksadk.kernel.contracts import AgentControlCommand, ControlSource
from ksadk.kernel.postgres_store import PostgresAgentKernelStore, PostgresKernelEventLog
from ksadk.kernel.store import command_digest, control_event

DSN = os.environ["KSADK_TEST_POSTGRES_DSN"]


def build_command():
    payload = json.loads(os.environ["WORKER_PAYLOAD"])
    return AgentControlCommand.model_validate(payload)


async def main():
    mode = sys.argv[1]
    async def connect():
        import asyncpg
        return await asyncpg.connect(DSN)

    if mode == "accept-and-hold":
        # accept commit 之后保持连接但不 ack 任何东西；随后被 parent kill。
        conn = await connect()
        store = PostgresAgentKernelStore(conn, PostgresKernelEventLog(conn), owns_pool=False)
        await store.ensure_schema()
        receipt = await store.accept_command(build_command(), queue_limit=8)
        out = {"status": receipt.status, "message_id": str(receipt.message_id)}
        print(json.dumps(out), flush=True)
        await asyncio.sleep(60)

    elif mode == "claim-in-tx":
        # 在事务内完成 inbox claim + ControlEvent insert，但 commit 前被 kill。
        conn = await connect()
        store = PostgresAgentKernelStore(conn, PostgresKernelEventLog(conn), owns_pool=False)
        await store.ensure_schema()
        tr = conn.transaction()
        await tr.start()
        activation = await conn.fetchrow(
            "SELECT activation_id, fencing_token FROM kernel_activations "
            "WHERE agent_instance_id=$1 AND session_id=$2",
            os.environ["WORKER_AGENT"], os.environ["WORKER_SESSION"],
        )
        row = await conn.fetchrow(
            "SELECT message_id, status FROM kernel_inbox "
            "WHERE agent_instance_id=$1 AND session_id=$2 AND status='accepted' "
            "ORDER BY accepted_seq FOR UPDATE SKIP LOCKED LIMIT 1",
            os.environ["WORKER_AGENT"], os.environ["WORKER_SESSION"],
        )
        await conn.execute(
            "UPDATE kernel_inbox SET status='claimed', claimed_fence=$1 WHERE message_id=$2",
            activation["fencing_token"], row["message_id"],
        )
        await PostgresKernelEventLog(conn).append_on(
            conn,
            control_event(
                session_id=os.environ["WORKER_SESSION"],
                event_type="control.message_claimed",
                payload={"message_id": str(row["message_id"]), "in_tx": True},
            ),
            guard=None,
        )
        print(json.dumps({"message_id": str(row["message_id"])}), flush=True)
        await asyncio.sleep(60)

    elif mode == "acquire":
        conn = await connect()
        store = PostgresAgentKernelStore(conn, PostgresKernelEventLog(conn), owns_pool=False)
        await store.ensure_schema()
        from ksadk.kernel.store import ActivationLeaseRequest
        request = ActivationLeaseRequest(
            agent_instance_id=os.environ["WORKER_AGENT"],
            session_id=os.environ["WORKER_SESSION"],
            activation_id=sys.argv[2],
            lease_ttl_seconds=float(sys.argv[3]),
        )
        from ksadk.kernel.errors import InvalidCommandError
        try:
            lease = await store.acquire_activation(request)
        except InvalidCommandError:
            print(json.dumps({"lease_denied": True}), flush=True)
            await conn.close()
            return
        print(json.dumps({"fencing_token": lease.fencing_token}), flush=True)
        await asyncio.sleep(60)

asyncio.run(main())
"""


async def _run_worker(pg_dsn, mode: str, *args: str, extra_env: dict | None = None,
                      kill_after: float = 3.0) -> tuple[str | None, int, str]:
    env = dict(os.environ)
    env["KSADK_TEST_POSTGRES_DSN"] = pg_dsn
    env.update(extra_env or {})
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", WORKER_SOURCE, mode, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )
    line = None
    try:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=kill_after)
        await asyncio.sleep(0.3)  # 让 worker 进入 sleep/hold 阶段
    except asyncio.TimeoutError:
        pass
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    stderr = await proc.stderr.read()
    await proc.wait()
    return (line.decode().strip() if line else None), proc.returncode, stderr.decode()


async def test_process_lease_survives_owner_death(pg_store, pg_dsn):
    first_line, _, _ = await _run_worker(
        pg_dsn, "acquire", "proc-a", "30", kill_after=10,
        extra_env={"WORKER_AGENT": AGENT, "WORKER_SESSION": SESSION},
    )
    assert first_line is not None
    import json as _json
    first_token = _json.loads(first_line)["fencing_token"]

    # 第二个进程在 lease 有效期内抢不到。
    line, rc, _ = await _run_worker(
        pg_dsn, "acquire", "proc-b", "30", kill_after=5,
        extra_env={"WORKER_AGENT": AGENT, "WORKER_SESSION": SESSION},
    )
    assert rc == 0
    assert _json.loads(line) == {"lease_denied": True}

    # 本进程（第三个 owner）在过期后 takeover，token 原子 +1。
    await pg_store._pool.execute(
        "UPDATE kernel_activations SET lease_expires_at = now() - interval '1 second'"
    )
    takeover = await pg_store.acquire_activation(lease_request("proc-c"))
    assert takeover.fencing_token == first_token + 1
    with pytest.raises(StaleFenceError):
        await pg_store.claim_next(AGENT, SESSION, first_token)


async def test_accept_commit_then_disconnect_retry_is_duplicate(pg_store, pg_dsn):
    cmd = command("k-proc", "retry-me")
    line, _, stderr = await _run_worker(
        pg_dsn, "accept-and-hold", kill_after=10,
        extra_env={"WORKER_PAYLOAD": cmd.model_dump_json()},
    )
    import json as _json
    payload = _json.loads(line)
    assert payload["status"] == "accepted"
    # 断链（kill）后由本进程重试同一 command：必须 duplicate 而非二次入队。
    retry = await pg_store.accept_command(cmd, queue_limit=8)
    assert retry.status == "duplicate"
    assert str(retry.message_id) == payload["message_id"]


async def test_kill_writer_before_commit_leaves_no_half_state(pg_store, pg_dsn):
    cmd = command("k-half", "half-state")
    await pg_store.accept_command(cmd, queue_limit=8)
    lease = await pg_store.acquire_activation(lease_request("pod-half"))
    line, _, stderr = await _run_worker(
        pg_dsn, "claim-in-tx", kill_after=10,
        extra_env={"WORKER_AGENT": AGENT, "WORKER_SESSION": SESSION},
    )
    assert line is not None, f"worker failed: {stderr}"  # 事务内写入完成但未 commit 即被 kill
    # 无半状态：claim 未提交、ControlEvent 也不存在。
    row = await pg_store._pool.fetchrow(
        "SELECT status FROM kernel_inbox WHERE idempotency_key=$1", cmd.idempotency_key
    )
    assert row["status"] == InboxState.ACCEPTED.value
    events = await pg_store._events.read(SESSION, 0, 200)
    assert all(
        not (event.event_type == "control.message_claimed"
             and event.payload.get("in_tx") is True)
        for event in events
    )
    # 存活 owner 仍可正常 claim（FOR UPDATE SKIP LOCKED 释放后无残留锁）。
    message = await pg_store.claim_next(AGENT, SESSION, lease.fencing_token)
    assert message is not None
    assert message.idempotency_key == cmd.idempotency_key


@pytest.mark.asyncio
async def test_history_batch_lookup_scopes_100_plus_ids(pg_store):
    """Both lookup types page at 100 IDs and reject mixed tenant/instance/session rows."""
    import json
    from types import SimpleNamespace

    from ksadk.kernel.history_projection import project_inbox_inputs
    from tests.kernel.store_conformance import TENANT

    # This fixture writes rows directly into TENANT rather than using the
    # store admission path; configure the reader for that same partition.
    pg_store.tenant_id = TENANT
    marker = uuid.uuid4().hex[:10]
    messages = []
    runs = []
    events = []
    valid_message_ids = []
    valid_run_ids = []
    for index in range(124):
        request = command(f"history-{marker}-{index}", f"input {index}")
        message_id = str(uuid.uuid4())
        run_id = f"history-run-{marker}-{index}"
        tenant = TENANT if index < 120 else "tenant-other"
        instance = AGENT if index < 121 else "instance-other"
        session = SESSION if index < 122 else "session-other"
        messages.append((message_id, tenant, instance, session,
                         request.idempotency_key, "digest", index + 1,
                         "accepted", request.model_dump_json()))
        runs.append((run_id, tenant, instance, session, "pending",
                     json.dumps({"command_id": str(request.command_id)})))
        events.append({
            "EventId": f"history-event-{index}", "SessionId": SESSION,
            "SeqId": index + 1, "InvocationId": run_id,
            "EventType": "control.command_accepted",
            "Content": {"session_event": {"family": "control",
                "event_type": "control.command_accepted", "payload": {
                    "command_type": "enqueue", "command_id": str(request.command_id),
                    "message_id": message_id}}},
        })
        if index < 120:
            valid_message_ids.append(message_id)
            valid_run_ids.append(run_id)
    await pg_store._pool.executemany(
        "INSERT INTO kernel_inbox (message_id,tenant_id,agent_instance_id,session_id,"
        "idempotency_key,request_digest,accepted_seq,status,payload) "
        "VALUES ($1::uuid,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)", messages,
    )
    await pg_store._pool.executemany(
        "INSERT INTO kernel_runs (run_id,tenant_id,agent_instance_id,session_id,state,metadata) "
        "VALUES ($1,$2,$3,$4,$5,$6::jsonb)", runs,
    )
    loaded_messages = await pg_store.load_messages_for_history(
        [row[0] for row in messages], tenant_id=TENANT,
        agent_instance_id=AGENT, session_id=SESSION,
    )
    loaded_runs = await pg_store.load_runs_for_history(
        [row[0] for row in runs], tenant_id=TENANT,
        agent_instance_id=AGENT, session_id=SESSION,
    )
    assert set(loaded_messages) == set(valid_message_ids)
    assert set(loaded_runs) == set(valid_run_ids)
    runtime = SimpleNamespace(
        config=SimpleNamespace(tenant_id=TENANT, agent_instance_id=AGENT),
        kernel_store=pg_store,
    )
    cache = {}
    projected = await project_inbox_inputs(
        events, runtime=runtime, session_id=SESSION, tenant_id=TENANT,
        request_cache=cache,
    )
    assert [event["EventType"] for event in projected].count("user_message") == 120
    assert len(cache["messages"]) == 124
    assert len(cache["runs"]) == 124
    # The extended window fetches only the previously unseen ID.
    extra = {**events[0], "EventId": "history-missing-extra",
             "Content": {"session_event": {"family": "control",
                "event_type": "control.command_accepted", "payload": {
                    "command_type": "enqueue", "command_id": str(uuid.uuid4()),
                    "message_id": str(uuid.uuid4())}}}}
    await project_inbox_inputs(events + [extra], runtime=runtime,
                               session_id=SESSION, tenant_id=TENANT,
                               request_cache=cache)
    assert len(cache["messages"]) == 125


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["run", "inbox"])
async def test_history_batch_precheck_and_fetch_share_snapshot(pg_store, pg_dsn,
                                                                monkeypatch, kind):
    """A mutable Kernel JSON row cannot grow after precheck within one read."""
    import json
    from contextlib import asynccontextmanager

    import asyncpg

    from ksadk.sessions.errors import HistoryProjectionBudgetExceeded
    from ksadk.sessions.history_budget import CURRENT_HISTORY_READ_BUDGET, HistoryReadBudget
    from tests.kernel.store_conformance import TENANT

    marker = uuid.uuid4().hex
    message_id = str(uuid.uuid4())
    request = command(f"snapshot-{marker}", "small")
    run_id = f"snapshot-run-{marker}"
    if kind == "run":
        await pg_store._pool.execute(
            "INSERT INTO kernel_runs (run_id,tenant_id,agent_instance_id,session_id,"
            "state,metadata) VALUES ($1,$2,$3,$4,'pending',$5::jsonb)",
            run_id, TENANT, AGENT, SESSION, json.dumps({"note": "before"}),
        )
        identity = run_id
        table, column, key = "kernel_runs", "metadata", "run_id"
    else:
        await pg_store._pool.execute(
            "INSERT INTO kernel_inbox (message_id,tenant_id,agent_instance_id,session_id,"
            "idempotency_key,request_digest,accepted_seq,status,payload) "
            "VALUES ($1::uuid,$2,$3,$4,$5,'digest',1,'accepted',$6::jsonb)",
            message_id, TENANT, AGENT, SESSION, request.idempotency_key,
            request.model_dump_json(),
        )
        identity = message_id
        table, column, key = "kernel_inbox", "payload", "message_id"

    original_connection = pg_store._connection
    updated = False

    @asynccontextmanager
    async def interposed_connection(*args, **kwargs):
        async with original_connection(*args, **kwargs) as connection:
            class Proxy:
                def __getattr__(self, name):
                    return getattr(connection, name)

                async def fetchval(self, *fetch_args, **fetch_kwargs):
                    nonlocal updated
                    value = await connection.fetchval(*fetch_args, **fetch_kwargs)
                    if not updated:
                        updated = True
                        peer = await asyncpg.connect(pg_dsn)
                        try:
                            await peer.execute(
                                f"UPDATE {table} SET {column}=$1::jsonb WHERE {key}=$2"
                                + ("::uuid" if kind == "inbox" else ""),
                                json.dumps({"note": "x" * (17 * 1024 * 1024)}), identity,
                            )
                        finally:
                            await peer.close()
                    return value

            yield Proxy()

    monkeypatch.setattr(pg_store, "_connection", interposed_connection)
    token = CURRENT_HISTORY_READ_BUDGET.set(HistoryReadBudget())
    try:
        kwargs = dict(tenant_id=TENANT, agent_instance_id=AGENT, session_id=SESSION)
        if kind == "run":
            first = await pg_store.load_runs_for_history([run_id], **kwargs)
            assert first[run_id].metadata == {"note": "before"}
        else:
            first = await pg_store.load_messages_for_history([message_id], **kwargs)
            assert first[message_id].command.command_id == request.command_id
        assert updated
        CURRENT_HISTORY_READ_BUDGET.set(HistoryReadBudget())
        with pytest.raises(HistoryProjectionBudgetExceeded):
            if kind == "run":
                await pg_store.load_runs_for_history([run_id], **kwargs)
            else:
                await pg_store.load_messages_for_history([message_id], **kwargs)
    finally:
        CURRENT_HISTORY_READ_BUDGET.reset(token)


@pytest.mark.asyncio
async def test_history_batch_cumulative_inbox_payload_rejected_before_body_fetch(pg_store,
                                                                                 monkeypatch):
    import json

    from ksadk.sessions.errors import HistoryProjectionBudgetExceeded
    from ksadk.sessions.history_budget import CURRENT_HISTORY_READ_BUDGET, HistoryReadBudget
    from tests.kernel.store_conformance import TENANT

    marker = uuid.uuid4().hex
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    rows = []
    for index, message_id in enumerate(ids):
        payload = json.loads(command(f"large-{marker}-{index}", "small").model_dump_json())
        payload["padding"] = "x" * (9 * 1024 * 1024)
        rows.append((message_id, TENANT, AGENT, SESSION, f"large-{marker}-{index}",
                     "digest", index + 1, "accepted", json.dumps(payload)))
    await pg_store._pool.executemany(
        "INSERT INTO kernel_inbox (message_id,tenant_id,agent_instance_id,session_id,"
        "idempotency_key,request_digest,accepted_seq,status,payload) "
        "VALUES ($1::uuid,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)", rows,
    )
    def must_not_decode(_row):
        raise AssertionError("oversize Kernel body was fetched before precheck")
    monkeypatch.setattr(pg_store, "_row_to_message", must_not_decode)
    token = CURRENT_HISTORY_READ_BUDGET.set(HistoryReadBudget())
    try:
        with pytest.raises(HistoryProjectionBudgetExceeded):
            await pg_store.load_messages_for_history(
                ids, tenant_id=TENANT, agent_instance_id=AGENT, session_id=SESSION,
            )
    finally:
        CURRENT_HISTORY_READ_BUDGET.reset(token)


async def test_postgres_nonce_store_is_durable_across_instances(pg_store):
    """P0-4: nonce 去重跨 store 实例（等价跨 Pod / 重启）仍然生效。"""

    from ksadk.kernel.postgres_store import PostgresNonceStore
    pool = pg_store._pool
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM kernel_permit_nonces")
    store_a = PostgresNonceStore(pool)
    store_b = PostgresNonceStore(pool)
    # 首次注册成功。
    assert await store_a.register("nonce-durable-1", "cmd-1", "idem-1") is True
    # 同 identity 的网络重试可通过（另一实例）。
    assert await store_b.register("nonce-durable-1", "cmd-1", "idem-1") is True
    # 同 nonce 被其它 command 复用（重放）必须拒绝。
    assert await store_b.register("nonce-durable-1", "cmd-2", "idem-2") is False
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM kernel_permit_nonces")


# ------------------------------------------------------- Task 4 Step 5 tests
# 事务级 RuntimeEvent fencing：typed RuntimeEventStore（family=runtime/v2）
# 写入必须与 activation 行验证发生在同一个 PG 事务。被 takeover 的旧 owner
# 经 fenced SessionEventStore append 一律 StaleFenceError 且不落事件。


async def _fenced_events(pg_store):
    from ksadk.kernel.postgres_store import PostgresFencedSessionEventStore

    return PostgresFencedSessionEventStore(pg_store)


async def test_fenced_typed_event_confirms_lost_commit_by_id_once(pg_store, monkeypatch):
    from ksadk.events.canonical import RunProgress, SourceRef
    from ksadk.events.canonical_store import RuntimeEventStore
    from ksadk.kernel.contracts import ActivationWriteGuard

    events = await _fenced_events(pg_store)
    lease = await pg_store.acquire_activation(lease_request("lost-reply-owner"))
    guard = ActivationWriteGuard(
        activation_id=lease.activation_id, fencing_token=lease.fencing_token,
    )
    fact = RunProgress(
        schema_version=2, event_id="evt_lost_reply", seq=0,
        timestamp=time.time(), run_id="run-lost-reply", scope_id="scope-lost-reply",
        status="running", progress=.5, message="committed once",
        source=SourceRef(framework="ksadk"),
    )
    view = RuntimeEventStore(events, session_id=SESSION)
    original_append = events.append
    calls = 0

    async def committed_then_lost(*args, **kwargs):
        nonlocal calls
        calls += 1
        await original_append(*args, **kwargs)
        raise TimeoutError("injected lost commit acknowledgement")

    monkeypatch.setattr(events, "append", committed_then_lost)
    confirmed = await view.append(fact, guard=guard)
    assert confirmed.event_id == fact.event_id
    assert calls == 1
    assert len([row for row in await events.read(SESSION, 0, 100)
                if row.payload.get("event_id") == fact.event_id]) == 1
    with pytest.raises(ValueError, match="collision"):
        await view.append(fact.model_copy(update={"message": "other fact"}), guard=guard)
    await expire_lease(pg_store, lease)
    await pg_store.acquire_activation(lease_request("next-owner"))
    with pytest.raises(StaleFenceError):
        await view.append(fact, guard=guard)
    assert calls == 3


async def test_fenced_runtime_event_store_rejects_stale_owner(pg_store):
    events = await _fenced_events(pg_store)
    old = await pg_store.acquire_activation(lease_request("pod-stale-owner"))
    await expire_lease(pg_store, old)
    new = await pg_store.acquire_activation(lease_request("pod-new-owner"))

    from ksadk.events.canonical_store import RuntimeEventStore
    from ksadk.kernel.contracts import ActivationWriteGuard

    runtime_store = RuntimeEventStore(events, session_id=SESSION)
    stale_guard = ActivationWriteGuard(
        activation_id=old.activation_id, fencing_token=old.fencing_token
    )
    live_guard = ActivationWriteGuard(
        activation_id=new.activation_id, fencing_token=new.fencing_token
    )
    from ksadk.events.canonical import RunProgress, SourceRef

    def progress(event_id: str) -> RunProgress:
        return RunProgress(
            schema_version=2,
            event_id=event_id,
            seq=0,
            timestamp=time.time(),
            run_id="run-fence-1",
            scope_id="run:run-fence-1",
            status="running",
            progress=0.5,
            message="stale owner write",
            source=SourceRef(framework="ksadk"),
        )

    with pytest.raises(StaleFenceError):
        await runtime_store.append(progress("evt_stale_1"), guard=stale_guard)
    # 旧 owner 的事件没有落库。
    persisted = await events.read(SESSION, 0, 1000)
    assert all(e.event_id != "evt_stale_1" for e in persisted), persisted
    # 新 owner（当前 unexpired activation）正常写入。
    written = await runtime_store.append(progress("evt_live_1"), guard=live_guard)
    assert written.seq >= 1


async def test_fenced_runtime_event_store_rejects_released_activation(pg_store):
    events = await _fenced_events(pg_store)
    lease = await pg_store.acquire_activation(lease_request("pod-release"))
    await pg_store.release_activation(
        lease.activation_id, expected_fence=lease.fencing_token
    )
    from ksadk.events.canonical_store import RuntimeEventStore
    from ksadk.kernel.contracts import ActivationWriteGuard

    runtime_store = RuntimeEventStore(events, session_id=SESSION)
    guard = ActivationWriteGuard(
        activation_id=lease.activation_id, fencing_token=lease.fencing_token
    )
    from ksadk.events.canonical import RunProgress, SourceRef

    event = RunProgress(
        schema_version=2,
        event_id="evt_released_1",
        seq=0,
        timestamp=time.time(),
        run_id="run-fence-2",
        scope_id="run:run-fence-2",
        status="running",
        progress=1.0,
        message="released owner write",
        source=SourceRef(framework="ksadk"),
    )
    with pytest.raises(StaleFenceError):
        await runtime_store.append(event, guard=guard)


async def test_fenced_runtime_event_store_rejects_foreign_activation_id(pg_store):
    """guard 的 activation_id 与 lease 行不符（同 token 也不行）时拒绝。"""
    events = await _fenced_events(pg_store)
    lease = await pg_store.acquire_activation(lease_request("pod-foreign"))
    from ksadk.events.canonical_store import RuntimeEventStore
    from ksadk.kernel.contracts import ActivationWriteGuard

    runtime_store = RuntimeEventStore(events, session_id=SESSION)
    guard = ActivationWriteGuard(
        activation_id="not-the-held-activation", fencing_token=lease.fencing_token
    )
    from ksadk.events.canonical import RunProgress, SourceRef

    event = RunProgress(
        schema_version=2,
        event_id="evt_foreign_1",
        seq=0,
        timestamp=time.time(),
        run_id="run-fence-3",
        scope_id="run:run-fence-3",
        status="running",
        progress=1.0,
        message="foreign activation write",
        source=SourceRef(framework="ksadk"),
    )
    with pytest.raises(StaleFenceError):
        await runtime_store.append(event, guard=guard)


@pytest.mark.parametrize("skew_seconds", [3600, -3600])
async def test_lease_authority_uses_database_clock(pg_store, monkeypatch, skew_seconds):
    """Pod clock skew must neither expire live DB leases nor revive expired ones."""
    from datetime import timedelta

    from ksadk.kernel import postgres_store
    from ksadk.kernel.contracts import ActivationWriteGuard
    from ksadk.kernel.postgres_store import PostgresFencedSessionEventStore

    lease = await pg_store.acquire_activation(lease_request("clock-owner"))
    guard = ActivationWriteGuard(
        activation_id=lease.activation_id, fencing_token=lease.fencing_token
    )
    events = PostgresFencedSessionEventStore(pg_store)
    if skew_seconds < 0:
        await expire_lease(pg_store, lease)
    monkeypatch.setattr(
        postgres_store, "_now", lambda: datetime.now(UTC) + timedelta(seconds=skew_seconds)
    )

    async def append_control():
        await pg_store.append_event(runtime_envelope(), expected_fence=lease.fencing_token)

    async def append_runtime():
        await events.append(runtime_envelope(), guard=guard)

    async def renew():
        await pg_store.renew_activation(lease.activation_id, expected_fence=lease.fencing_token,
                                       lease_ttl_seconds=60)

    async def interaction_guard():
        async with pg_store._connection() as connection:
            async with connection.transaction():
                await pg_store._assert_interaction_guard(connection, AGENT, SESSION, guard)
                await pg_store._interaction_row_for_guard(connection, "missing-interaction", guard)

    for operation in (append_control, append_runtime, interaction_guard, renew):
        if skew_seconds > 0:
            await operation()
        else:
            with pytest.raises(StaleFenceError):
                await operation()


async def test_live_process_owner_excludes_competitor(pg_store, pg_dsn):
    import json

    env = {**os.environ, "KSADK_TEST_POSTGRES_DSN": pg_dsn,
           "WORKER_AGENT": AGENT, "WORKER_SESSION": SESSION}
    owner = await asyncio.create_subprocess_exec(
        sys.executable, "-c", WORKER_SOURCE, "acquire", "live-owner", "30",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )
    try:
        line = await asyncio.wait_for(owner.stdout.readline(), timeout=10)
        token = json.loads(line)["fencing_token"]
        denied, code, stderr = await _run_worker(
            pg_dsn, "acquire", "live-competitor", "30", kill_after=10,
            extra_env={"WORKER_AGENT": AGENT, "WORKER_SESSION": SESSION},
        )
        assert code == 0, stderr
        assert json.loads(denied) == {"lease_denied": True}
        assert owner.returncode is None
        # The denied competitor must not displace the live owner or its fence.
        renewed = await pg_store.acquire_activation(lease_request("live-owner", ttl=30))
        assert renewed.fencing_token == token
    finally:
        if owner.returncode is None:
            owner.kill()
        await owner.communicate()
