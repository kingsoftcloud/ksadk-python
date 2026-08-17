# -*- coding: utf-8 -*-
"""Agent Kernel Phase 1 本地 closed-loop 验证（Task 13 本地等价物）。

预发 E2E（``--preprod``）的六项核心断言在本地用 InMemory / SQLite store +
真实 kernel / worker / recovery 组件先跑通同一套断言；真实预发执行时换成
HTTP 驱动的同语义用例（见 test_agent_kernel_preprod_e2e.py）。

覆盖验收矩阵行：FIFO / Backpressure / Persist-before-ack（幂等）/
SSE reconnect / Cold non-attach / Split brain。
"""

from __future__ import annotations

import pytest

from ksadk.events.session_event import SessionServiceEventStore
from ksadk.kernel.authorization import AgentControlPermitVerifier
from ksadk.kernel.contracts import (
    ActivationLease,
    SessionEventSubscription,
)
from ksadk.kernel.control import AgentKernel
from ksadk.kernel.errors import StaleFenceError
from ksadk.kernel.memory_store import InMemoryAgentKernelStore
from ksadk.kernel.recovery import RecoveryCoordinator
from ksadk.kernel.sqlite_store import SQLiteAgentKernelStore
from ksadk.kernel.state import InboxState, RunState
from ksadk.kernel.store import ActivationLeaseRequest, RunRecord, control_event
from ksadk.kernel.worker import AgentKernelWorker
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.sessions.local_service import LocalSessionService
from tests.kernel.control_harness import (
    AGENT,
    CLOCK_AT,
    TENANT,
    FakeAdapter,
    PermitAuthority,
    command,
)

SESSIONS = ("s1", "s2")


class ClosureStack:
    """InMemory / SQLite 共用的 kernel 组装。"""

    def __init__(
        self,
        store,
        events,
        authority: PermitAuthority,
        adapter: FakeAdapter,
        *,
        queue_limit: int = 100,
    ):
        self.store = store
        self.events = events
        self.authority = authority
        self.adapter = adapter
        self.jwks = authority.jwks()
        self.verifier = AgentControlPermitVerifier(self.jwks)
        self.kernel = AgentKernel(
            store,
            events,
            self.verifier,
            queue_limit=queue_limit,
            capabilities=adapter.capabilities,
            clock=lambda: CLOCK_AT,
        )

    def permit(self, *operations: str, **kwargs):
        ops = operations or ("enqueue",)
        return self.authority.permit(operations=ops, **kwargs)

    async def lease(self, session_id: str = "s1", activation_id: str = "act-1") -> ActivationLease:
        return await self.store.acquire_activation(
            ActivationLeaseRequest(
                agent_instance_id=AGENT,
                session_id=session_id,
                activation_id=activation_id,
                runtime_type="fake",
                bundle_digest="bundle-1",
                capability_digest="capdigest-1",
                lease_ttl_seconds=60.0,
            )
        )

    def worker(self) -> AgentKernelWorker:
        return AgentKernelWorker(self.store, adapter_factory=lambda: self.adapter)


async def _drain(stack: ClosureStack, lease: ActivationLease, limit: int = 120):
    worker = stack.worker()
    outcomes = []
    for _ in range(limit):
        result = await worker.run_once(AGENT, lease)
        outcomes.append(result.outcome)
        if result.outcome == "idle":
            break
    return outcomes, worker


async def _build_stack(tmp_path, driver: str, *, adapter=None, queue_limit=100):
    adapter = adapter or FakeAdapter()
    authority = PermitAuthority()
    if driver == "memory":
        service = InMemorySessionService()
        for session_id in SESSIONS:
            await service.create_session(
                agent_id=AGENT, user_id="kernel-user", session_id=session_id
            )
        events = SessionServiceEventStore(service)
        store = InMemoryAgentKernelStore(events)
    else:
        service = LocalSessionService(db_path=tmp_path / "sessions.sqlite")
        for session_id in SESSIONS:
            await service.create_session(
                agent_id=AGENT, user_id="kernel-user", session_id=session_id
            )
        events = SessionServiceEventStore(service)
        store = SQLiteAgentKernelStore(tmp_path / "kernel.sqlite", events)
        await store.ensure_schema()
    stack = ClosureStack(store, events, authority, adapter, queue_limit=queue_limit)
    return stack


# --------------------------------------------------------------- FIFO


async def test_closure_fifo_hundred_commands_in_accepted_seq_order(tmp_path):
    # FIFO 走 AgentKernelWorker（list_pending/claim_message 语义），当前
    # SQLite store 只交付 claim_next 子集，本地等价物用 InMemory 驱动。
    stack = await _build_stack(tmp_path, "memory")
    lease = await stack.lease()
    keys = []
    for index in range(100):
        key = f"fifo-{index:03d}"
        keys.append(key)
        receipt = await stack.kernel.submit(
            command(idempotency_key=key), permit=stack.permit("enqueue")
        )
        assert receipt.status == "accepted"
        assert receipt.accepted_seq == index + 1
    outcomes, _ = await _drain(stack, lease)
    assert outcomes == ["completed"] * 100 + ["idle"]
    assert stack.adapter.calls.count(("start", "s1")) == 100
    messages = await stack.store.list_messages(AGENT, "s1")
    by_seq = sorted(messages, key=lambda m: m.accepted_seq)
    assert [m.idempotency_key for m in by_seq] == keys
    assert all(m.status == InboxState.COMPLETED for m in messages)


# --------------------------------------------------------------- idempotency


async def test_closure_idempotent_retry_executes_only_once(tmp_path):
    stack = await _build_stack(tmp_path, "memory")
    lease = await stack.lease()
    cmd = command(idempotency_key="dup-1", content="hello")
    first = await stack.kernel.submit(cmd, permit=stack.permit("enqueue"))
    second = await stack.kernel.submit(cmd, permit=stack.permit("enqueue"))
    assert first.status == "accepted"
    assert second.status == "duplicate"
    assert second.message_id == first.message_id
    assert second.accepted_seq == first.accepted_seq
    # 同 key 换 payload：typed idempotency_conflict，不静默覆盖。
    conflict = await stack.kernel.submit(
        command(idempotency_key="dup-1", content="other"), permit=stack.permit("enqueue")
    )
    assert conflict.status == "rejected"
    assert conflict.error is not None and conflict.error.code == "idempotency_conflict"

    outcomes, _ = await _drain(stack, lease)
    assert outcomes == ["completed", "idle"]
    assert stack.adapter.calls.count(("start", "s1")) == 1
    messages = await stack.store.list_messages(AGENT, "s1")
    assert len(messages) == 1


# --------------------------------------------------------------- queue_full


@pytest.mark.parametrize("driver", ["memory", "sqlite"])
async def test_closure_backpressure_returns_typed_queue_full(tmp_path, driver):
    stack = await _build_stack(tmp_path, driver, queue_limit=3)
    for index in range(3):
        receipt = await stack.kernel.submit(
            command(idempotency_key=f"q-{index}"), permit=stack.permit("enqueue")
        )
        assert receipt.status == "accepted"
    overflow = await stack.kernel.submit(
        command(idempotency_key="q-overflow"), permit=stack.permit("enqueue")
    )
    assert overflow.status == "queue_full"
    assert overflow.error is not None
    assert overflow.error.code == "queue_full"
    assert overflow.error.retryable is True
    assert overflow.message_id is None
    # 旧消息不丢：3 条仍然 ACCEPTED（load_message 两种 driver 都支持）。
    statuses = []
    for index in range(3):
        dup = await stack.kernel.submit(
            command(idempotency_key=f"q-{index}"), permit=stack.permit("enqueue")
        )
        assert dup.status == "duplicate"
        message = await stack.store.load_message(str(dup.message_id))
        assert message is not None
        statuses.append(message.status)
    assert statuses == [InboxState.ACCEPTED] * 3


# --------------------------------------------------------------- reconnect


@pytest.mark.parametrize("driver", ["memory", "sqlite"])
async def test_closure_sse_reconnect_resumes_after_seq_without_gap(tmp_path, driver):
    stack = await _build_stack(tmp_path, driver)
    for index in range(5):
        receipt = await stack.kernel.submit(
            command(idempotency_key=f"r-{index}"), permit=stack.permit("enqueue")
        )
        assert receipt.status == "accepted"
    committed = await stack.events.read("s1", 0, 100)
    assert len(committed) == 5
    last_seen_seq = committed[1].seq  # “断链”前收到的最后 seq（=2）

    subscription = SessionEventSubscription(
        tenant_id=TENANT,
        agent_instance_id=AGENT,
        session_id="s1",
        authorization_ref="permit-ref",
        after_seq=last_seen_seq,
    )
    replayed = []
    async for envelope in stack.kernel.subscribe(
        subscription, permit=stack.permit("subscribe_events")
    ):
        replayed.append(envelope)
        if len(replayed) >= 3:  # 只取 replay 段，不跟随 live tail
            break
    seqs = [e.seq for e in replayed]
    assert seqs == [3, 4, 5]  # 从 last+1 开始，无 gap / duplicate
    event_ids = [str(e.event_id) for e in replayed]
    assert len(set(event_ids)) == len(event_ids)
    assert [str(e.event_id) for e in committed[2:]] == event_ids


# --------------------------------------------------------------- cold recovery


@pytest.mark.parametrize("driver", ["memory", "sqlite"])
async def test_closure_cold_recovery_deterministic_interrupt(tmp_path, driver):
    stack = await _build_stack(tmp_path, driver)
    old = await stack.lease(activation_id="act-old")
    run = await stack.store.save_run_transition(
        RunRecord(
            run_id="run-cold",
            agent_instance_id=AGENT,
            session_id="s1",
            state=RunState.PENDING,
        ),
        expected_fence=old.fencing_token,
    )
    await stack.store.save_run_transition(
        run.model_copy(update={"state": RunState.RUNNING}),
        expected_fence=old.fencing_token,
    )
    # Pod 消失：release（等价 lease 过期后 takeover）并让新 activation 接管。
    await stack.store.release_activation(
        old.activation_id, expected_fence=old.fencing_token
    )
    new = await stack.lease(activation_id="act-new")
    assert new.fencing_token > old.fencing_token

    coordinator = RecoveryCoordinator(
        stack.store, stack.events, stack.adapter.capabilities
    )
    report = await coordinator.recover(AGENT, new, run_id="run-cold")
    assert report.outcome == "interrupted"
    assert report.run_id == "run-cold"
    final_run = await stack.store.load_run("run-cold")
    assert final_run is not None
    assert final_run.state is RunState.INTERRUPTED
    # 唯一 terminal 语义：recovery 决策只落一次，run.interrupted 至多一条。
    events = await stack.events.read("s1", 0, 100)
    decided = [
        e for e in events if e.event_type == "control.recovery_decided"
    ]
    assert len(decided) == 1
    assert decided[0].payload["outcome"] == "interrupted"
    terminal = [e for e in events if e.event_type == "run.interrupted"]
    assert len(terminal) <= 1


# --------------------------------------------------------------- stale fence


@pytest.mark.parametrize("driver", ["memory", "sqlite"])
async def test_closure_stale_fence_rejects_old_writer(tmp_path, driver):
    stack = await _build_stack(tmp_path, driver)
    old = await stack.lease(activation_id="act-old")
    new_owner_run = RunRecord(
        run_id="run-fence",
        agent_instance_id=AGENT,
        session_id="s1",
        state=RunState.PENDING,
    )
    await stack.store.save_run_transition(
        new_owner_run, expected_fence=old.fencing_token
    )
    # split-brain：旧 lease 被释放，新 activation 以更高 fence takeover。
    await stack.store.release_activation(
        old.activation_id, expected_fence=old.fencing_token
    )
    new = await stack.lease(activation_id="act-new")
    assert new.fencing_token > old.fencing_token

    stale_writes = 0
    # 旧 writer 尝试写 terminal run transition：拒绝。
    with pytest.raises(StaleFenceError):
        await stack.store.save_run_transition(
            new_owner_run.model_copy(update={"state": RunState.COMPLETED}),
            expected_fence=old.fencing_token,
        )
    stale_writes += 1
    # 旧 writer 尝试写 canonical event：拒绝。
    with pytest.raises(StaleFenceError):
        await stack.store.append_event(
            control_event(
                session_id="s1",
                event_type="control.command_rejected",
                payload={"command_id": "c1"},
            ),
            expected_fence=old.fencing_token,
            agent_instance_id=AGENT,
        )
    stale_writes += 1
    assert stale_writes == 2  # stale_fence_total 等价计数

    # 新 owner 正常写：canonical log 只含新 owner 事实。
    await stack.store.save_run_transition(
        new_owner_run.model_copy(update={"state": RunState.RUNNING}),
        expected_fence=new.fencing_token,
    )
    await stack.store.append_event(
        control_event(
            session_id="s1",
            event_type="control.command_accepted",
            payload={"command_id": "new-owner"},
        ),
        expected_fence=new.fencing_token,
        agent_instance_id=AGENT,
    )
    final_run = await stack.store.load_run("run-fence")
    assert final_run.state is RunState.RUNNING
