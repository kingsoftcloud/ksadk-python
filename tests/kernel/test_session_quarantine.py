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

import pytest
import asyncpg

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


@pytest.mark.parametrize("failure_mode", ["report", "exception"])
async def test_failed_takeover_settles_only_its_own_session(failure_mode):
    from ksadk.kernel.recovery import RecoveryReport

    stack, runtime = await _build()
    first = await _seed_orphan_run(stack, "s1")
    second = await _seed_orphan_run(stack, "s2")
    lease, took_over = await runtime.lease_heartbeat.ensure_lease("s2")
    assert took_over and lease is not None

    async def failed_recovery(agent_instance_id, activation, **kwargs):
        assert kwargs["session_id"] == "s2"
        if failure_mode == "exception":
            raise ValueError("native checkpoint missing")
        return RecoveryReport(
            agent_instance_id=agent_instance_id, activation_id=activation.activation_id,
            run_id=second, outcome="failed", reason="attach_failed:ValueError",
        )

    runtime.recovery.recover = failed_recovery
    try:
        assert await runtime._recover_safely(lease, "s2") is None
        assert (await stack.store.load_run(second)).state is RunState.INTERRUPTED
        assert (await stack.store.load_run(first)).state is RunState.RUNNING
        events = await stack.events.read("s2", 0, 100)
        assert sum(event.event_type == "control.recovery_decided" for event in events) == 1
    finally:
        await runtime.close()


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
    """Keep the accepted Inbox through a read outage and consume it after recovery."""
    stack, runtime = await _build(sessions=("s1",), store_failure_degrade_threshold=2)
    await stack.kernel.submit(command(idempotency_key="read-outage", session_id="s1"),
                              permit=stack.permit("enqueue", session_id="s1"))
    original = stack.store.list_messages
    failures = 0

    async def broken(agent_instance_id, **kwargs):
        nonlocal failures
        failures += 1
        raise ConnectionError("store is down")

    stack.store.list_messages = broken
    try:
        await runtime.start()
        await _wait_until(lambda: failures >= 3)
        assert not runtime.degraded and runtime.worker_running
        assert not (await runtime.readiness.check())["ready"]
        assert not any(kind == "start" for kind, _ in stack.adapter.calls)
        assert [m.status.value for m in await original(AGENT)] == ["accepted"]
        stack.store.list_messages = original
        await _wait_until(lambda: any(kind == "start" for kind, _ in stack.adapter.calls))
        assert sum(kind == "start" for kind, _ in stack.adapter.calls) == 1
    finally:
        stack.store.list_messages = original
        await runtime.close()


@pytest.mark.parametrize('failure', [TimeoutError, asyncpg.QueryCanceledError, asyncpg.LockNotAvailableError])
async def test_transient_pre_start_read_retries_same_command_without_poisoning_session(monkeypatch, failure):
    stack, runtime = await _build(sessions=('s1',))
    original = runtime.worker._session_continuation_metadata
    attempts = 0

    async def flaky(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise failure('database command deadline exceeded')
        return await original(*args, **kwargs)

    monkeypatch.setattr(runtime.worker, '_session_continuation_metadata', flaky)
    await stack.kernel.submit(command(idempotency_key='read-timeout'),
                              permit=stack.permit('enqueue'))
    try:
        await runtime.start()
        await _wait_until(lambda: attempts >= 2)
        await _wait_until(lambda: ('start', 's1') in stack.adapter.calls)
        assert stack.adapter.calls.count(('start', 's1')) == 1
        assert not runtime.quarantined_sessions()
        messages = await stack.store.list_messages(AGENT)
        assert messages[0].status.value == 'completed'
    finally:
        await runtime.close()


async def test_backend_settlement_failure_is_retried_without_restart(monkeypatch):
    stack, runtime = await _build(sessions=('s1',))
    run_id = await _seed_orphan_run(stack, 's1')
    original = runtime.recovery.settle_interrupted
    failures = 0

    async def flaky(*args, **kwargs):
        nonlocal failures
        failures += 1
        if failures == 1:
            raise TimeoutError('temporary read timeout')
        return await original(*args, **kwargs)

    monkeypatch.setattr(runtime.recovery, 'settle_interrupted', flaky)
    # Exercise the same path as a failed background settlement, including a
    # pre-existing durable RUNNING record that must not be started twice.
    lease, _ = await runtime.lease_heartbeat.ensure_lease('s1')
    await runtime._quarantine_session('s1', TimeoutError('read deadline'))
    try:
        await runtime.start()
        await _wait_until(lambda: failures >= 2)
        await _wait_until(lambda: ('start', 's1') in stack.adapter.calls)
        assert (await stack.store.load_run(run_id)).state is RunState.INTERRUPTED
        assert stack.adapter.calls.count(('start', 's1')) == 1
        assert not runtime.quarantined_sessions()
    finally:
        await runtime.close()


async def test_quarantined_session_rejects_new_admission_after_authorization():
    stack, runtime = await _build(sessions=('s1',))
    runtime._quarantined.add('s1')
    try:
        receipt = await runtime.kernel.submit(command(idempotency_key='new'),
                                               permit=stack.permit('enqueue'))
        assert receipt.status == 'rejected'
        assert receipt.error.code == 'session_quarantined'
        assert not await stack.store.list_pending(AGENT)
    finally:
        await runtime.close()


async def test_uncertain_provider_start_is_settled_without_repeating_side_effect(monkeypatch):
    stack, runtime = await _build(sessions=('s1',))
    original = stack.adapter.start
    starts = []

    async def uncertain(request):
        starts.append(request.input)
        if len(starts) == 1:
            raise TimeoutError('provider start response lost')
        return await original(request)

    monkeypatch.setattr(stack.adapter, 'start', uncertain)
    for key in ('uncertain', 'next-turn'):
        await runtime.kernel.submit(command(idempotency_key=key, payload={'content': key}),
                                    permit=stack.permit('enqueue'))
    try:
        await runtime.start()
        await _wait_until(lambda: len(starts) == 2)
        async def acked():
            return all(m.status.value == 'completed' for m in await stack.store.list_messages(AGENT))
        async with asyncio.timeout(5):
            while not await acked():
                await asyncio.sleep(.01)
        assert starts == ['uncertain', 'next-turn']
        assert not runtime.quarantined_sessions()
    finally:
        await runtime.close()


async def test_retry_that_loses_fence_does_not_quarantine_or_restart_provider(monkeypatch):
    from ksadk.kernel.errors import StaleFenceError
    stack, runtime = await _build(sessions=('s1',))
    await runtime._quarantine_session('s1', TimeoutError('read deadline'))
    async def stale(*args, **kwargs):
        raise StaleFenceError('new owner already took over')
    monkeypatch.setattr(runtime.recovery, 'settle_interrupted', stale)
    try:
        assert not await runtime._run_session_once('s1')
        assert not runtime.quarantined_sessions()
        assert not runtime._session_retry_after
        assert not stack.adapter.calls
    finally:
        await runtime.close()
