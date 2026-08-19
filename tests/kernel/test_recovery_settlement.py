# -*- coding: utf-8 -*-
"""P0-1 回归：takeover 后 recover() 抛错不得静默吞掉。

真实预发事故形态：接管后 RecoveryCoordinator.recover 抛错被
``_run_loop`` 静默吞掉，留下 RUNNING run 阻塞该 session 后续 Inbox。

期望行为：
- recover 抛错 → 必须持久化收口（durable ``run.interrupted`` transition +
  fenced ``control.recovery_decided`` 审计事实），后续 Inbox 消息可消费；
- 连收口都失败 → runtime loop 停止接管，运行态显式 degraded
  （不再静默消费后续 Inbox）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from ksadk.kernel.authorization import InMemoryNonceStore
from ksadk.kernel.bootstrap import (
    AgentKernelRuntimeConfig,
    build_agent_kernel_runtime,
)
from ksadk.kernel.state import RunState
from ksadk.kernel.store import ActivationLeaseRequest, RunRecord
from tests.kernel.control_harness import AGENT, CLOCK_AT, command, kernel_stack
from tests.kernel.test_production_bootstrap import (
    BUNDLE_DIGEST,
    CAPABILITY_DIGEST,
    CONTRACT_DIGEST,
    FAKE_DSN,
)


async def _build(**overrides: Any):
    stack = await kernel_stack()
    base = dict(
        agent_instance_id=AGENT,
        authority_mode="hosted",
        driver="postgres",
        dsn=FAKE_DSN,
        jwks=stack.jwks,
        permit_issuer="agentengine-server-test",
        adapter_provider=lambda: stack.adapter,
        contract_digest=CONTRACT_DIGEST,
        capability_digest=CAPABILITY_DIGEST,
        bundle_digest=BUNDLE_DIGEST,
        activation_id="kernel-pod-test",
        nonce_store=InMemoryNonceStore(),
        store=stack.store,
        session_events=stack.events,
        session_service=stack.session_service,
        clock=lambda: CLOCK_AT,
        poll_interval=0.01,
        lease_ttl_seconds=60.0,
    )
    base.update(overrides)
    return stack, build_agent_kernel_runtime(AgentKernelRuntimeConfig(**base))


async def _seed_orphan_run(stack, session_id: str = "s1") -> str:
    """预置一个 RUNNING 孤儿 run + 一条被它阻塞的 accepted enqueue 消息。"""

    # 先用别的 activation 拿一次 lease 再释放，使 runtime heartbeat
    # 下一次 acquire 的 fencing_token > 1（ensure_lease 判定 took_over）。
    pre = await stack.store.acquire_activation(
        _lease_request(session_id, activation_id="act-pre")
    )
    await stack.store.release_activation(
        pre.activation_id, expected_fence=pre.fencing_token
    )
    seed_lease = await stack.store.acquire_activation(
        _lease_request(session_id, activation_id="act-seed")
    )
    pending = RunRecord(
        run_id="orphan-run-1",
        agent_instance_id=AGENT,
        session_id=session_id,
        state=RunState.PENDING,
        activation_fence=seed_lease.fencing_token,
    )
    await stack.store.save_run_transition(
        pending, expected_fence=seed_lease.fencing_token
    )
    run = pending.model_copy(update={"state": RunState.RUNNING})
    await stack.store.save_run_transition(
        run, expected_fence=seed_lease.fencing_token
    )
    await stack.store.release_activation(
        seed_lease.activation_id, expected_fence=seed_lease.fencing_token
    )
    await stack.kernel.submit(
        command(idempotency_key="blocked-1"), permit=stack.permit("enqueue")
    )
    return run.run_id


def _lease_request(session_id: str, *, activation_id: str):
    return ActivationLeaseRequest(
        agent_instance_id=AGENT,
        session_id=session_id,
        activation_id=activation_id,
        runtime_type="fake",
        bundle_digest=BUNDLE_DIGEST,
        capability_digest=CAPABILITY_DIGEST,
    )


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def _recovery_facts(stack, run_id: str) -> list[dict]:
    envelopes = await stack.events.read("s1", 0, 1000)
    return [
        env.payload
        for env in envelopes
        if env.event_type == "control.recovery_decided" and env.run_id == run_id
    ]


async def test_takeover_recover_failure_settles_interrupted_and_unblocks_inbox():
    stack, runtime = await _build()

    run_id = await _seed_orphan_run(stack)

    async def _boom(agent_instance_id, lease, **kwargs):
        raise RuntimeError("recover exploded")

    runtime.recovery.recover = _boom  # type: ignore[method-assign]
    try:
        await runtime.start()
        # 孤儿 run 被 durable 收口为 interrupted。
        await _wait_until(
            lambda: stack.store._runs.get(run_id) is not None
            and stack.store._runs[run_id].state == RunState.INTERRUPTED
        )
        run = stack.store._runs[run_id]
        assert run.state is RunState.INTERRUPTED
        # fenced recovery_decided 审计事实（当前 fencing token）。
        lease = await stack.store.current_lease(AGENT, "s1")
        assert lease is not None
        facts = await _recovery_facts(stack, run_id)
        assert facts, "expected a durable control.recovery_decided fact"
        fact = facts[-1]
        assert fact["outcome"] == "interrupted"
        assert fact["fencing_token"] == lease.fencing_token
        # 后续 Inbox 消息可正常消费（不再被孤儿 run 阻塞）。
        await _wait_until(lambda: stack.adapter.calls.count(("start", "s1")) >= 1)
    finally:
        await runtime.close()


async def test_takeover_settlement_failure_degrades_runtime():
    stack, runtime = await _build()

    run_id = await _seed_orphan_run(stack)

    async def _boom(agent_instance_id, lease, **kwargs):
        raise RuntimeError("recover exploded")

    async def _settle_boom(agent_instance_id, lease, **kwargs):
        raise RuntimeError("settle also exploded")

    runtime.recovery.recover = _boom  # type: ignore[method-assign]
    runtime.recovery.settle_interrupted = _settle_boom  # type: ignore[method-assign]
    try:
        await runtime.start()
        await _wait_until(lambda: runtime.degraded)
        # loop 停止接管：worker 不再运行，也不再消费 Inbox。
        assert not runtime.worker_running
        assert runtime.degraded
        await asyncio.sleep(0.1)
        assert stack.adapter.calls.count(("start", "s1")) == 0
        assert stack.store._runs[run_id].state is RunState.RUNNING
        readiness = await runtime.readiness.check()
        assert readiness["degraded"] is True
        assert readiness["ready"] is False
    finally:
        await runtime.close()
