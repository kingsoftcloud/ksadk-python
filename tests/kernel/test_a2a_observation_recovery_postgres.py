# ruff: noqa: F811
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ksadk.a2a.bootstrap import _ManagedKernelEvents
from ksadk.a2a.inbound_kernel import InboundKernelSubmission, stream_inbound_kernel_facts
from ksadk.kernel.contracts import ActivationWriteGuard, AgentControlReceipt, SessionEventEnvelope
from ksadk.kernel.postgres_store import PostgresFencedSessionEventStore
from tests.kernel.store_conformance import SESSION, lease_request
from tests.kernel.test_postgres_store import pg_dsn, pg_store  # noqa: F401
from tests.kernel.test_postgres_store import kernel_postgres_server_dsn  # noqa: F401


@pytest.mark.asyncio
async def test_admitted_a2a_observation_resumes_same_cursor_after_real_sql_timeout(
    pg_store, monkeypatch,
):
    from ksadk.kernel import postgres_store as module

    lease = await pg_store.acquire_activation(lease_request("observer"))
    guard = ActivationWriteGuard(activation_id=lease.activation_id,
                                 fencing_token=lease.fencing_token)
    store = PostgresFencedSessionEventStore(pg_store)
    command = uuid4()
    for state in ["pending", "completed"]:
        await store.append(SessionEventEnvelope(
            event_id=uuid4(), session_id=SESSION, seq=0, timestamp="2026-10-18T00:00:00Z",
            family="control", family_version=1, event_type="control.run_transition",
            run_id="ours", causation_id=str(command),
            payload={"run_id": "ours", "state": state},
        ), guard=guard)
    budget = replace(module.session_db_budget(), read_sql_seconds=0.03,
                     event_observe_recovery_seconds=2)
    monkeypatch.setattr(module, "session_db_budget", lambda: budget)
    original = store._log.read_subscription_page
    cursors = []

    async def flaky(session, cursor):
        cursors.append(cursor)
        if len(cursors) == 2:
            async with store._log._connection() as connection:
                await connection.fetchval("SELECT pg_sleep(0.15)")
        return (await original(session, cursor))[:1] if len(cursors) == 1 else await original(
            session, cursor,
        )

    monkeypatch.setattr(store._log, "read_subscription_page", flaky)
    from ksadk.kernel import bootstrap

    monkeypatch.setattr(bootstrap, "get_agent_kernel_runtime",
                        lambda: SimpleNamespace(session_events=store))
    receipt = AgentControlReceipt(command_id=command, status="accepted", message_id=uuid4(),
                                  accepted_seq=20)
    submission = InboundKernelSubmission(SESSION, "ar-target", receipt)
    rows = [fact async for fact in stream_inbound_kernel_facts(submission, _ManagedKernelEvents())]
    assert [row.payload["state"] for row in rows] == ["pending", "completed"]
    assert len({row.event_id for row in rows}) == 2
    assert cursors == [0, rows[0].seq, rows[0].seq]
    assert len(await store.read(SESSION, 0, 10)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError(), PermissionError("denied")])
async def test_observation_recovery_has_one_outage_deadline_and_never_retries_denial(
    pg_store, monkeypatch, error,
):
    from ksadk.kernel import postgres_store as module

    budget = replace(module.session_db_budget(), event_observe_recovery_seconds=0.04)
    monkeypatch.setattr(module, "session_db_budget", lambda: budget)
    store = PostgresFencedSessionEventStore(pg_store)
    calls = []

    async def broken(*args):
        calls.append(1)
        raise error

    monkeypatch.setattr(store._log, "read_subscription_page", broken)
    started = asyncio.get_running_loop().time()
    with pytest.raises(type(error)):
        await anext(store.subscribe_recovering(SESSION, 0))
    assert asyncio.get_running_loop().time() - started < 0.5
    assert len(calls) == (1 if isinstance(error, PermissionError) else 2)


@pytest.mark.asyncio
async def test_unknown_target_guard_survives_new_authority_but_allows_new_user_run(pg_store):
    from ksadk.a2a.tool_outcomes import A2ASendOutcomeUnknown, assert_target_outcome_known
    from ksadk.events.canonical_store import RuntimeEventStore
    from ksadk.events.run_authority import RunEventAuthority, bind_run_event_authority
    from tests.a2a.test_tool_outcome_guard import facts

    lease = await pg_store.acquire_activation(lease_request("guard"))
    guard = ActivationWriteGuard(activation_id=lease.activation_id,
                                 fencing_token=lease.fencing_token)
    store = PostgresFencedSessionEventStore(pg_store)
    events = RuntimeEventStore(store, session_id=SESSION)
    for event in facts():
        await events.append(event, guard=guard)
    for _ in range(2):
        authority = RunEventAuthority(store, session_id=SESSION, run_id="current", guard=guard)
        with bind_run_event_authority(authority), pytest.raises(A2ASendOutcomeUnknown):
            await assert_target_outcome_known(authority, "target")
    authority = RunEventAuthority(store, session_id=SESSION, run_id="new-user-run", guard=guard)
    with bind_run_event_authority(authority):
        await assert_target_outcome_known(authority, "target")


@pytest.mark.asyncio
@pytest.mark.parametrize("write", [False, True])
async def test_pool_acquire_allows_a_cold_connection_within_connect_budget(pg_store, write):
    from ksadk.sessions.db_budget import DatabaseBudget, budgeted_pool_connection

    class ColdLease:
        def __init__(self):
            self.lease = pg_store._pool.acquire()

        async def __aenter__(self):
            connection = await self.lease.__aenter__()
            try:
                # A valid cold connect can exceed the old 1s pool wait while
                # remaining inside the independent 2s connect phase.
                await asyncio.sleep(1.1)
                return connection
            except BaseException:
                await self.lease.__aexit__(None, None, None)
                raise

        async def __aexit__(self, *args):
            return await self.lease.__aexit__(*args)

    pool = SimpleNamespace(acquire=lambda **kwargs: ColdLease())
    old = replace(DatabaseBudget(), read_acquire_seconds=1, write_acquire_seconds=1)
    with pytest.raises(TimeoutError):
        async with budgeted_pool_connection(pool, old, write=write):
            pytest.fail("old pool phase unexpectedly accepted the delayed connect")
    async with budgeted_pool_connection(pool, DatabaseBudget(), write=write) as connection:
        assert await connection.fetchval("SELECT 42") == 42
