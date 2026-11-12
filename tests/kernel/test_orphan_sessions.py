"""Session deletion and recovery must leave unrelated Kernel work runnable."""

import asyncio
import os
from contextlib import AsyncExitStack
from dataclasses import replace
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import uuid4

import pytest

from ksadk.events.session_event import SessionServiceEventStore
from ksadk.kernel.contracts import ActivationWriteGuard
from ksadk.kernel.errors import StaleFenceError
from ksadk.kernel.memory_store import InMemoryAgentKernelStore
from ksadk.kernel.postgres_store import PostgresAgentKernelStore, PostgresFencedSessionEventStore, PostgresKernelEventLog
from ksadk.kernel.state import InboxState, RunState
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.sessions.local_service import LocalSessionService
from ksadk.sessions.postgres_service import PostgresSessionService
from tests.interaction.test_ledger_conformance import make_record
from tests.kernel.control_harness import AGENT, command
from tests.kernel.store_conformance import lease_request, make_run, seed_session
from tests.kernel.test_session_quarantine import _build
from tests.postgres_isolation import isolated_postgres_dsn


@pytest.fixture
async def pg_dsn():
    dsn = os.getenv("KSADK_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("real PostgreSQL required")
    async with isolated_postgres_dsn(dsn) as scoped_dsn:
        yield scoped_dsn


@pytest.fixture
async def backend_pg_dsn(backend):
    # Memory cases remain runnable without PostgreSQL. Resolve the async DB
    # fixture before the test loop starts, rather than via getfixturevalue().
    if backend != "postgres":
        yield None
        return
    dsn = os.getenv("KSADK_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("real PostgreSQL required")
    async with isolated_postgres_dsn(dsn) as scoped_dsn:
        yield scoped_dsn


async def postgres_kernel(dsn, namespace, tenant):
    service = PostgresSessionService(dsn=dsn, namespace=namespace, tenant_id=tenant)
    await service._ensure_schema()
    log = PostgresKernelEventLog(service._pool, namespace=namespace, tenant_id=tenant)
    store = PostgresAgentKernelStore(service._pool, log, tenant_id=tenant)
    await store.ensure_schema()
    return service, store, PostgresFencedSessionEventStore(store)


async def test_postgres_fixture_does_not_share_default_schema(pg_dsn):
    import asyncpg

    connection = await asyncpg.connect(pg_dsn)
    try:
        schema = await connection.fetchval("SELECT current_schema()")
        assert schema != "public", "parallel Kernel fixtures must not share reset_for_tests tables"
        assert await connection.fetchval("SELECT current_schemas(false)") == [schema]
    finally:
        await connection.close()


@pytest.fixture
async def legacy_schema(pg_dsn):
    import asyncpg

    schema = "retirement_" + uuid4().hex
    url = urlsplit(pg_dsn)
    params = dict(parse_qsl(url.query), search_path=schema)
    dsn = url._replace(query=urlencode(params)).geturl()
    async with AsyncExitStack() as cleanup:
        admin = await asyncpg.connect(pg_dsn)
        cleanup.push_async_callback(admin.close)
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        cleanup.push_async_callback(admin.execute, f'DROP SCHEMA "{schema}" CASCADE')
        yield dsn, schema


async def test_postgres_upgrade_and_cold_restart_leave_ddl_untouched(legacy_schema):
    from ksadk.kernel.postgres_store import SCHEMA_PATH

    dsn, schema = legacy_schema
    async with AsyncExitStack() as cleanup:
        old = PostgresSessionService(dsn=dsn, namespace=schema, tenant_id=schema)
        cleanup.push_async_callback(old.aclose)
        await old._ensure_schema()
        await old._pool.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        await old._pool.execute("""
            INSERT INTO kernel_runs(run_id, tenant_id, agent_instance_id, session_id, state)
            VALUES ('old-run', $1, $2, 'deleted-session', 'waiting')
        """, schema, AGENT)
        await old.aclose()

        first, _, _ = await postgres_kernel(dsn, schema, schema)
        cleanup.push_async_callback(first.aclose)
        # CREATE OR REPLACE changes the catalog row's xmin even if its text is identical.
        definition = """SELECT xmin::text FROM pg_proc
                        WHERE oid=to_regprocedure('ksadk_after_kernel_session_delete()')"""
        installed = await first._pool.fetchval(definition)
        assert installed
        assert await first._pool.fetchval("""
            SELECT EXISTS (SELECT FROM pg_trigger WHERE tgrelid='ksadk_sessions'::regclass
                           AND tgname='ksadk_retire_deleted_session')
        """)
        await first.aclose()

        restarted, store, _ = await postgres_kernel(dsn, schema, schema)
        cleanup.push_async_callback(restarted.aclose)
        assert await restarted._pool.fetchval(definition) == installed
        await store.acquire_activation(lease_request("new-owner", session_id="deleted-session"))
        run = await store.load_run("old-run")
        assert run.state is RunState.INTERRUPTED
        assert run.metadata["interruption_reason"] == "session_missing"


async def seed_open(service, store, sid, state=RunState.WAITING, *, agent=AGENT):
    await service.create_session(agent_id=agent, user_id="test", session_id=sid)
    cmd = command(session_id=sid).model_copy(update={"agent_instance_id": agent})
    receipt = await store.accept_command(cmd, queue_limit=10)
    request = replace(lease_request("old-" + agent + sid, session_id=sid), agent_instance_id=agent)
    lease = await store.acquire_activation(request)
    message_id = str(receipt.message_id)
    await store.claim_message(message_id, lease.fencing_token)
    run = make_run(f"run-{agent}-{sid}").model_copy(update={"session_id": sid, "agent_instance_id": agent})
    await store.save_run_transition(run, expected_fence=lease.fencing_token)
    if state != RunState.PENDING:
        await store.save_run_transition(run.model_copy(update={"state": RunState.RUNNING}),
                                        expected_fence=lease.fencing_token)
    if state not in (RunState.PENDING, RunState.RUNNING):
        await store.save_run_transition(run.model_copy(update={"state": state}), expected_fence=lease.fencing_token)
    return lease, message_id, run.run_id


async def completes(stack, runtime, service, store, events, sid):
    await seed_session(service, sid)()
    receipt = await runtime.kernel.submit(command(session_id=sid), permit=stack.permit("enqueue", session_id=sid))
    async with asyncio.timeout(10):
        while (await store.load_message(str(receipt.message_id))).status is not InboxState.COMPLETED:
            await asyncio.sleep(0.02)
        while not any(e.event_type == "run.completed" for e in await events.read(sid, 0, 100)):
            await asyncio.sleep(0.02)
    assert ("start", sid) in stack.adapter.calls


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_delete_retires_only_the_deleted_session_and_fences_old_writer(backend, backend_pg_dsn):
    ns = uuid4().hex
    if backend == "memory":
        service = InMemorySessionService()
        events = SessionServiceEventStore(service)
        store = InMemoryAgentKernelStore(events)
    else:
        service, store, events = await postgres_kernel(backend_pg_dsn, ns, ns)
    try:
        sid = ns + "-deleted"
        old, message, run_id = await seed_open(service, store, sid)
        _, _, waiting = await seed_open(service, store, ns + "-waiting")
        _, _, completed = await seed_open(service, store, ns + "-finished", RunState.COMPLETED)
        guard = ActivationWriteGuard(activation_id=old.activation_id, fencing_token=old.fencing_token)
        approval = make_record(ns, tenant_id=ns, agent_instance_id=AGENT, session_id=sid)
        await store.request(approval.model_copy(update={"run_id": run_id}), guard=guard)
        assert await service.delete_session(sid)
        assert not await service.delete_session(sid)
        assert await service.delete_session(ns + "-finished")
        await seed_session(service, sid)()  # Reuse the public id before another scheduler tick.
        run = await store.load_run(run_id)
        assert run.state is RunState.INTERRUPTED and run.metadata["interruption_reason"] == "session_deleted"
        assert (await store.load_message(message)).status is InboxState.DISCARDED
        assert not await store.list_pending_interactions(ns, sid)
        assert (await store.load_run(waiting)).state is RunState.WAITING
        assert (await store.load_run(completed)).state is RunState.COMPLETED
        with pytest.raises(StaleFenceError):
            await store.save_run_transition(run, expected_fence=old.fencing_token)
        assert not await events.read(sid, 0, 10)
    finally:
        if backend == "postgres":
            await service.aclose()


@pytest.mark.parametrize("backend", ["postgres", "memory"])
async def test_recovery_consumes_new_work_after_many_orphans(backend, backend_pg_dsn, tmp_path):
    ns = uuid4().hex
    if backend == "postgres":
        dsn = backend_pg_dsn
        service, store, events = await postgres_kernel(dsn, ns, ns)
    else:
        # An ephemeral kernel can outlive sessions held by another service.
        service = LocalSessionService(tmp_path / "sessions.sqlite")
        events = SessionServiceEventStore(service)
        store = InMemoryAgentKernelStore(events)
    rows = []
    try:
        for i, state in enumerate((RunState.PENDING, RunState.RUNNING, RunState.PAUSED,
                                   RunState.WAITING, RunState.WAITING, RunState.RUNNING)):
            sid = f"{ns}-old-{i}"
            owner, message, run_id = await seed_open(service, store, sid, state)
            await store.release_activation(owner.activation_id, expected_fence=owner.fencing_token)
            rows.append((sid, message, run_id))
        if backend == "postgres":
            # Recreate the old on-disk shape, bypassing current SDK deletion.
            ids = [sid for sid, _, _ in rows]
            async with service._pool.acquire() as conn, conn.transaction():
                for table in ("kernel_activations", "kernel_inbox", "kernel_runs"):
                    await conn.execute(f"UPDATE {table} SET session_namespace=NULL WHERE session_id=ANY($1)", ids)
                await conn.execute("DELETE FROM kernel_inbox WHERE session_id=$1", ids[-1])
                await conn.execute("ALTER TABLE ksadk_sessions DISABLE TRIGGER ksadk_retire_deleted_session")
                await conn.execute("DELETE FROM ksadk_sessions WHERE namespace=$1", ns)
                await conn.execute("ALTER TABLE ksadk_sessions ENABLE TRIGGER ksadk_retire_deleted_session")
            await service.aclose()
        else:
            for sid, _, _ in rows:
                await service.delete_session(sid)
        for restart in range(2):
            if backend == "postgres":
                service, store, events = await postgres_kernel(dsn, ns, ns)
            stack, runtime = await _build(store=store, session_events=events, session_service=service)
            try:
                await runtime.start()
                await completes(stack, runtime, service, store, events, f"{ns}-new-{restart}")
                async with asyncio.timeout(10):
                    while set(sid for sid, _, _ in rows) & await store.pending_session_ids(AGENT):
                        await asyncio.sleep(0.02)
                assert runtime.worker_running and not runtime.degraded and not runtime.quarantined_sessions()
                for sid, message, run_id in rows:
                    run = await store.load_run(run_id)
                    assert run.state is RunState.INTERRUPTED and run.metadata["interruption_reason"] == "session_missing"
                    if item := await store.load_message(message):
                        assert item.status is InboxState.DISCARDED
                    assert await service.get_session_metadata(sid) is None
                    assert not await events.read(sid, 0, 100)
            finally:
                await runtime.close()
                if backend == "postgres":
                    await service.aclose()
    finally:
        if backend == "postgres":
            await service.aclose()


async def test_postgres_same_session_id_in_other_namespace_is_preserved(pg_dsn):
    tenant, sid = uuid4().hex, uuid4().hex
    left, left_store, _ = await postgres_kernel(pg_dsn, tenant + "-left", tenant)
    right, right_store, _ = await postgres_kernel(pg_dsn, tenant + "-right", tenant)
    try:
        owner, _, left_run = await seed_open(left, left_store, sid)
        other, other_message, right_run = await seed_open(right, right_store, sid, agent="peer-"+AGENT)
        # An old database has no namespace on these rows. Recover ownership
        # from the actual admitted message and run events in each namespace.
        async with left._pool.acquire() as connection, connection.transaction():
            for table in ("kernel_inbox", "kernel_runs", "kernel_activations"):
                await connection.execute(f"UPDATE {table} SET session_namespace=NULL WHERE session_id=$1", sid)
        await left_store.ensure_schema()
        await left.delete_session(sid)
        assert (await left_store.load_run(left_run)).state is RunState.INTERRUPTED
        assert (await right_store.load_run(right_run)).state is RunState.WAITING
        assert (await right_store.load_message(other_message)).status is InboxState.CLAIMED
        assert (await right_store.current_lease("peer-"+AGENT, sid)).fencing_token == other.fencing_token
        assert await right.get_session_metadata(sid)
        with pytest.raises(StaleFenceError):
            await left_store.renew_activation(owner.activation_id, expected_fence=owner.fencing_token, lease_ttl_seconds=60)
    finally:
        await left.aclose()
        await right.aclose()


async def test_waiting_sessions_cannot_monopolize_all_scheduler_slots(monkeypatch):
    monkeypatch.setattr("ksadk.kernel.bootstrap.MAX_CONCURRENT_KERNEL_SESSIONS", 2)
    stack, runtime = await _build()
    for sid in ("a-held", "b-held", "c-held"):
        await seed_open(stack.session_service, stack.store, sid)
    try:
        await runtime.start()
        await completes(stack, runtime, stack.session_service, stack.store, stack.events, "z-new")
        assert runtime.worker_running and not runtime.degraded
    finally:
        await runtime.close()


async def test_external_session_absence_isolated_without_erasing_unverified_sqlite_work(tmp_path):
    from ksadk.kernel.sqlite_store import SQLiteAgentKernelStore

    service = InMemorySessionService()
    events = SessionServiceEventStore(service)
    store = SQLiteAgentKernelStore(tmp_path / "kernel.sqlite", events)
    await store.ensure_schema()
    stack, runtime = await _build(store=store, session_events=events, session_service=service)
    try:
        missing = set()
        for i in range(5):
            sid = f"external-{i}"
            owner, _, _ = await seed_open(service, store, sid)
            await store.release_activation(owner.activation_id, expected_fence=owner.fencing_token)
            await service.delete_session(sid)
            missing.add(sid)
        await runtime.start()
        await completes(stack, runtime, service, store, events, "external-healthy")
        async with asyncio.timeout(5):
            while runtime.quarantined_sessions() != missing:
                await asyncio.sleep(0.02)
        assert runtime.worker_running and not runtime.degraded
        for sid in missing:
            assert (await store.load_run(f"run-{AGENT}-{sid}")).state is RunState.WAITING
    finally:
        await runtime.close()
        await store.close()


async def test_postgres_raw_delete_and_rollback_preserve_scoped_waits(pg_dsn):
    from ksadk.mcp_runtime.oauth.kernel import SQL

    ns = uuid4().hex
    service, store, _ = await postgres_kernel(pg_dsn, ns, ns)
    sid = ns + "-wait"
    try:
        lease, message, run_id = await seed_open(service, store, sid)
        await service._pool.execute(SQL)
        await service._pool.execute(
            """INSERT INTO mcp_authorization_waits
               (wait_id,instance_id,session_id,run_id,interaction_id,native_target,snapshot,status)
               VALUES($1,$2,$3,$4,$1,'{}','{}','consumed')""",
            ns, AGENT, sid, run_id,
        )
        async with service._pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            await conn.execute("DELETE FROM ksadk_sessions WHERE namespace=$1 AND id=$2", ns, sid)
            assert await conn.fetchval("SELECT status FROM mcp_authorization_waits WHERE wait_id=$1", ns) == "cancelled"
            await tx.rollback()
        assert (await store.load_run(run_id)).state is RunState.WAITING
        assert (await store.load_message(message)).status is InboxState.CLAIMED
        assert (await store.current_lease(AGENT, sid)).fencing_token == lease.fencing_token
        assert await service._pool.fetchval("SELECT status FROM mcp_authorization_waits WHERE wait_id=$1", ns) == "consumed"
        assert await service.delete_session(sid)
        assert await service._pool.fetchval("SELECT status FROM mcp_authorization_waits WHERE wait_id=$1", ns) == "cancelled"
    finally:
        await service.aclose()


async def test_previously_quarantined_but_deleted_sessions_do_not_count_toward_degradation():
    stack, runtime = await _build(sessions=("first", "second"), quarantine_degrade_threshold=2)
    try:
        failure = RuntimeError("unrecoverable history for this session")
        assert await runtime._quarantine_session("first", failure)
        assert await stack.session_service.delete_session("first")
        assert await runtime._quarantine_session("second", failure)
        assert runtime.quarantined_sessions() == {"second"} and not runtime.degraded
    finally:
        await runtime.close()


@pytest.mark.parametrize("split", [False, True])
async def test_sqlite_consumed_wait_no_longer_schedules_deleted_session(tmp_path, split):
    from ksadk.kernel.sqlite_store import SQLiteAgentKernelStore
    from ksadk.mcp_runtime.oauth.local.runtime import LocalAuthorizationExtension

    service = LocalSessionService(tmp_path / "sessions.sqlite")
    store = SQLiteAgentKernelStore(
        tmp_path / "kernel.sqlite" if split else service.db_path, SessionServiceEventStore(service)
    )
    await store.ensure_schema()
    ext = LocalAuthorizationExtension(store, None)
    try:
        await ext.ensure_schema()
        owner, _, run = await seed_open(service, store, "gone")
        async with ext.pool.acquire() as connection:
            await connection.execute(
                """INSERT INTO mcp_authorization_waits
                   (wait_id,instance_id,session_id,run_id,interaction_id,native_target,snapshot,status)
                   VALUES('wait',$1,'gone',$2,'interaction','{}','{}','consumed')""", AGENT, run,
            )
        assert await ext.pending_session_ids(AGENT) == {"gone"}
        await service.delete_session("gone")
        if split:
            await store.acquire_activation(lease_request(owner.activation_id, session_id="gone"))
        assert not await ext.pending_session_ids(AGENT)
        assert (await ext.row("wait"))["status"] == "cancelled"
    finally:
        await store.close()


async def test_sqlite_kernel_uses_its_remote_postgres_authority(pg_dsn, tmp_path):
    import sqlite3
    from ksadk.kernel.sqlite_store import SQLiteAgentKernelStore

    ns = uuid4().hex
    service = PostgresSessionService(dsn=pg_dsn, namespace=ns)
    events = SessionServiceEventStore(service)
    store = SQLiteAgentKernelStore(tmp_path / "studio.sqlite", events)
    await store.ensure_schema()
    stack, runtime = await _build(store=store, session_events=events, session_service=service)
    try:
        owner, _, run = await seed_open(service, store, "old")
        await store.release_activation(owner.activation_id, expected_fence=owner.fencing_token)
        with sqlite3.connect(store.db_path) as db:
            db.execute("DELETE FROM kernel_inbox WHERE session_id='old'")
        assert await service.delete_session("old")
        assert "old" in await store.pending_session_ids(AGENT)
        await runtime.start()
        await completes(stack, runtime, service, store, events, "healthy")
        async with asyncio.timeout(5):
            while (await store.load_run(run)).state is not RunState.INTERRUPTED:
                await asyncio.sleep(.02)
        assert runtime.worker_running and not runtime.quarantined_sessions()
    finally:
        await runtime.close()
        await store.close()
        await service.aclose()


async def test_postgres_only_consumes_inbox_in_claiming_namespace(pg_dsn):
    tenant, sid = uuid4().hex, uuid4().hex
    left, a, _ = await postgres_kernel(pg_dsn, tenant + "-a", tenant)
    right, b, _ = await postgres_kernel(pg_dsn, tenant + "-b", tenant)
    try:
        await seed_open(left, a, sid)
        await seed_session(right, sid)()
        queued = await b.accept_command(command(session_id=sid), queue_limit=10)
        assert not await a.list_pending(AGENT, sid)
        assert [m.message_id for m in await b.list_pending(AGENT, sid)] == [str(queued.message_id)]
        assert await left.delete_session(sid)
        lease = await b.acquire_activation(lease_request("replacement", session_id=sid))
        claimed = await b.claim_next(AGENT, sid, lease.fencing_token)
        assert claimed.message_id == str(queued.message_id)
    finally:
        await left.aclose()
        await right.aclose()


@pytest.mark.parametrize("operation", ["admit", "transition"])
async def test_memory_delete_serializes_with_inflight_event_commit(monkeypatch, operation):
    service = InMemorySessionService()
    events = SessionServiceEventStore(service)
    store = InMemoryAgentKernelStore(events)
    sid = "reused"
    if operation == "transition":
        lease, _, run_id = await seed_open(service, store, sid, RunState.RUNNING)
        run = await store.load_run(run_id)
        write = store.save_run_transition(
            run.model_copy(update={"state": RunState.WAITING}), expected_fence=lease.fencing_token
        )
    else:
        await seed_session(service, sid)()
        write = store.accept_command(command(session_id=sid), queue_limit=10)

    appended, resume = asyncio.Event(), asyncio.Event()
    append = events.append

    async def pause_after_append(*args, **kwargs):
        result = await append(*args, **kwargs)
        appended.set()
        await resume.wait()
        return result

    monkeypatch.setattr(events, "append", pause_after_append)
    writer = asyncio.create_task(write)
    async with asyncio.timeout(5):
        await appended.wait()
        deletion = asyncio.create_task(service.delete_session(sid))
        await asyncio.sleep(0)  # Let delete reach its lock while the writer is suspended.
        resume.set()
        result = await writer
        assert await deletion
        await seed_session(service, sid)()
        assert not await events.read(sid, 0, 10)
        if operation == "transition":
            assert (await store.load_run(run_id)).state is RunState.INTERRUPTED
        else:
            assert (await store.load_message(str(result.message_id))).status is InboxState.DISCARDED


async def test_postgres_current_scope_restart_never_probes_kernel_history(pg_dsn):
    scope = uuid4().hex
    service, store, _ = await postgres_kernel(pg_dsn, scope, scope)
    try:
        # A table lock detects even a probe of an empty relation. Readiness
        # with no legacy work must depend only on catalog and migration state.
        async with service._pool.acquire() as conn, conn.transaction():
            await conn.execute("""
                LOCK TABLE kernel_runs, kernel_inbox, kernel_interactions, ksadk_events
                IN ACCESS EXCLUSIVE MODE
            """)
            for _ in range(2):
                await asyncio.wait_for(store.ensure_schema(), timeout=2)
    finally:
        await service.aclose()


async def test_postgres_rolling_legacy_repair_releases_schema_locks(pg_dsn, monkeypatch):
    monkeypatch.setenv("KSADK_DB_LOCK_SECONDS", "8")
    scope, sid = uuid4().hex, uuid4().hex
    service, store, _ = await postgres_kernel(pg_dsn, scope, scope)
    try:
        _, _, run_id = await seed_open(service, store, sid)
        await service._pool.execute("UPDATE kernel_runs SET session_namespace=NULL WHERE run_id=$1", run_id)
        async with asyncio.timeout(8), asyncio.TaskGroup() as tasks:
            async with service._pool.acquire() as held, held.transaction():
                await held.execute("SELECT FROM kernel_runs WHERE run_id=$1 FOR UPDATE", run_id)
                pid = await held.fetchval("SELECT pg_backend_pid()")
                tasks.create_task(store.ensure_schema())
                async with asyncio.timeout(3):
                    while not await held.fetchval("""
                        SELECT EXISTS(SELECT FROM pg_stat_activity a WHERE $1=ANY(pg_blocking_pids(a.pid)))
                    """, pid):
                        await held.execute("SELECT pg_stat_clear_snapshot()")
                        await asyncio.sleep(0.01)
                # An unrelated Pod can initialize while a legacy data row is held.
                other, _, _ = await asyncio.wait_for(postgres_kernel(pg_dsn, scope + "-other", scope + "-other"), 2)
                await other.aclose()
        assert await service._pool.fetchval("SELECT session_namespace FROM kernel_runs WHERE run_id=$1", run_id) == scope
    finally:
        await service.aclose()


async def test_postgres_resumes_interrupted_legacy_upgrade(legacy_schema, monkeypatch):
    from ksadk.kernel import legacy_postgres_repair
    from ksadk.kernel.postgres_store import SCHEMA_PATH

    dsn, ns = legacy_schema
    service = PostgresSessionService(dsn=dsn, namespace=ns, tenant_id=ns)
    try:
        await service._ensure_schema()
        pool = service._pool
        # Previous wheel, before the work queue or its triggers existed.
        await pool.execute(SCHEMA_PATH.read_text())
        await pool.execute(SCHEMA_PATH.with_name("002_session_retirement.sql").read_text())
        store = PostgresAgentKernelStore(pool, PostgresKernelEventLog(pool, namespace=ns, tenant_id=ns),
                                        tenant_id=ns)
        receipts = []
        for sid in ("legacy-a", "legacy-b", "legacy-c"):
            lease, mid, run = await seed_open(service, store, sid)
            receipt = await store.load_message(mid)
            receipts.append((mid, receipt.accepted_seq))
            record = make_record(sid, tenant_id=ns, agent_instance_id=AGENT, session_id=sid)
            guard = ActivationWriteGuard(activation_id=lease.activation_id, fencing_token=lease.fencing_token)
            await store.request(record.model_copy(update={"run_id": run}), guard=guard)
        for table in ("kernel_runs", "kernel_interactions", "kernel_activations"):
            await pool.execute(f"UPDATE {table} SET session_namespace=NULL")
        await pool.execute("UPDATE kernel_inbox SET session_namespace=NULL, accepted_seq=0")

        monkeypatch.setattr(legacy_postgres_repair, "_SEED_PAGE", 1)
        original = legacy_postgres_repair._seed_legacy_page
        calls = 0

        async def interrupt_second_page(conn):
            nonlocal calls
            calls += 1
            result = await original(conn)
            if calls == 2:
                raise RuntimeError("simulated restart before commit")
            return result

        with monkeypatch.context() as patch:
            patch.setattr(legacy_postgres_repair, "_seed_legacy_page", interrupt_second_page)
            with pytest.raises(RuntimeError, match="simulated restart"):
                await store.ensure_schema()
        assert await pool.fetchval("SELECT count(*) FROM kernel_legacy_session_repairs") == 1

        await asyncio.gather(store.ensure_schema(), store.ensure_schema())
        assert not await pool.fetchval("SELECT EXISTS (SELECT FROM kernel_legacy_repair_progress)")
        assert not await pool.fetchval("SELECT EXISTS (SELECT FROM kernel_legacy_session_repairs)")
        for table in ("kernel_runs", "kernel_interactions", "kernel_inbox", "kernel_activations"):
            assert await pool.fetchval(f"SELECT count(*) FROM {table} WHERE session_namespace=$1", ns) == 3
        for mid, seq in receipts:
            assert (await store.load_message(mid)).accepted_seq == seq
    finally:
        await service.aclose()


async def test_postgres_retries_unproven_legacy_work_when_its_event_arrives(pg_dsn):
    scope, sid = uuid4().hex, uuid4().hex
    service, store, _ = await postgres_kernel(pg_dsn, scope, scope)
    try:
        _, _, run = await seed_open(service, store, sid)
        pool = service._pool
        await pool.execute("""
            UPDATE ksadk_events SET event_type='test.withheld'
            WHERE namespace=$1 AND session_id=$2 AND invocation_id=$3 AND event_type='control.run_transition'
        """, scope, sid, run)
        await pool.execute("UPDATE kernel_runs SET session_namespace=NULL WHERE run_id=$1", run)
        await store.ensure_schema()
        namespace = "SELECT session_namespace FROM kernel_runs WHERE run_id=$1"
        assert await pool.fetchval(namespace, run) is None
        await pool.execute("""
            UPDATE ksadk_events SET event_type='control.run_transition'
            WHERE namespace=$1 AND session_id=$2 AND invocation_id=$3 AND event_type='test.withheld'
        """, scope, sid, run)
        await store.ensure_schema()
        assert await pool.fetchval(namespace, run) == scope
    finally:
        await service.aclose()
