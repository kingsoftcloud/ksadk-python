# -*- coding: utf-8 -*-
"""AgentKernelStore 跨后端 conformance 契约（Phase 1 Task 3）。

任何 AgentKernelStore 实现（InMemory / SQLite / PostgreSQL）都必须通过这里
全部行为断言：FIFO、幂等 duplicate、typed queue_full、lease 过期 reclaim、
Run 终态 first-wins、fence CAS 与并发不丢失不重复。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from ksadk.kernel.contracts import (
    ActivationLease,
    AgentControlCommand,
    AgentControlReceipt,
    ControlError,
    ControlSource,
    SessionEventEnvelope,
)
from ksadk.kernel.errors import AgentKernelError, QueueFullError, StaleFenceError
from ksadk.kernel.state import InboxState, RunState

TENANT = "tenant-1"
AGENT = "agent-1"
SESSION = "s1"


def now_iso(offset_seconds: float = 0.0) -> str:
    moment = datetime.now(UTC) + timedelta(seconds=offset_seconds)
    return moment.isoformat()


def command(
    idempotency_key: str,
    content: str,
    *,
    session_id: str = SESSION,
    command_type: str = "enqueue",
) -> AgentControlCommand:
    payload = {"content": {"text": content}}
    if command_type == "inject":
        payload = {"context": {"text": content}}
    return AgentControlCommand(
        command_id=uuid.uuid4(),
        idempotency_key=idempotency_key,
        tenant_id=TENANT,
        agent_instance_id=AGENT,
        session_id=session_id,
        command_type=command_type,
        payload=payload,
        source=ControlSource(kind="studio", ref="local-studio"),
        authorization_ref="permit-1",
        submitted_at=now_iso(),
    )


def lease_request(activation_id: str, *, session_id: str = SESSION, ttl: float = 30.0):
    from ksadk.kernel.store import ActivationLeaseRequest

    return ActivationLeaseRequest(
        agent_instance_id=AGENT,
        session_id=session_id,
        activation_id=activation_id,
        runtime_type="ksadk",
        bundle_digest="digest-1",
        capability_digest="cap-1",
        lease_ttl_seconds=ttl,
    )


def make_run(run_id: str = "run-1", state: RunState = RunState.PENDING):
    from ksadk.kernel.store import RunRecord

    return RunRecord(
        run_id=run_id,
        agent_instance_id=AGENT,
        session_id=SESSION,
        state=state,
    )


def seed_session(session_service, session_id: str = SESSION):
    """Session backend 要求 session 先存在；conformance 统一 seed。"""

    async def _seed():
        existing = await session_service.get_session(session_id)
        if existing is None:
            await session_service.create_session(
                agent_id=AGENT, user_id="kernel-user", session_id=session_id
            )

    return _seed


# ---------------------------------------------------------------- conformance


async def assert_fifo_and_idempotency(store):
    r1 = await store.accept_command(command("k1", "first"), queue_limit=2)
    r1_retry = await store.accept_command(command("k1", "first"), queue_limit=2)
    r2 = await store.accept_command(command("k2", "second"), queue_limit=2)
    assert r1.status == "accepted"
    assert r1_retry.status == "duplicate"
    assert r1_retry.message_id == r1.message_id
    assert r1_retry.accepted_seq == r1.accepted_seq
    assert r2.accepted_seq is not None and r2.accepted_seq > r1.accepted_seq

    lease = await store.acquire_activation(lease_request("act-1"))
    assert lease.fencing_token >= 1
    first = await store.claim_next(AGENT, SESSION, lease.fencing_token)
    second = await store.claim_next(AGENT, SESSION, lease.fencing_token)
    assert first is not None and second is not None
    assert str(first.message_id) == str(r1.message_id)
    assert str(second.message_id) == str(r2.message_id)
    assert first.status == InboxState.CLAIMED
    await store.complete_claim(first.message_id, expected_fence=lease.fencing_token)
    reloaded = await store.load_message(first.message_id)
    assert reloaded is not None
    assert reloaded.status == InboxState.COMPLETED


async def assert_duplicate_with_different_digest_is_rejected(store):
    accepted = await store.accept_command(command("k1", "first"), queue_limit=4)
    assert accepted.status == "accepted"
    conflicting = await store.accept_command(command("k1", "different"), queue_limit=4)
    assert conflicting.status == "rejected"
    assert conflicting.error is not None
    assert conflicting.error.code == "idempotency_conflict"


async def assert_queue_full_is_typed(store):
    await store.accept_command(command("k1", "a"), queue_limit=2)
    await store.accept_command(command("k2", "b"), queue_limit=2)
    overflow = await store.accept_command(command("k3", "c"), queue_limit=2)
    assert overflow.status == "queue_full"
    assert overflow.error is not None
    assert overflow.error.code == "queue_full"
    assert overflow.error.retryable is True


async def assert_expired_claim_reclaim_requires_higher_fence(store):
    r1 = await store.accept_command(command("k1", "first"), queue_limit=4)
    short = await store.acquire_activation(lease_request("act-short", ttl=0.05))
    claimed = await store.claim_next(AGENT, SESSION, short.fencing_token)
    assert claimed is not None and str(claimed.message_id) == str(r1.message_id)

    # 旧 owner 的 fence 在过期后不能再完成或认领。
    await asyncio.sleep(0.08)
    with pytest.raises(StaleFenceError):
        await store.complete_claim(r1.message_id, expected_fence=short.fencing_token)

    takeover = await store.acquire_activation(lease_request("act-new"))
    assert takeover.fencing_token == short.fencing_token + 1

    # 过期 claim 只能被更高 fence 的 owner reclaim：旧 fence 不能再 claim。
    with pytest.raises(StaleFenceError):
        await store.claim_next(AGENT, SESSION, short.fencing_token)

    reclaimed = await store.claim_next(AGENT, SESSION, takeover.fencing_token)
    assert reclaimed is not None
    assert str(reclaimed.message_id) == str(r1.message_id)
    await store.complete_claim(r1.message_id, expected_fence=takeover.fencing_token)


async def assert_run_terminal_first_wins(store):
    lease = await store.acquire_activation(lease_request("act-run"))
    run = make_run()
    created = await store.save_run_transition(run, expected_fence=lease.fencing_token)
    assert created.state == RunState.PENDING

    started = await store.save_run_transition(
        created.model_copy(update={"state": RunState.RUNNING}),
        expected_fence=lease.fencing_token,
    )
    assert started.state == RunState.RUNNING

    completed = await store.save_run_transition(
        started.model_copy(update={"state": RunState.COMPLETED}),
        expected_fence=lease.fencing_token,
    )
    assert completed.state == RunState.COMPLETED

    # 终态 first-wins：terminal 之后任何再 transition 都拒绝。
    with pytest.raises(AgentKernelError):
        await store.save_run_transition(
            completed.model_copy(update={"state": RunState.FAILED}),
            expected_fence=lease.fencing_token,
        )
    assert (await store.load_run("run-1")).state == RunState.COMPLETED


async def assert_single_active_run_per_session(store):
    lease = await store.acquire_activation(lease_request("act-single"))
    first = await store.save_run_transition(make_run("run-1"), expected_fence=lease.fencing_token)
    await store.save_run_transition(
        first.model_copy(update={"state": RunState.RUNNING}),
        expected_fence=lease.fencing_token,
    )
    with pytest.raises(AgentKernelError):
        await store.save_run_transition(
            make_run("run-2", state=RunState.RUNNING),
            expected_fence=lease.fencing_token,
        )


async def assert_stale_fence_rejected_on_event_append(store):
    lease = await store.acquire_activation(lease_request("act-events"))
    stale = lease.fencing_token - 1 if lease.fencing_token > 1 else 0
    envelope = SessionEventEnvelope(
        event_id=uuid.uuid4(),
        session_id=SESSION,
        seq=0,
        timestamp=now_iso(),
        family="runtime",
        family_version=2,
        event_type="run.started",
        payload={"schema_version": 2, "event_id": f"evt_{uuid.uuid4().hex}", "seq": 0,
                 "timestamp": time.time(), "run_id": "run-1", "run_seq": 1,
                 "scope_id": "scope-1", "source": {"framework": "ksadk"},
                 "status": "running"},
    )
    with pytest.raises(StaleFenceError):
        await store.append_event(envelope, expected_fence=stale)
    persisted = await store.append_event(envelope, expected_fence=lease.fencing_token)
    assert persisted.seq >= 1


async def assert_concurrent_accepts_have_no_loss_or_duplicate(store, count: int = 100):
    async def one(index: int):
        return await store.accept_command(
            command(f"k{index}", f"message-{index}"), queue_limit=count
        )

    receipts = await asyncio.gather(*(one(i) for i in range(count)))
    assert all(r.status == "accepted" for r in receipts)
    message_ids = {str(r.message_id) for r in receipts}
    assert len(message_ids) == count
    seqs = sorted(r.accepted_seq for r in receipts)
    assert len(set(seqs)) == count
    assert seqs == list(range(1, count + 1))

    lease = await store.acquire_activation(lease_request("act-drain", ttl=60.0))
    claimed = []
    while True:
        message = await store.claim_next(AGENT, SESSION, lease.fencing_token)
        if message is None:
            break
        claimed.append(message.message_id)
        await store.complete_claim(message.message_id, expected_fence=lease.fencing_token)
    assert len(claimed) == count
    assert set(claimed) == message_ids


CONFORMANCE_CHECKS = (
    assert_fifo_and_idempotency,
    assert_duplicate_with_different_digest_is_rejected,
    assert_queue_full_is_typed,
    assert_expired_claim_reclaim_requires_higher_fence,
    assert_run_terminal_first_wins,
    assert_single_active_run_per_session,
    assert_stale_fence_rejected_on_event_append,
)
