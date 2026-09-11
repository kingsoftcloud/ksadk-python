from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from ksadk.events.canonical import RunCompleted, RunStarted, SourceRef
from ksadk.events.session_event import SessionServiceEventStore
from ksadk.kernel.contracts import AgentControlCommand, ControlSource
from ksadk.kernel.errors import InvalidCommandError, StaleFenceError
from ksadk.kernel.execution_grants import (
    ExecutionGrantBlocked,
    ExecutionGrantSpec,
    execution_grant_run_id,
)
from ksadk.kernel.memory_store import InMemoryAgentKernelStore
from ksadk.kernel.sqlite_store import SQLiteAgentKernelStore
from ksadk.kernel.state import InboxState, RunState
from ksadk.kernel.store import ActivationLeaseRequest, now_iso
from ksadk.kernel.worker import AgentKernelWorker
from ksadk.runtime import (
    BaseRuntime,
    CancelResult,
    CheckpointCapability,
    CheckpointDescriptor,
    RunHandle,
    RuntimeAdapter,
)
from ksadk.sessions.in_memory import InMemorySessionService

SPEC = ExecutionGrantSpec(
    grant_id="occurrence-1:member-1",
    tenant_id="test-tenant",
    agent_instance_id="test-agent",
    session_id="test-session",
    owner_ref="trusted-host://test-owner",
)


def command(key: str, *, grant_id: str | None = SPEC.grant_id, **overrides):
    values = dict(
        command_id=uuid4(),
        idempotency_key=key,
        tenant_id=SPEC.tenant_id,
        agent_instance_id=SPEC.agent_instance_id,
        session_id=SPEC.session_id,
        command_type="enqueue",
        payload={"content": key},
        source=ControlSource(kind="scheduler", ref="test-only"),
        authorization_ref="test-permit",
        submitted_at=now_iso(),
    )
    if grant_id is not None:
        values["payload"]["execution_grant_id"] = grant_id
    values.update(overrides)
    return AgentControlCommand(**values)


@dataclass
class Harness:
    store: object
    events: object
    session_service: object
    path: object
    backend: str

    async def lease(self, owner="owner-1"):
        return await self.store.acquire_activation(
            ActivationLeaseRequest(
                agent_instance_id=SPEC.agent_instance_id,
                session_id=SPEC.session_id,
                activation_id=owner,
                lease_ttl_seconds=60,
            )
        )

    async def reopen(self):
        assert self.backend == "sqlite"
        await self.store.close()
        self.store = SQLiteAgentKernelStore(self.path, self.events)
        await self.store.ensure_schema()


BACKENDS = ["memory", "sqlite"]
if os.environ.get("KSADK_TEST_GRANTS_POSTGRES_DSN"):
    BACKENDS.append("postgres")


@pytest.fixture(params=BACKENDS)
async def harness(request, tmp_path):
    sessions = InMemorySessionService()
    await sessions.create_session(
        agent_id=SPEC.agent_instance_id, user_id=SPEC.tenant_id, session_id=SPEC.session_id
    )
    events = SessionServiceEventStore(sessions)
    path = tmp_path / "kernel.sqlite"
    pg_cleanup = None
    if request.param == "postgres":
        import asyncpg

        from ksadk.kernel.postgres_store import PostgresAgentKernelStore

        # An explicitly configured test DSN is used only inside a fresh schema.
        dsn = os.environ["KSADK_TEST_GRANTS_POSTGRES_DSN"]
        schema = "grant_test_" + uuid4().hex
        admin = await asyncpg.connect(dsn)
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await asyncpg.create_pool(dsn, server_settings={"search_path": schema})

        class EventLog:
            async def append_on(self, connection, envelope, guard):
                # The test isolates the real PG control transaction. Canonical
                # runtime events still use the normal SessionServiceEventStore.
                return envelope

        store = PostgresAgentKernelStore(pool, EventLog(), tenant_id=SPEC.tenant_id)
        await store.ensure_schema()
        pg_cleanup = (pool, admin, schema)
    else:
        store = (
            InMemoryAgentKernelStore(events)
            if request.param == "memory"
            else SQLiteAgentKernelStore(path, events)
        )
    if request.param == "sqlite":
        await store.ensure_schema()
    result = Harness(store, events, sessions, path, request.param)
    yield result
    if request.param == "sqlite":
        await result.store.close()
    if pg_cleanup:
        pool, admin, schema = pg_cleanup
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


class FixtureRuntime(BaseRuntime):
    runtime_type = "grant-test"

    def native_capabilities(self):
        return {}


class FixtureAdapter(RuntimeAdapter):
    def __init__(self, *, finish=None, start_entered=None, start_release=None, fail_start=False):
        super().__init__(FixtureRuntime())
        self.started = []
        self.cancelled = []
        self.finish = finish
        self.start_entered = start_entered
        self.start_release = start_release
        self.fail_start = fail_start

    async def start(self, request):
        self.started.append(request)
        if self.start_entered:
            self.start_entered.set()
        if self.start_release:
            await self.start_release.wait()
        if self.fail_start:
            raise RuntimeError("fixture lost the native start acknowledgement")
        return RunHandle(
            run_id=request.metadata["run_id"],
            session_id=request.session_id,
            runtime_type="grant-test",
            native_ref={"test": True},
        )

    async def stream(self, handle):
        common = dict(
            schema_version=2,
            timestamp=1.0,
            run_id=handle.run_id,
            scope_id=handle.run_id,
            source=SourceRef(framework="ksadk"),
        )
        yield RunStarted(event_id=f"{handle.run_id}:1", seq=1, status="running", **common)
        if self.finish:
            await self.finish.wait()
        yield RunCompleted(
            event_id=f"{handle.run_id}:2", seq=2, status="completed", output_refs=(), **common
        )

    async def cancel(self, handle):
        self.cancelled.append(handle.run_id)
        if self.finish:
            self.finish.set()
        return CancelResult.INTERRUPTED_ACTIVE_TURN

    async def resume(self, handle, target, payload):
        return handle

    async def checkpoint(self, handle):
        return CheckpointDescriptor(
            checkpoint_id=handle.run_id,
            invocation_id=handle.run_id,
            capability=CheckpointCapability(
                supported=False,
                granularity="none",
                rollback_scope="none",
                fork_supported=False,
                durable=False,
                shared_across_pods=False,
            ),
        )

    async def close(self, handle):
        pass


def worker(harness, adapter):
    return AgentKernelWorker(
        harness.store,
        adapter_factory=lambda: adapter,
        session_events=harness.events,
        session_service=harness.session_service,
    )


async def run_once(worker, lease):
    return await worker.run_once(SPEC.agent_instance_id, lease, session_id=SPEC.session_id)


async def wait_settled(harness, spec=SPEC):
    async def poll():
        while True:
            state = await harness.store.get_execution_grant(spec)
            if not state.in_flight_message_ids:
                return state
            await asyncio.sleep(0.001)

    return await asyncio.wait_for(poll(), 3)


async def test_grant_scope_cas_irreversibility_and_lost_ack(harness):
    store = harness.store
    first = await store.ensure_execution_grant(SPEC)
    assert first.state == "active"
    assert await store.ensure_execution_grant(SPEC) == first
    for field in ("tenant_id", "agent_instance_id", "session_id", "owner_ref"):
        with pytest.raises(InvalidCommandError):
            await store.ensure_execution_grant(SPEC.model_copy(update={field: "other"}))
    suspended = await store.set_execution_grant_state(
        SPEC,
        "suspended",
        expected_revision=1,
        idempotency_key="pause",
    )
    assert suspended.grant.revision == 2
    with pytest.raises(InvalidCommandError, match="revision mismatch"):
        await store.set_execution_grant_state(
            SPEC, "active", expected_revision=1, idempotency_key="stale"
        )
    revoked = await store.set_execution_grant_state(
        SPEC,
        "revoked",
        expected_revision=2,
        idempotency_key="stop",
    )
    assert revoked.grant.revision == 3
    # ACK of pause was lost. Retrying returns its exact receipt even after stop.
    assert (
        await store.set_execution_grant_state(
            SPEC,
            "suspended",
            expected_revision=1,
            idempotency_key="pause",
        )
        == suspended
    )
    with pytest.raises(InvalidCommandError, match="idempotency conflict"):
        await store.set_execution_grant_state(
            SPEC, "revoked", expected_revision=1, idempotency_key="pause"
        )
    with pytest.raises(InvalidCommandError, match="cannot be reactivated"):
        await store.set_execution_grant_state(
            SPEC, "active", expected_revision=3, idempotency_key="revive"
        )
    assert (await store.ensure_execution_grant(SPEC)).state == "revoked"
    assert (await store.get_execution_grant(SPEC)).grant == revoked.grant


async def test_admission_requires_existing_exact_grant_and_preserves_duplicate(harness):
    store = harness.store
    submitted = command("one")
    missing = await store.accept_command(submitted, queue_limit=10)
    assert missing.error.code == "execution_grant_not_found"
    await store.ensure_execution_grant(SPEC)
    wrong = command("wrong", tenant_id="other-tenant")
    assert (
        await store.accept_command(wrong, queue_limit=10)
    ).error.code == "execution_grant_scope_mismatch"
    accepted = await store.accept_command(submitted, queue_limit=10)
    stop = await store.set_execution_grant_state(
        SPEC, "revoked", expected_revision=1, idempotency_key="stop"
    )
    assert stop.discarded_message_ids == (str(accepted.message_id),)
    retry = await store.accept_command(submitted, queue_limit=10)
    assert retry.status == "duplicate" and retry.message_id == accepted.message_id
    assert (
        await store.accept_command(command("new"), queue_limit=10)
    ).error.code == "execution_grant_revoked"
    assert (await store.load_message(accepted.message_id)).status == InboxState.DISCARDED


async def test_suspended_queue_resumes_same_command_once(harness):
    store = harness.store
    await store.ensure_execution_grant(SPEC)
    submitted = command("one")
    accepted = await store.accept_command(submitted, queue_limit=10)
    lease = await harness.lease()
    adapter = FixtureAdapter()
    executor = worker(harness, adapter)
    await store.set_execution_grant_state(
        SPEC, "suspended", expected_revision=1, idempotency_key="pause"
    )
    assert (await run_once(executor, lease)).outcome == "idle"
    assert not adapter.started
    assert (await store.load_message(accepted.message_id)).status == InboxState.ACCEPTED
    resumed = await store.set_execution_grant_state(
        SPEC, "active", expected_revision=2, idempotency_key="resume"
    )
    assert resumed.queued_message_ids == (str(accepted.message_id),)
    result = await run_once(executor, lease)
    assert result.run_id == execution_grant_run_id(submitted)
    state = await wait_settled(harness)
    assert state.settled_message_ids == (str(accepted.message_id),)
    assert (await run_once(executor, lease)).outcome == "idle"
    assert len(adapter.started) == 1


async def test_revocation_between_list_and_claim_prevents_start(harness, monkeypatch):
    store = harness.store
    await store.ensure_execution_grant(SPEC)
    accepted = await store.accept_command(command("one"), queue_limit=10)
    lease = await harness.lease()
    listed, release = asyncio.Event(), asyncio.Event()
    original = store.list_pending

    async def paused_list(*args, **kwargs):
        messages = await original(*args, **kwargs)
        listed.set()
        await release.wait()
        return messages

    monkeypatch.setattr(store, "list_pending", paused_list)
    adapter = FixtureAdapter()
    running = asyncio.create_task(run_once(worker(harness, adapter), lease))
    await asyncio.wait_for(listed.wait(), 2)
    barrier = await store.set_execution_grant_state(
        SPEC, "revoked", expected_revision=1, idempotency_key="stop"
    )
    assert barrier.discarded_message_ids == (str(accepted.message_id),)
    release.set()
    assert (await asyncio.wait_for(running, 2)).outcome == "idle"
    assert not adapter.started


async def test_claim_before_barrier_remains_inflight_until_terminal(harness):
    store = harness.store
    await store.ensure_execution_grant(SPEC)
    accepted = await store.accept_command(command("one"), queue_limit=10)
    lease = await harness.lease()
    entered, release, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    adapter = FixtureAdapter(start_entered=entered, start_release=release, finish=finish)
    running = asyncio.create_task(run_once(worker(harness, adapter), lease))
    await asyncio.wait_for(entered.wait(), 2)
    barrier = await store.set_execution_grant_state(
        SPEC, "revoked", expected_revision=1, idempotency_key="stop"
    )
    assert barrier.in_flight_message_ids == (str(accepted.message_id),)
    assert not barrier.discarded_message_ids
    release.set()
    assert (await asyncio.wait_for(running, 2)).outcome == "completed"
    # Inbox completed while the provider is still producing output.
    assert (await store.get_execution_grant(SPEC)).in_flight_message_ids == (
        str(accepted.message_id),
    )
    finish.set()
    assert (await wait_settled(harness)).settled_message_ids == (str(accepted.message_id),)


async def test_grant_is_per_occurrence_not_entire_session(harness):
    store = harness.store
    second = SPEC.model_copy(update={"grant_id": "occurrence-2:member-1"})
    await store.ensure_execution_grant(SPEC)
    await store.ensure_execution_grant(second)
    one = await store.accept_command(command("one"), queue_limit=10)
    two = await store.accept_command(command("two", grant_id=second.grant_id), queue_limit=10)
    await store.set_execution_grant_state(
        SPEC, "revoked", expected_revision=1, idempotency_key="stop"
    )
    adapter = FixtureAdapter()
    await run_once(worker(harness, adapter), await harness.lease())
    state = await wait_settled(harness, second)
    assert state.settled_message_ids == (str(two.message_id),)
    assert (await store.load_message(one.message_id)).status == InboxState.DISCARDED
    assert [request.input for request in adapter.started] == ["two"]


async def test_takeover_of_qualified_command_keeps_original_eligibility(harness):
    store = harness.store
    await store.ensure_execution_grant(SPEC)
    accepted = await store.accept_command(command("one"), queue_limit=10)
    old = await harness.lease()
    await store.claim_message(accepted.message_id, old.fencing_token)
    await store.set_execution_grant_state(
        SPEC, "revoked", expected_revision=1, idempotency_key="stop"
    )
    await store.release_activation(old.activation_id, expected_fence=old.fencing_token)
    new = await harness.lease("owner-2")
    with pytest.raises(StaleFenceError):
        await store.claim_message(accepted.message_id, old.fencing_token)
    adapter = FixtureAdapter()
    assert (await run_once(worker(harness, adapter), new)).outcome == "completed"
    await wait_settled(harness)
    assert len(adapter.started) == 1


async def test_uncertain_native_start_is_never_replayed_after_recovery(harness):
    store = harness.store
    await store.ensure_execution_grant(SPEC)
    submitted = command("one")
    accepted = await store.accept_command(submitted, queue_limit=10)
    lease = await harness.lease()
    adapter = FixtureAdapter(fail_start=True)
    assert (await run_once(worker(harness, adapter), lease)).outcome == "terminal_failure"
    assert len(adapter.started) == 1
    barrier = await store.set_execution_grant_state(
        SPEC, "revoked", expected_revision=1, idempotency_key="stop"
    )
    assert barrier.in_flight_message_ids == (str(accepted.message_id),)
    recovered = FixtureAdapter()
    result = await run_once(worker(harness, recovered), lease)
    assert result.run_id == execution_grant_run_id(submitted)
    record = await store.load_run(result.run_id)
    assert record.state == RunState.INTERRUPTED
    assert record.metadata["reason"] == "execution_start_uncertain"
    assert not recovered.started
    assert (await store.get_execution_grant(SPEC)).settled_message_ids == (
        str(accepted.message_id),
    )


async def test_legacy_commands_without_grants_keep_working(harness):
    accepted = await harness.store.accept_command(command("legacy", grant_id=None), queue_limit=10)
    adapter = FixtureAdapter()
    result = await run_once(worker(harness, adapter), await harness.lease())
    assert result.outcome == "completed" and result.message_id == str(accepted.message_id)
    while (await harness.store.load_run(result.run_id)).state == RunState.RUNNING:
        await asyncio.sleep(0.001)
    assert len(adapter.started) == 1


async def test_later_grant_cannot_skip_suspended_fifo_head(harness):
    store = harness.store
    later = SPEC.model_copy(update={"grant_id": "later-occurrence"})
    await store.ensure_execution_grant(SPEC)
    await store.ensure_execution_grant(later)
    await store.accept_command(command("first"), queue_limit=10)
    accepted = await store.accept_command(
        command("second", grant_id=later.grant_id), queue_limit=10
    )
    await store.set_execution_grant_state(
        SPEC, "suspended", expected_revision=1, idempotency_key="pause"
    )
    lease = await harness.lease()
    # Even a second worker with a stale list cannot directly claim the later
    # command: FIFO is checked together with grant qualification in the store.
    with pytest.raises(ExecutionGrantBlocked) as blocked:
        await store.claim_message(accepted.message_id, lease.fencing_token)
    assert blocked.value.grant_state == "queued"
    assert (await store.load_message(accepted.message_id)).status == InboxState.ACCEPTED
    await store.set_execution_grant_state(
        SPEC, "revoked", expected_revision=2, idempotency_key="stop"
    )
    assert (
        await store.claim_message(accepted.message_id, lease.fencing_token)
    ).status == InboxState.CLAIMED


async def test_control_behind_suspended_enqueue_still_cancels_active_run(harness):
    store = harness.store
    await store.ensure_execution_grant(SPEC)
    await store.accept_command(command("first"), queue_limit=10)
    finish = asyncio.Event()
    adapter = FixtureAdapter(finish=finish)
    executor = worker(harness, adapter)
    lease = await harness.lease()
    result = await run_once(executor, lease)
    queued = await store.accept_command(command("second"), queue_limit=10)
    await store.set_execution_grant_state(
        SPEC, "suspended", expected_revision=1, idempotency_key="pause"
    )
    control = command(
        "cancel", grant_id=None, command_type="interrupt", payload={"run_id": result.run_id}
    )
    await store.accept_command(control, queue_limit=10)
    assert (await run_once(executor, lease)).outcome == "completed"
    assert adapter.cancelled == [result.run_id]
    assert len(adapter.started) == 1
    assert (await store.load_message(queued.message_id)).status == InboxState.ACCEPTED
    assert (await store.load_run(result.run_id)).state == RunState.CANCELLED
    await asyncio.sleep(0.01)  # let the cancelled provider's terminal stream drain


async def test_public_kernel_facade_still_requires_valid_permit(harness):
    from ksadk.kernel.control import AgentKernel
    from ksadk.kernel.ingress import InProcessPermitIssuer

    issuer = InProcessPermitIssuer()
    kernel = AgentKernel(harness.store, harness.events, issuer.verifier())
    grant = await kernel.ensure_execution_grant(SPEC)
    permit = issuer.issue(
        tenant_id=SPEC.tenant_id,
        agent_instance_id=SPEC.agent_instance_id,
        session_id=SPEC.session_id,
        operations=("enqueue",),
    )
    submitted = command("one", authorization_ref=permit.permit_id)
    rejected = await kernel.submit(
        submitted, permit=permit.model_copy(update={"signature": "AAAA"})
    )
    assert rejected.status == "rejected" and rejected.error.code == "invalid_permit"
    assert not (await kernel.get_execution_grant(SPEC)).commands
    accepted = await kernel.submit(submitted, permit=permit)
    assert accepted.status == "accepted"
    barrier = await kernel.set_execution_grant_state(
        SPEC, "revoked", expected_revision=grant.revision, idempotency_key="stop"
    )
    schema_path = (
        Path(__file__).parents[2] / "contracts/agent-kernel/execution-grants/v1/barrier.schema.json"
    )
    from jsonschema import Draft202012Validator

    Draft202012Validator(json.loads(schema_path.read_text())).validate(
        barrier.model_dump(mode="json")
    )
    assert barrier.discarded_message_ids == (str(accepted.message_id),)


@pytest.mark.parametrize("harness", ["sqlite"], indirect=True)
async def test_sqlite_restart_preserves_queue_and_exact_barrier_receipt(harness):
    await harness.store.ensure_execution_grant(SPEC)
    accepted = await harness.store.accept_command(command("one"), queue_limit=10)
    receipt = await harness.store.set_execution_grant_state(
        SPEC,
        "suspended",
        expected_revision=1,
        idempotency_key="lost-ack",
    )
    await harness.reopen()
    assert (await harness.store.get_execution_grant(SPEC)).grant.state == "suspended"
    assert (
        await harness.store.set_execution_grant_state(
            SPEC,
            "suspended",
            expected_revision=1,
            idempotency_key="lost-ack",
        )
        == receipt
    )
    await harness.store.set_execution_grant_state(
        SPEC, "active", expected_revision=2, idempotency_key="resume"
    )
    adapter = FixtureAdapter()
    await run_once(worker(harness, adapter), await harness.lease())
    assert (await wait_settled(harness)).settled_message_ids == (str(accepted.message_id),)
    assert len(adapter.started) == 1


@pytest.mark.parametrize("harness", ["sqlite"], indirect=True)
async def test_sqlite_barrier_failure_rolls_back_state_queue_and_receipt(harness, monkeypatch):
    store = harness.store
    await store.ensure_execution_grant(SPEC)
    accepted = await store.accept_command(command("one"), queue_limit=10)
    original = store._sqlite_grant_barrier

    async def crash(*args):
        raise RuntimeError("fixture crash before receipt/commit")

    monkeypatch.setattr(store, "_sqlite_grant_barrier", crash)
    with pytest.raises(RuntimeError, match="fixture crash"):
        await store.set_execution_grant_state(
            SPEC, "revoked", expected_revision=1, idempotency_key="stop"
        )
    monkeypatch.setattr(store, "_sqlite_grant_barrier", original)
    state = await store.get_execution_grant(SPEC)
    assert state.grant.state == "active" and state.grant.revision == 1
    assert state.queued_message_ids == (str(accepted.message_id),)
    stopped = await store.set_execution_grant_state(
        SPEC, "revoked", expected_revision=1, idempotency_key="stop"
    )
    assert stopped.discarded_message_ids == (str(accepted.message_id),)


async def test_concurrent_grant_updates_have_one_cas_winner(harness):
    await harness.store.ensure_execution_grant(SPEC)
    results = await asyncio.gather(
        harness.store.set_execution_grant_state(
            SPEC, "suspended", expected_revision=1, idempotency_key="pause"
        ),
        harness.store.set_execution_grant_state(
            SPEC, "revoked", expected_revision=1, idempotency_key="stop"
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, InvalidCommandError) for result in results) == 1
    assert (await harness.store.get_execution_grant(SPEC)).grant.revision == 2


@pytest.mark.parametrize("harness", ["sqlite"], indirect=True)
async def test_sqlite_process_death_before_barrier_commit_preserves_queue(harness):
    await harness.store.ensure_execution_grant(SPEC)
    accepted = await harness.store.accept_command(command("one"), queue_limit=10)
    script = """
import asyncio, json, os, sys
from ksadk.events.session_event import SessionServiceEventStore
from ksadk.kernel.execution_grants import ExecutionGrantSpec
from ksadk.kernel.sqlite_store import SQLiteAgentKernelStore
from ksadk.sessions.in_memory import InMemorySessionService
async def main():
    store = SQLiteAgentKernelStore(sys.argv[1], SessionServiceEventStore(InMemorySessionService()))
    await store.ensure_schema()
    async def die_after_queue_mutation(*args):
        os._exit(23)
    store._sqlite_grant_barrier = die_after_queue_mutation
    await store.set_execution_grant_state(
        ExecutionGrantSpec.model_validate(json.loads(sys.stdin.read())),
        "revoked", expected_revision=1, idempotency_key="process-death",
    )
asyncio.run(main())
"""
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script, str(harness.path)],
        input=SPEC.model_dump_json(),
        text=True,
        capture_output=True,
        cwd=Path(__file__).parents[2],
        timeout=10,
    )
    assert result.returncode == 23, result.stderr
    state = await harness.store.get_execution_grant(SPEC)
    assert state.grant.state == "active" and state.grant.revision == 1
    assert state.queued_message_ids == (str(accepted.message_id),)
    receipt = await harness.store.set_execution_grant_state(
        SPEC, "revoked", expected_revision=1, idempotency_key="process-death"
    )
    assert receipt.discarded_message_ids == (str(accepted.message_id),)


@pytest.mark.parametrize("harness", ["sqlite"], indirect=True)
async def test_sqlite_cross_connection_claim_and_revoke_have_one_transaction_order(
    harness, monkeypatch
):
    store = harness.store
    await store.ensure_execution_grant(SPEC)
    accepted = await store.accept_command(command("one"), queue_limit=10)
    lease = await harness.lease()
    second = SQLiteAgentKernelStore(harness.path, harness.events)
    await second.ensure_schema()
    checked, release = asyncio.Event(), asyncio.Event()
    original = store._sqlite_require_claim_grant

    async def hold_after_check(connection, row):
        await original(connection, row)
        checked.set()
        await release.wait()

    monkeypatch.setattr(store, "_sqlite_require_claim_grant", hold_after_check)
    try:
        claiming = asyncio.create_task(
            store.claim_message(accepted.message_id, lease.fencing_token)
        )
        await asyncio.wait_for(checked.wait(), 2)
        revoking = asyncio.create_task(
            second.set_execution_grant_state(
                SPEC,
                "revoked",
                expected_revision=1,
                idempotency_key="stop",
            )
        )
        # A separate connection has attempted the revoke while the first
        # transaction is deliberately held between grant check and claim.
        await asyncio.sleep(0.03)
        assert not revoking.done()
        release.set()
        await asyncio.wait_for(claiming, 2)
        barrier = await asyncio.wait_for(revoking, 2)
        assert barrier.in_flight_message_ids == (str(accepted.message_id),)
        assert not barrier.discarded_message_ids
    finally:
        release.set()
        await second.close()
