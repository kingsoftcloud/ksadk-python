# -*- coding: utf-8 -*-
"""P0-1 粒度修正：单个坏 session 隔离（quarantine），不再全局 degraded。

真实预发故障形态：某个 session 的历史脏数据使 takeover recovery 的 replay
校验抛 ``StreamConformanceError`` → ``_recover_safely`` 失败 → 整个 run loop
degraded 退出 → 所有 session 的 inbox 无人消费。

期望行为：
- 单个 session 恢复失败 → 该 session 进 quarantined 集合（不再 claim
  inbox、不再尝试恢复、不写 canonical 事件），WARNING 日志如实上报，
  readiness 增加 ``quarantined_sessions``（additive）；其余 session 照常服务。
- 全局性故障才进程级 degraded：连续 N 个（默认 5）不同 session 恢复失败，
  或 store 操作抛连接级异常时。
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


async def _build(sessions: tuple[str, ...] = ("s1", "s2"), **overrides: Any):
    stack = await kernel_stack(sessions=sessions)
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


def _lease_request(session_id: str, *, activation_id: str):
    return ActivationLeaseRequest(
        agent_instance_id=AGENT,
        session_id=session_id,
        activation_id=activation_id,
        runtime_type="fake",
        bundle_digest=BUNDLE_DIGEST,
        capability_digest=CAPABILITY_DIGEST,
    )


async def _seed_orphan_run(stack, session_id: str) -> str:
    """预置一个 RUNNING 孤儿 run + 一条被它阻塞的 accepted enqueue 消息。"""

    pre = await stack.store.acquire_activation(
        _lease_request(session_id, activation_id=f"act-pre-{session_id}")
    )
    await stack.store.release_activation(
        pre.activation_id, expected_fence=pre.fencing_token
    )
    seed_lease = await stack.store.acquire_activation(
        _lease_request(session_id, activation_id=f"act-seed-{session_id}")
    )
    pending = RunRecord(
        run_id=f"orphan-run-{session_id}",
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
        command(idempotency_key=f"blocked-{session_id}", session_id=session_id),
        permit=stack.permit("enqueue", session_id=session_id),
    )
    return run.run_id


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


def _break_recovery_for(runtime, *session_ids: str):
    """让指定 session 的 recover/settle_interrupted 全部抛错。"""

    targets = {
        runtime.lease_heartbeat.activation_id_for_session(sid)
        for sid in session_ids
    }
    calls: list[str] = []

    async def _recover_boom(agent_instance_id, lease, **kwargs):
        calls.append(lease.activation_id)
        raise RuntimeError("replay exploded: run.started after interrupted")

    async def _settle_boom(agent_instance_id, lease, **kwargs):
        if lease.activation_id in targets:
            raise RuntimeError("settle also exploded")
        return None

    runtime.recovery.recover = _recover_boom  # type: ignore[method-assign]
    runtime.recovery.settle_interrupted = _settle_boom  # type: ignore[method-assign]
    return calls


async def test_bad_session_quarantined_and_others_still_served():
    """1 个坏 session + 2 个正常：正常照常消费，坏的被隔离并如实上报。"""

    stack, runtime = await _build(sessions=("s1", "s2", "s3"))
    run_id = await _seed_orphan_run(stack, "s1")
    for sid in ("s2", "s3"):
        await stack.kernel.submit(
            command(idempotency_key=f"ok-{sid}", session_id=sid),
            permit=stack.permit("enqueue", session_id=sid),
        )
    calls = _break_recovery_for(runtime, "s1")

    try:
        await runtime.start()
        # 正常 session 的 inbox 照常消费。
        await _wait_until(lambda: ("start", "s2") in stack.adapter.calls)
        await _wait_until(lambda: ("start", "s3") in stack.adapter.calls)
        # 坏 session 被隔离。
        await _wait_until(lambda: "s1" in runtime.quarantined_sessions())
        assert ("start", "s1") not in stack.adapter.calls
        # 不再反复尝试恢复坏 session。
        calls_before = len(calls)
        await asyncio.sleep(0.3)
        assert len(calls) == calls_before
        # inbox 消息保持 accepted（不被消费，可人工清理后恢复）。
        messages = await stack.store.list_messages(AGENT)
        s1_messages = [m for m in messages if m.session_id == "s1"]
        assert s1_messages and all(m.status.value == "accepted" for m in s1_messages)
        # 不写任何 canonical 事件（孤儿 run 保持 RUNNING，不被污染）。
        envelopes = await stack.events.read("s1", 0, 1000)
        assert not [
            e for e in envelopes if e.event_type == "control.recovery_decided"
        ]
        assert stack.store._runs[run_id].state is RunState.RUNNING
        # readiness 如实上报，其余维度健康。
        health = await runtime.readiness.check()
        assert health["quarantined_sessions"] == 1
        assert health["degraded"] is False
        assert health["ready"] is True
    finally:
        await runtime.close()


async def test_terminal_command_failure_is_logged_and_quarantined_without_spin(
    caplog,
):
    """坏命令只能执行一次；同一 activation 不得无间隔重试拖死进程。"""

    stack, runtime = await _build(sessions=("s1", "s2"))
    stack.adapter.start_error = RuntimeError("poison command")
    await stack.kernel.submit(
        command(idempotency_key="bad-command", session_id="s1"),
        permit=stack.permit("enqueue", session_id="s1"),
    )

    try:
        with caplog.at_level("WARNING"):
            await runtime.start()
            await _wait_until(lambda: "s1" in runtime.quarantined_sessions())
        attempts = [call for call in stack.adapter.calls if call == ("start", "s1")]
        assert len(attempts) == 1
        await asyncio.sleep(0.1)
        assert [call for call in stack.adapter.calls if call == ("start", "s1")] == attempts
        assert any(
            "quarantined after command execution failed" in record.message
            and "poison command" in record.message
            for record in caplog.records
        )
        health = await runtime.readiness.check()
        assert health["quarantined_session_ids"] == ["s1"]
        assert health["degraded"] is False
    finally:
        await runtime.close()


async def test_all_sessions_bad_degrades_process():
    """恢复失败扩散到阈值个不同 session（默认 5）→ 进程级 degraded。"""

    sessions = ("s1", "s2", "s3", "s4", "s5")
    stack, runtime = await _build(sessions=sessions)
    for sid in sessions:
        await _seed_orphan_run(stack, sid)
    _break_recovery_for(runtime, *sessions)

    try:
        await runtime.start()
        await _wait_until(lambda: runtime.degraded)
        assert not runtime.worker_running
        assert runtime.degraded
        # 不再消费任何 inbox。
        await asyncio.sleep(0.1)
        assert stack.adapter.calls == []
        health = await runtime.readiness.check()
        assert health["degraded"] is True
        assert health["ready"] is False
        assert health["quarantined_sessions"] >= 1
    finally:
        await runtime.close()


async def test_store_outage_during_recovery_degrades_process():
    """恢复失败且 store 已不可达（连接级故障）→ 进程级 degraded。"""

    stack, runtime = await _build(sessions=("s1",))
    await _seed_orphan_run(stack, "s1")

    original_recover = runtime.recovery.recover
    original_settle = runtime.recovery.settle_interrupted
    original_list = stack.store.list_messages

    async def _recover_boom(agent_instance_id, lease, **kwargs):
        raise RuntimeError("recover exploded")

    async def _settle_boom(agent_instance_id, lease, **kwargs):
        # 收口失败的同时 store 断连：区分不了 session 级故障，必须降级。
        stack.store.list_messages = _list_broken  # type: ignore[method-assign]
        raise RuntimeError("settle exploded")

    async def _list_broken(agent_instance_id, **kwargs):
        raise ConnectionError("store is down")

    runtime.recovery.recover = _recover_boom  # type: ignore[method-assign]
    runtime.recovery.settle_interrupted = _settle_boom  # type: ignore[method-assign]

    try:
        await runtime.start()
        await _wait_until(lambda: runtime.degraded)
        assert runtime.degraded
        assert not runtime.worker_running
    finally:
        runtime.recovery.recover = original_recover  # type: ignore[method-assign]
        runtime.recovery.settle_interrupted = original_settle  # type: ignore[method-assign]
        stack.store.list_messages = original_list  # type: ignore[method-assign]
        await runtime.close()


async def test_persistent_store_outage_degrades_run_loop():
    """store 持续不可达（连 pending sessions 都查不到）→ 进程级 degraded。"""

    stack, runtime = await _build(sessions=("s1",))

    async def _list_broken(agent_instance_id, **kwargs):
        raise ConnectionError("store is down")

    stack.store.list_messages = _list_broken  # type: ignore[method-assign]
    try:
        await runtime.start()
        await _wait_until(lambda: runtime.degraded, timeout=10.0)
        assert runtime.degraded
        assert not runtime.worker_running
    finally:
        await runtime.close()
