from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from ksadk.events.canonical import RunCompleted, RunStarted, SourceRef
from ksadk.events.session_event import SessionServiceEventStore
from ksadk.kernel.contracts import AgentControlCommand, ControlSource
from ksadk.kernel.errors import InvalidCommandError, StaleFenceError
from ksadk.kernel.execution_grants import (
    ExecutionGrantBlocked,
    ExecutionGrantRecord,
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


async def test_admission_pause_blocks_claim_but_keeps_grant_active(harness):
    store = harness.store
    record = await store.ensure_execution_grant(SPEC)
    submitted = command("pause-queue")
    accepted = await store.accept_command(submitted, queue_limit=10)
    paused = await store.set_execution_admission(
        SPEC, False, expected_revision=1, idempotency_key="pause-admission"
    )
    assert paused.grant.state == "active"
    assert paused.grant.revision == record.revision
    assert paused.grant.admission_revision == 2
    assert not paused.grant.admission_allowed
    assert (await store.require_execution_grant(SPEC)).state == "active"
    adapter = FixtureAdapter()
    executor, lease = worker(harness, adapter), await harness.lease()
    assert (await run_once(executor, lease)).outcome == "idle"
    assert not adapter.started
    assert (await store.load_message(accepted.message_id)).status == InboxState.ACCEPTED
    await store.set_execution_admission(
        SPEC, True, expected_revision=2, idempotency_key="resume-admission"
    )
    result = await run_once(executor, lease)
    assert result.run_id == execution_grant_run_id(submitted)
    await wait_settled(harness)
    assert len(adapter.started) == 1


async def test_governed_enqueue_carries_opaque_context_to_runtime(harness):
    await harness.store.ensure_execution_grant(SPEC)
    submitted = command("context-forward")
    submitted = submitted.model_copy(
        update={
            "payload": {
                **submitted.payload,
                "teams_context_ref": "context-example",
                "execution_policy_ref": "policy-example",
            }
        }
    )
    await harness.store.accept_command(submitted, queue_limit=10)
    adapter = FixtureAdapter()
    await run_once(worker(harness, adapter), await harness.lease())
    await wait_settled(harness)
    assert adapter.started[0].metadata["teams_context_ref"] == "context-example"
    assert adapter.started[0].metadata["execution_policy_ref"] == "policy-example"


async def test_admission_pause_claim_race_uses_kernel_transaction(harness, monkeypatch):
    store = harness.store
    await store.ensure_execution_grant(SPEC)
    await store.accept_command(command("race-pause"), queue_limit=10)
    lease = await harness.lease()
    listed, release = asyncio.Event(), asyncio.Event()
    original = store.list_pending

    async def delayed_list(*args, **kwargs):
        result = await original(*args, **kwargs)
        listed.set()
        await release.wait()
        return result

    monkeypatch.setattr(store, "list_pending", delayed_list)
    adapter = FixtureAdapter()
    running = asyncio.create_task(run_once(worker(harness, adapter), lease))
    await asyncio.wait_for(listed.wait(), 2)
    await store.set_execution_admission(
        SPEC, False, expected_revision=1, idempotency_key="pause-race"
    )
    release.set()
    assert (await asyncio.wait_for(running, 2)).outcome == "idle"
    assert not adapter.started


async def test_inflight_keeps_tool_eligibility_after_admission_pause(harness):
    store = harness.store
    await store.ensure_execution_grant(SPEC)
    accepted = await store.accept_command(command("running-before-pause"), queue_limit=10)
    entered, release, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    adapter = FixtureAdapter(start_entered=entered, start_release=release, finish=finish)
    running = asyncio.create_task(run_once(worker(harness, adapter), await harness.lease()))
    await asyncio.wait_for(entered.wait(), 2)
    barrier = await store.set_execution_admission(
        SPEC, False, expected_revision=1, idempotency_key="pause-inflight"
    )
    assert barrier.in_flight_message_ids == (str(accepted.message_id),)
    assert (await store.require_execution_grant(SPEC)).state == "active"
    release.set()
    assert (await asyncio.wait_for(running, 2)).outcome == "completed"
    finish.set()
    await wait_settled(harness)


async def test_admission_pause_and_grant_renewal_have_independent_revisions(harness):
    store = harness.store
    spec = SPEC.model_copy(update={"attempt_epoch": 1})
    now = datetime.now(timezone.utc)
    await store.ensure_execution_grant(spec, expires_at=(now + timedelta(seconds=20)).isoformat())
    paused = await store.set_execution_admission(
        spec, False, expected_revision=1, idempotency_key="pause-expiring"
    )
    renewed = await store.renew_execution_grant(
        spec,
        expected_revision=1,
        expires_at=(now + timedelta(seconds=30)).isoformat(),
        renewal_id="renew-while-paused",
    )
    assert renewed.grant.revision == 2
    assert renewed.grant.admission_revision == 2
    assert renewed.grant.admission_allowed is False
    assert (
        await store.set_execution_admission(
            spec, False, expected_revision=1, idempotency_key="pause-expiring"
        )
        == paused
    )  # Response loss returns original evidence, not current state.
    assert await store.require_execution_grant(spec)
    resumed = await store.set_execution_admission(
        spec, True, expected_revision=2, idempotency_key="resume-expiring"
    )
    assert resumed.grant.revision == 2
    assert resumed.grant.admission_revision == 3


async def test_admission_scope_cas_and_replay_cannot_revive_stopped_grant(harness):
    store = harness.store
    await store.ensure_execution_grant(SPEC)
    paused = await store.set_execution_admission(
        SPEC, False, expected_revision=1, idempotency_key="pause-persisted"
    )
    if harness.backend == "sqlite":
        await harness.reopen()
        store = harness.store
    assert not (await store.get_execution_grant(SPEC)).grant.admission_allowed
    with pytest.raises(InvalidCommandError, match="revision conflict"):
        await store.set_execution_admission(
            SPEC, True, expected_revision=1, idempotency_key="stale-admission"
        )
    with pytest.raises(InvalidCommandError, match="idempotency conflict"):
        await store.set_execution_admission(
            SPEC, True, expected_revision=1, idempotency_key="pause-persisted"
        )
    with pytest.raises(InvalidCommandError, match="scope|tenant"):
        await store.set_execution_admission(
            SPEC.model_copy(update={"owner_ref": "other"}),
            True,
            expected_revision=2,
            idempotency_key="other-owner",
        )
    await store.set_execution_grant_state(
        SPEC, "revoked", expected_revision=1, idempotency_key="stop-after-pause"
    )
    assert (
        await store.set_execution_admission(
            SPEC, False, expected_revision=1, idempotency_key="pause-persisted"
        )
        == paused
    )
    with pytest.raises(ExecutionGrantBlocked, match="revoked"):
        await store.set_execution_admission(
            SPEC, True, expected_revision=2, idempotency_key="resume-stopped"
        )


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
@pytest.mark.local_process_heavy
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


TIMED_SPEC = SPEC.model_copy(update={"attempt_epoch": 7})


def deadline(seconds: int = 60) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def timed_command(key: str, epoch=7):
    return command(
        key,
        payload={
            "content": key,
            "execution_grant_id": SPEC.grant_id,
            "execution_grant_attempt_epoch": epoch,
        },
    )


def advance_grant_clock(harness, monkeypatch, instant):
    import ksadk.kernel.execution_grants as grants

    monkeypatch.setattr(grants, "grant_now", lambda: instant)
    if harness.backend == "postgres":

        async def database_now(connection):
            return instant

        monkeypatch.setattr(harness.store, "_postgres_grant_now", database_now)


@pytest.mark.parametrize("already_claimed", [False, True])
async def test_expiry_is_checked_at_claim_and_tool_boundary(harness, monkeypatch, already_claimed):
    store = harness.store
    expires = deadline()
    await store.ensure_execution_grant(TIMED_SPEC, expires_at=expires)
    submitted = timed_command("original")
    receipt = await store.accept_command(submitted, queue_limit=10)
    lease = await harness.lease()
    if already_claimed:
        await store.claim_message(receipt.message_id, lease.fencing_token)
    advance_grant_clock(harness, monkeypatch, expires)
    with pytest.raises(ExecutionGrantBlocked, match="expired"):
        await store.claim_message(receipt.message_id, lease.fencing_token)
    with pytest.raises(ExecutionGrantBlocked, match="expired"):
        await store.require_execution_grant(TIMED_SPEC)
    rejected = await store.accept_command(timed_command("new"), queue_limit=10)
    assert rejected.error.code == "execution_grant_expired"
    # A read/retry still returns the original accepted receipt after expiry.
    duplicate = await store.accept_command(submitted, queue_limit=10)
    assert duplicate.status == "duplicate" and duplicate.message_id == receipt.message_id
    current = await store.get_execution_grant(TIMED_SPEC)
    assert current.grant.expires_at == expires
    assert (current.in_flight_message_ids if already_claimed else current.queued_message_ids) == (
        str(receipt.message_id),
    )


async def test_attempt_epoch_required_and_is_immutable(harness):
    store = harness.store
    await store.ensure_execution_grant(TIMED_SPEC, expires_at=deadline())
    for submitted in (command("missing"), timed_command("stale", 6)):
        receipt = await store.accept_command(submitted, queue_limit=10)
        assert receipt.error.code == "execution_grant_attempt_epoch_mismatch"
    wrong_epoch = TIMED_SPEC.model_copy(update={"attempt_epoch": 8})
    with pytest.raises(InvalidCommandError, match="scope or owner mismatch"):
        await store.ensure_execution_grant(wrong_epoch, expires_at=deadline())
    with pytest.raises(InvalidCommandError, match="scope or owner mismatch"):
        await store.require_execution_grant(wrong_epoch)
    assert (await store.accept_command(timed_command("right"), queue_limit=10)).status == "accepted"


async def test_renewal_cas_lost_ack_lookup_and_revoke(harness):
    store = harness.store
    initial, extended = deadline(), deadline(120)
    await store.ensure_execution_grant(TIMED_SPEC, expires_at=initial)
    receipt = await store.renew_execution_grant(
        TIMED_SPEC,
        expected_revision=1,
        expires_at=extended,
        renewal_id="renew-1",
    )
    assert receipt.grant.revision == 2 and receipt.grant.expires_at == extended
    assert (
        await store.lookup_execution_grant_operation(TIMED_SPEC, idempotency_key="absent") is None
    )
    assert (
        await store.lookup_execution_grant_operation(TIMED_SPEC, idempotency_key="renew-1")
        == receipt
    )
    with pytest.raises(InvalidCommandError, match="idempotency conflict"):
        await store.renew_execution_grant(
            TIMED_SPEC,
            expected_revision=1,
            expires_at=deadline(180),
            renewal_id="renew-1",
        )
    with pytest.raises(InvalidCommandError, match="revision mismatch"):
        await store.renew_execution_grant(
            TIMED_SPEC,
            expected_revision=1,
            expires_at=deadline(180),
            renewal_id="stale",
        )
    await store.set_execution_grant_state(
        TIMED_SPEC, "revoked", expected_revision=2, idempotency_key="stop"
    )
    assert (
        await store.renew_execution_grant(
            TIMED_SPEC,
            expected_revision=1,
            expires_at=extended,
            renewal_id="renew-1",
        )
        == receipt
    )
    with pytest.raises(ExecutionGrantBlocked, match="revoked"):
        await store.renew_execution_grant(
            TIMED_SPEC,
            expected_revision=3,
            expires_at=deadline(180),
            renewal_id="revive",
        )
    assert (await store.get_execution_grant(TIMED_SPEC)).grant.state == "revoked"


async def test_renewal_race_has_one_winner(harness):
    store = harness.store
    await store.ensure_execution_grant(TIMED_SPEC, expires_at=deadline())
    results = await asyncio.gather(
        *(
            store.renew_execution_grant(
                TIMED_SPEC,
                expected_revision=1,
                expires_at=deadline(120 + index),
                renewal_id=f"renew-{index}",
            )
            for index in range(2)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, InvalidCommandError) for result in results) == 1
    assert (await store.get_execution_grant(TIMED_SPEC)).grant.revision == 2


async def test_expired_or_suspended_grant_cannot_be_renewed(harness):
    store = harness.store
    await store.ensure_execution_grant(TIMED_SPEC, expires_at=deadline(-1))
    with pytest.raises(ExecutionGrantBlocked, match="expired"):
        await store.renew_execution_grant(
            TIMED_SPEC,
            expected_revision=1,
            expires_at=deadline(120),
            renewal_id="late",
        )
    other = TIMED_SPEC.model_copy(update={"grant_id": "suspended"})
    await store.ensure_execution_grant(other, expires_at=deadline())
    await store.set_execution_grant_state(
        other, "suspended", expected_revision=1, idempotency_key="pause"
    )
    with pytest.raises(ExecutionGrantBlocked, match="suspended"):
        await store.renew_execution_grant(
            other, expected_revision=2, expires_at=deadline(120), renewal_id="paused"
        )


@pytest.mark.parametrize("harness", ["memory", "sqlite"], indirect=True)
async def test_monotonic_expiry_survives_wall_clock_rollback(harness, monkeypatch):
    import ksadk.kernel.execution_grants as grants

    store = harness.store
    expires = deadline()
    await store.ensure_execution_grant(TIMED_SPEC, expires_at=expires)
    receipt = await store.accept_command(timed_command("one"), queue_limit=10)
    lease = await harness.lease()
    store._grant_deadlines[TIMED_SPEC.grant_id] = (expires, time.monotonic() - 1)
    monkeypatch.setattr(grants, "grant_now", lambda: deadline(-3600))
    with pytest.raises(ExecutionGrantBlocked, match="expired"):
        await store.claim_message(receipt.message_id, lease.fencing_token)
    with pytest.raises(ExecutionGrantBlocked, match="expired"):
        await store.require_execution_grant(TIMED_SPEC)
    with pytest.raises(ExecutionGrantBlocked, match="expired"):
        await store.renew_execution_grant(
            TIMED_SPEC, expected_revision=1, expires_at=deadline(120), renewal_id="late"
        )


@pytest.mark.parametrize("harness", ["sqlite"], indirect=True)
async def test_restart_and_old_renewal_replay_cannot_reanchor_local_expiry(harness):
    expires, renewed = deadline(), deadline(120)
    await harness.store.ensure_execution_grant(TIMED_SPEC, expires_at=expires)
    receipt = await harness.store.renew_execution_grant(
        TIMED_SPEC, expected_revision=1, expires_at=renewed, renewal_id="lost-ack"
    )
    accepted = await harness.store.accept_command(timed_command("one"), queue_limit=10)
    await harness.reopen()
    store = harness.store
    # Neither idempotent ensure nor an old renewal ACK constitutes fresh permission.
    assert (await store.ensure_execution_grant(TIMED_SPEC)).expires_at == renewed
    assert (
        await store.lookup_execution_grant_operation(TIMED_SPEC, idempotency_key="lost-ack")
        == receipt
    )
    assert (
        await store.renew_execution_grant(
            TIMED_SPEC, expected_revision=1, expires_at=renewed, renewal_id="lost-ack"
        )
        == receipt
    )
    lease = await harness.lease()
    for operation in (
        store.require_execution_grant(TIMED_SPEC),
        store.claim_message(accepted.message_id, lease.fencing_token),
    ):
        with pytest.raises(ExecutionGrantBlocked, match="clock_unverified"):
            await operation
    await store.renew_execution_grant(
        TIMED_SPEC,
        expected_revision=2,
        expires_at=deadline(180),
        renewal_id="fresh-authorized-renewal",
    )
    assert (await store.require_execution_grant(TIMED_SPEC)).revision == 3
    assert (
        await store.claim_message(accepted.message_id, lease.fencing_token)
    ).status == InboxState.CLAIMED


@pytest.mark.parametrize("harness", ["sqlite"], indirect=True)
async def test_renewal_failure_rolls_back_expiry_and_receipt(harness, monkeypatch):
    store = harness.store
    initial = await store.ensure_execution_grant(TIMED_SPEC, expires_at=deadline())
    original = store._sqlite_grant_barrier

    async def crash(*args):
        raise RuntimeError("before commit")

    monkeypatch.setattr(store, "_sqlite_grant_barrier", crash)
    with pytest.raises(RuntimeError, match="before commit"):
        await store.renew_execution_grant(
            TIMED_SPEC, expected_revision=1, expires_at=deadline(120), renewal_id="retry"
        )
    monkeypatch.setattr(store, "_sqlite_grant_barrier", original)
    assert (await store.get_execution_grant(TIMED_SPEC)).grant == initial
    assert await store.lookup_execution_grant_operation(TIMED_SPEC, idempotency_key="retry") is None
    assert (await store.require_execution_grant(TIMED_SPEC)).revision == 1


def test_expiring_grant_requires_epoch_and_absolute_time():
    from pydantic import ValidationError

    for spec, expires in (
        (SPEC, deadline()),
        (TIMED_SPEC, None),
        (TIMED_SPEC, "2026-09-18T12:00:00"),
    ):
        with pytest.raises(ValidationError):
            ExecutionGrantRecord(
                **spec.model_dump(), expires_at=expires, created_at=now_iso(), updated_at=now_iso()
            )
    for epoch in (True, 0, "7"):
        with pytest.raises(ValidationError):
            timed_command("invalid", epoch)


@pytest.mark.parametrize("harness", ["sqlite"], indirect=True)
async def test_sqlite_upgrade_preserves_legacy_record_and_operation_digest(harness):
    from ksadk.kernel.execution_grants import ExecutionGrantBarrier

    store = harness.store
    await store.close()
    # Recreate the exact old table/receipt shape, not the new schema minus values.
    old_record = ExecutionGrantRecord(
        **SPEC.model_dump(), created_at=now_iso(), updated_at=now_iso()
    )
    old_grant = old_record.model_dump(
        exclude={"expires_at", "attempt_epoch", "admission_allowed", "admission_revision"}
    )
    old_receipt = ExecutionGrantBarrier(grant=old_record).model_dump()
    old_receipt["grant"] = old_grant
    original_spec = SPEC.model_dump(exclude={"attempt_epoch"})
    old_digest = hashlib.sha256(
        json.dumps(
            {"spec": original_spec, "state": "active", "expected_revision": 1},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    with sqlite3.connect(harness.path) as connection:
        connection.execute("DROP TABLE kernel_execution_grants")
        connection.execute("""CREATE TABLE kernel_execution_grants (
          grant_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, agent_instance_id TEXT NOT NULL,
          session_id TEXT NOT NULL, owner_ref TEXT NOT NULL, state TEXT NOT NULL,
          revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        connection.execute(
            "INSERT INTO kernel_execution_grants VALUES (?,?,?,?,?,?,?,?,?)",
            tuple(old_grant.values()),
        )
        connection.execute(
            "INSERT INTO kernel_execution_grant_operations VALUES (?,?,?,?)",
            (SPEC.grant_id, "old-key", old_digest, json.dumps(old_receipt)),
        )
    await harness.reopen()
    store = harness.store
    assert (await store.ensure_execution_grant(SPEC)).expires_at is None
    assert await store.set_execution_grant_state(
        SPEC, "active", expected_revision=1, idempotency_key="old-key"
    ) == ExecutionGrantBarrier(grant=old_record)
    assert (await store.require_execution_grant(SPEC)).revision == 1
    await store.ensure_schema()  # migration is repeatable


async def test_facade_expiry_renewal_lookup_and_tool_guard(harness):
    from ksadk.kernel.control import AgentKernel
    from ksadk.kernel.ingress import InProcessPermitIssuer

    kernel = AgentKernel(harness.store, harness.events, InProcessPermitIssuer().verifier())
    await kernel.ensure_execution_grant(TIMED_SPEC, expires_at=deadline())
    receipt = await kernel.renew_execution_grant(
        TIMED_SPEC, expected_revision=1, expires_at=deadline(120), renewal_id="renew"
    )
    assert (
        await kernel.lookup_execution_grant_operation(TIMED_SPEC, idempotency_key="renew")
        == receipt
    )
    assert await kernel.require_execution_grant(TIMED_SPEC) == receipt.grant


@pytest.mark.parametrize("harness", ["memory", "sqlite"], indirect=True)
@pytest.mark.parametrize("clock_skew", [-3600, 3600])
async def test_trusted_remaining_ttl_never_extends_expiry_on_clock_skew(
    harness,
    monkeypatch,
    clock_skew,
):
    import ksadk.kernel.execution_grants as grants

    expires = deadline(60)
    clock = [100.0]
    local_now = deadline(clock_skew)
    monkeypatch.setattr(grants, "grant_now", lambda: local_now)
    monkeypatch.setattr(grants, "grant_monotonic", lambda: clock[0])
    store = harness.store
    await store.ensure_execution_grant(TIMED_SPEC, expires_at=expires, remaining_ttl_seconds=5)
    assert store._grant_deadlines[TIMED_SPEC.grant_id][1] <= 105
    if clock_skew > 0:
        with pytest.raises(ExecutionGrantBlocked, match="expired"):
            await store.require_execution_grant(TIMED_SPEC)
    else:
        await store.require_execution_grant(TIMED_SPEC)
        clock[0] = 105
        with pytest.raises(ExecutionGrantBlocked, match="expired"):
            await store.require_execution_grant(TIMED_SPEC)


@pytest.mark.parametrize("harness", ["memory", "sqlite"], indirect=True)
async def test_remaining_ttl_counts_transaction_wait_and_does_not_reanchor_replay(
    harness, monkeypatch
):
    import ksadk.kernel.execution_grants as grants

    store, clock = harness.store, [100.0]
    monkeypatch.setattr(grants, "grant_monotonic", lambda: clock[0])
    lock = (
        store._lock(TIMED_SPEC.agent_instance_id, TIMED_SPEC.session_id)
        if harness.backend == "memory"
        else store._write_lock
    )
    async with lock:
        delayed = asyncio.create_task(
            store.ensure_execution_grant(
                TIMED_SPEC,
                expires_at=deadline(60),
                remaining_ttl_seconds=2,
            )
        )
        await asyncio.sleep(0)
        assert not delayed.done()
        clock[0] = 103
    await delayed
    with pytest.raises(ExecutionGrantBlocked, match="expired"):
        await store.require_execution_grant(TIMED_SPEC)

    second = TIMED_SPEC.model_copy(update={"grant_id": "second"})
    await store.ensure_execution_grant(second, expires_at=deadline(60), remaining_ttl_seconds=30)
    renewed = deadline(120)
    receipt = await store.renew_execution_grant(
        second,
        expected_revision=1,
        expires_at=renewed,
        renewal_id="renew",
        remaining_ttl_seconds=5,
    )
    anchor = store._grant_deadlines[second.grant_id]
    clock[0] += 6
    assert (
        await store.renew_execution_grant(
            second,
            expected_revision=1,
            expires_at=renewed,
            renewal_id="renew",
            remaining_ttl_seconds=30,
        )
        == receipt
    )
    assert store._grant_deadlines[second.grant_id] == anchor
    with pytest.raises(ExecutionGrantBlocked, match="expired"):
        await store.require_execution_grant(second)


@pytest.mark.parametrize("harness", ["postgres"], indirect=True)
@pytest.mark.skipif(not os.environ.get("KSADK_TEST_GRANTS_POSTGRES_DSN"), reason="live PG opt-in")
async def test_postgres_uses_database_time_and_checks_after_grant_row_lock(harness, monkeypatch):
    import ksadk.kernel.execution_grants as grants

    store = harness.store
    await store.ensure_execution_grant(TIMED_SPEC, expires_at=deadline(60))
    # A bad host UTC clock cannot expire or extend a database-owned grant.
    monkeypatch.setattr(grants, "grant_now", lambda: deadline(3600))
    await store.require_execution_grant(TIMED_SPEC)
    receipt = await store.accept_command(timed_command("one"), queue_limit=10)
    assert receipt.status == "accepted"
    lease = await harness.lease()
    async with store._connection() as connection:
        async with connection.transaction():
            await connection.execute(
                "UPDATE kernel_execution_grants SET expires_at=$1 WHERE grant_id=$2",
                deadline(0),
                TIMED_SPEC.grant_id,
            )
            claiming = asyncio.create_task(
                store.claim_message(receipt.message_id, lease.fencing_token)
            )
            await asyncio.sleep(0.02)
            assert not claiming.done()
    with pytest.raises(ExecutionGrantBlocked, match="expired"):
        await claiming
    assert (await store.load_message(receipt.message_id)).status == InboxState.ACCEPTED


def test_remaining_ttl_rejects_nonfinite_or_invalid_values():
    from ksadk.kernel.execution_grants import grant_deadline_budget

    for value in (float("inf"), float("nan"), -1, True, "5"):
        with pytest.raises(InvalidCommandError):
            grant_deadline_budget(value)
