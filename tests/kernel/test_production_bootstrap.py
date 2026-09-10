# -*- coding: utf-8 -*-
"""Task 4 Step 2: 生产 composition root（``ksadk.kernel.bootstrap``）。

``build_agent_kernel_runtime(config)`` 组装真实 AgentKernel 栈：
Store / fenced SessionEvent store / permit verifier（durable nonce）/
AgentKernelWorker / LeaseHeartbeat / RecoveryCoordinator / readiness probe。

- hosted 模式缺 PG DSN、Server JWKS、issuer、RuntimeAdapter provider、
  contract digest 或 durable nonce store 时启动必须失败（fail loud）。
- ``close()`` 停止全部后台任务（worker loop / lease heartbeat）。

测试用注入的 fake PG provider（内存 store + session service）代替真实
PostgreSQL；装配与生命周期语义与真实 driver 完全一致。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ksadk.kernel.authorization import InMemoryNonceStore
from ksadk.kernel.bootstrap import (
    AgentKernelRuntimeConfig,
    LeaseHeartbeat,
    build_agent_kernel_runtime,
    clear_agent_kernel_runtime,
    get_agent_kernel_runtime,
)
from ksadk.kernel.contract_fingerprints import (
    AGENT_KERNEL_V1_AGGREGATE_DIGEST,
    runtime_capability_matrix_digest,
)
from ksadk.kernel.control import AgentKernel
from ksadk.kernel.recovery import RecoveryCoordinator
from ksadk.kernel.worker import AgentKernelWorker
from ksadk.server.composition import configure_runtime_app
from ksadk.server.factory import RuntimeAppConfig, create_runtime_app
from tests.kernel.control_harness import (
    AGENT,
    CLOCK_AT,
    FakeAdapter,
    command,
    default_matrix,
    kernel_stack,
)

FAKE_DSN = "postgres://kernel-test:user@fake-host/kernel"
CONTRACT_DIGEST = AGENT_KERNEL_V1_AGGREGATE_DIGEST
CAPABILITY_DIGEST = runtime_capability_matrix_digest(default_matrix())
BUNDLE_DIGEST = "b" * 64


def test_lease_heartbeat_derives_distinct_owner_ids_per_session():
    heartbeat = LeaseHeartbeat(
        object(),  # type: ignore[arg-type]
        agent_instance_id=AGENT,
        activation_id="pod-uid",
        runtime_type="test",
        bundle_digest=BUNDLE_DIGEST,
        capability_digest=CAPABILITY_DIGEST,
        lease_ttl_seconds=60,
    )
    first = heartbeat.activation_id_for_session("session-a")
    second = heartbeat.activation_id_for_session("session-b")
    assert first != second
    assert heartbeat.activation_id_for_session("session-a") == first
    assert first.startswith("pod-uid:s:")


def _runtime_config(stack, **overrides: Any) -> AgentKernelRuntimeConfig:
    """hosted 配置：全量注入 fake PG provider（内存栈）。"""

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
    return AgentKernelRuntimeConfig(**base)


async def _runtime(**overrides: Any):
    stack = await kernel_stack()
    config = _runtime_config(stack, **overrides)
    return stack, build_agent_kernel_runtime(config)


async def test_bootstrap_builds_full_runtime_stack():
    stack, runtime = await _runtime()
    try:
        assert isinstance(runtime.kernel, AgentKernel)
        assert isinstance(runtime.worker, AgentKernelWorker)
        assert isinstance(runtime.recovery, RecoveryCoordinator)
        assert runtime.lease_heartbeat is not None
        assert runtime.readiness is not None
    finally:
        await runtime.close()


async def test_bootstrap_starts_worker_and_lease_heartbeat():
    stack, runtime = await _runtime()
    try:
        await runtime.start()
        await stack.kernel.submit(
            command(idempotency_key="boot-1"), permit=stack.permit("enqueue")
        )
        for _ in range(500):
            if stack.adapter.calls.count(("start", "s1")) >= 1:
                break
            await asyncio.sleep(0.01)
        assert stack.adapter.calls.count(("start", "s1")) == 1
        # worker loop 运行中；lease 已由 heartbeat 持有。
        assert runtime.worker_running
        lease = await stack.store.current_lease(AGENT, "s1")
        assert lease is not None
        assert lease.lease_expires_at > CLOCK_AT.isoformat()
        assert lease.capability_digest == CAPABILITY_DIGEST
    finally:
        await runtime.close()


async def test_runtime_renews_lease_while_stream_outlives_inbox_claim():
    """A live stream must not lose its fence just because Inbox was acked."""
    from ksadk.events.canonical import RunStarted, SourceRef

    stack = await kernel_stack()
    release_stream = asyncio.Event()

    async def live_stream(handle):
        yield RunStarted(
            schema_version=2,
            event_id="live-stream-started",
            seq=0,
            timestamp=1.0,
            run_id=handle.run_id,
            scope_id=f"run:{handle.run_id}",
            source=SourceRef(framework="ksadk"),
            status="running",
        )
        await release_stream.wait()

    stack.adapter.stream = live_stream  # type: ignore[method-assign]
    runtime = build_agent_kernel_runtime(
        _runtime_config(
            stack,
            lease_ttl_seconds=0.08,
            poll_interval=0.01,
        )
    )
    try:
        await runtime.start()
        await stack.kernel.submit(
            command(idempotency_key="keep-live-lease"), permit=stack.permit("enqueue")
        )
        for _ in range(100):
            active = await stack.store.find_active_run(AGENT, "s1")
            if active is not None and runtime.worker.active_session_ids() == {"s1"}:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("worker never started the live stream")
        first = await stack.store.current_lease(AGENT, "s1")
        assert first is not None
        await asyncio.sleep(0.11)
        renewed = await stack.store.current_lease(AGENT, "s1")
        assert renewed is not None
        assert renewed.lease_expires_at > first.lease_expires_at
    finally:
        release_stream.set()
        await runtime.close()


async def test_bootstrap_readiness_reports_real_health():
    stack, runtime = await _runtime()
    try:
        await runtime.start()
        await stack.kernel.submit(
            command(idempotency_key="boot-2"), permit=stack.permit("enqueue")
        )
        for _ in range(500):
            if stack.adapter.calls.count(("start", "s1")) >= 1:
                break
            await asyncio.sleep(0.01)
        health = await runtime.readiness.check()
        assert health["ready"] is True
        assert health["store_ok"] is True  # 真实 store 查询成功
        assert health["worker_running"] is True
        assert health["lease_healthy"] is True
        assert health["contract_digest"] == CONTRACT_DIGEST
        assert health["capability_digest"] == CAPABILITY_DIGEST
        assert health["capabilities"]["schema_version"] == 1
        assert health["capabilities"]["cancel"]["supported"] is False
        assert health["bundle_digest"] == BUNDLE_DIGEST
        from ksadk.version import VERSION

        assert health["runtime_identity"]["ksadk_version"] == VERSION
    finally:
        await runtime.close()


async def test_readiness_stays_ready_across_idle_lease_ttl():
    """P0：run 完成 / 等待审批（无 pending inbox 工作）期间心跳必须持续续约。

    readiness 语义是 "runtime 存活且能服务"，不是 "正在忙"：lease TTL
    过后只要 runtime 仍持有 activation，health 必须仍然 ready。"""

    stack, runtime = await _runtime(lease_ttl_seconds=0.4, poll_interval=0.01)
    try:
        await runtime.start()
        await stack.kernel.submit(
            command(idempotency_key="idle-ttl-1"), permit=stack.permit("enqueue")
        )
        for _ in range(500):
            if stack.adapter.calls.count(("start", "s1")) >= 1:
                break
            await asyncio.sleep(0.01)
        assert stack.adapter.calls.count(("start", "s1")) == 1
        # 空闲超过 3×TTL：心跳续约必须把 lease 一直顶在 TTL 窗口内。
        await asyncio.sleep(1.2)
        assert runtime.heartbeat_sessions() == {"s1"}
        health = await runtime.readiness.check()
        assert health["lease_healthy"] is True
        assert health["ready"] is True
        lease = await stack.store.current_lease(AGENT, "s1")
        assert lease is not None
        assert lease.activation_id == runtime.lease_heartbeat.activation_id_for_session("s1")
    finally:
        await runtime.close()


async def test_readiness_flips_not_ready_when_lease_truly_lost():
    """lease 被其它 activation 接管（真正丢失）后必须 not-ready。"""

    stack, runtime = await _runtime(lease_ttl_seconds=0.4, poll_interval=0.01)
    try:
        await runtime.start()
        await stack.kernel.submit(
            command(idempotency_key="lost-lease-1"), permit=stack.permit("enqueue")
        )
        for _ in range(500):
            if stack.adapter.calls.count(("start", "s1")) >= 1:
                break
            await asyncio.sleep(0.01)
        health = await runtime.readiness.check()
        assert health["ready"] is True

        # 模拟本 pod 失联后的 takeover：release 旧 lease，另一 activation 接管。
        from ksadk.kernel.store import ActivationLeaseRequest

        held = await stack.store.current_lease(AGENT, "s1")
        assert held is not None
        await stack.store.release_activation(
            held.activation_id, expected_fence=held.fencing_token
        )
        await stack.store.acquire_activation(
            ActivationLeaseRequest(
                session_id="s1",
                agent_instance_id=AGENT,
                activation_id="kernel-pod-other",
                runtime_type="ksadk-agent-kernel",
                bundle_digest=BUNDLE_DIGEST,
                capability_digest=CAPABILITY_DIGEST,
                lease_ttl_seconds=60.0,
            )
        )
        # 等待至少一次心跳续约周期 + readiness 判定。
        await asyncio.sleep(0.3)
        health = await runtime.readiness.check()
        assert health["lease_healthy"] is False
        assert health["ready"] is False
    finally:
        await runtime.close()


async def test_typed_runtime_store_reads_without_session_service():
    """hosted PG fenced store 没有 session_service：typed 读取不能炸。

    冷恢复 (RecoveryCoordinator -> scan_open_runs -> RuntimeEventStore.list)
    之前对 ``_service is None`` 的 typed store 直接 AttributeError，导致
    takeover recovery 双路径失败、runtime degraded。"""

    from ksadk.events.canonical import RunProgress, SourceRef
    from ksadk.events.canonical_store import RuntimeEventStore
    from ksadk.kernel.contracts import ActivationWriteGuard

    class FakeTypedEventStore:
        """只有 envelope append/read，没有 session_service（fenced 形状）。"""

        def __init__(self) -> None:
            self.rows: list = []

        async def append(self, envelope, *, guard):
            seq = len(self.rows) + 1
            stored = envelope.model_copy(
                update={"seq": seq, "payload": {**envelope.payload, "seq": seq}}
            )
            self.rows.append(stored)
            return stored

        async def read(self, session_id, after_seq, limit):
            return [e for e in self.rows if int(e.seq) > int(after_seq)][:limit]

        def subscribe(self, session_id, after_seq, **kwargs):  # pragma: no cover
            raise NotImplementedError

    events = FakeTypedEventStore()
    runtime_store = RuntimeEventStore(events, session_id="s1")
    guard = ActivationWriteGuard(activation_id="act-1", fencing_token=1)
    progress = RunProgress(
        schema_version=2,
        event_id="evt-typed-1",
        seq=0,
        timestamp=1_000.0,
        run_id="run-typed-1",
        scope_id="run:run-typed-1",
        status="running",
        progress=0.5,
        message="typed read",
        source=SourceRef(framework="ksadk"),
    )
    written = await runtime_store.append(progress, guard=guard)
    assert written.seq >= 1
    listed = await runtime_store.list("s1")
    assert [e.event_id for e in listed] == ["evt-typed-1"]
    by_id = await runtime_store.event_by_id("s1", "evt-typed-1")
    assert by_id is not None and by_id.run_id == "run-typed-1"
    assert await runtime_store.event_by_id("s1", "missing") is None


async def test_heartbeat_renews_while_run_loop_blocked_in_slow_start():
    """续约不能被 worker 的慢 start 阻塞。

    run loop 串行处理 session；某个 session 的 adapter.start() 慢于 lease
    TTL 时（hosted pod 上 codex 握手可 >30s），其它已持有 lease 的 session
    若靠 run loop 内联续约会过期 -> stream guard StaleFence -> run 卡死。
    续约必须跑在独立后台任务上。"""

    stack = await kernel_stack()
    adapter = stack.adapter
    adapter.start_delay = 0.6  # > TTL 0.4
    config = _runtime_config(
        stack, lease_ttl_seconds=0.4, poll_interval=0.01
    )
    runtime = build_agent_kernel_runtime(config)
    try:
        await runtime.start()
        await stack.kernel.submit(
            command(idempotency_key="slow-start-1"), permit=stack.permit("enqueue")
        )
        for _ in range(500):
            if runtime.heartbeat_sessions():
                break
            await asyncio.sleep(0.01)
        assert runtime.heartbeat_sessions() == {"s1"}
        # 空闲 + run loop 可能被慢 start 阻塞，等待远超 TTL。
        await asyncio.sleep(1.5)
        health = await runtime.readiness.check()
        assert health["lease_healthy"] is True, health
        assert health["ready"] is True, health
        lease = await stack.store.current_lease(AGENT, "s1")
        assert lease is not None
    finally:
        await runtime.close()


async def test_bootstrap_close_stops_every_background_task():
    stack, runtime = await _runtime()
    await runtime.start()
    tasks = list(runtime.background_tasks)
    assert tasks
    await runtime.close()
    assert not runtime.worker_running
    for task in tasks:
        assert task.done(), f"background task {task} still running after close()"


@pytest.mark.parametrize(
    "missing",
    [
        "dsn",
        "jwks",
        "permit_issuer",
        "adapter_provider",
        "contract_digest",
        "capability_digest",
        "bundle_digest",
        "nonce_store",
        "activation_id",
        "agent_instance_id",
    ],
)
async def test_hosted_bootstrap_fails_closed_on_missing_dependencies(missing):
    overrides: dict[str, Any] = {missing: None}
    if missing == "dsn":
        overrides["dsn"] = ""
    if missing in {"capability_digest", "bundle_digest", "agent_instance_id"}:
        overrides[missing] = ""
    with pytest.raises(RuntimeError, match=missing):
        await _runtime(**overrides)


async def test_hosted_bootstrap_rejects_incompatible_contract_digest():
    with pytest.raises(RuntimeError, match="contract_digest_mismatch"):
        await _runtime(contract_digest="0" * 64)


async def test_hosted_bootstrap_rejects_adapter_capability_digest_mismatch():
    from tests.kernel.control_harness import default_matrix, native

    incompatible_matrix = default_matrix().model_copy(
        update={"cancel": native()}
    )
    stack = await kernel_stack(adapter=FakeAdapter(matrix=incompatible_matrix))
    config = _runtime_config(stack)
    with pytest.raises(RuntimeError, match="capability_digest_mismatch"):
        build_agent_kernel_runtime(config)


async def test_local_mode_bootstraps_without_server_authority():
    """local 模式（开发）允许进程内 authority，不要求 server JWKS/DSN。"""
    stack = await kernel_stack()
    config = AgentKernelRuntimeConfig(
        agent_instance_id=AGENT,
        authority_mode="local",
        driver="memory",
        adapter_provider=lambda: stack.adapter,
        contract_digest=CONTRACT_DIGEST,
        capability_digest=CAPABILITY_DIGEST,
        bundle_digest=BUNDLE_DIGEST,
        store=stack.store,
        session_events=stack.events,
        session_service=stack.session_service,
        clock=lambda: CLOCK_AT,
        poll_interval=0.01,
    )
    runtime = build_agent_kernel_runtime(config)
    try:
        await runtime.start()
        assert runtime.worker_running
    finally:
        await runtime.close()


async def test_hosted_ephemeral_memory_runtime_does_not_require_postgres():
    """A single-pod hosted Agent may opt into explicit ephemeral semantics."""
    stack = await kernel_stack()
    runtime = build_agent_kernel_runtime(
        _runtime_config(
            stack,
            driver="memory",
            dsn="",
            durability_tier="ephemeral",
        )
    )
    try:
        await runtime.start()
        health = await runtime.readiness.check()
        assert health["ready"] is True
        assert health["durability_tier"] == "ephemeral"
    finally:
        await runtime.close()


async def test_hosted_memory_runtime_requires_explicit_ephemeral_tier():
    stack = await kernel_stack()
    with pytest.raises(RuntimeError, match="durability_tier=ephemeral"):
        build_agent_kernel_runtime(
            _runtime_config(stack, driver="memory", dsn="")
        )


def test_runtime_app_lifespan_starts_and_stops_full_kernel_runtime(monkeypatch):
    """生产 lifespan 启动完整 Kernel，且 HTTP 与 worker 共享事件日志。"""

    from ksadk.kernel import ingress

    clear_agent_kernel_runtime()
    ingress.clear_agent_kernel()
    monkeypatch.setenv("AGENT_KERNEL_ENABLED", "1")
    monkeypatch.setenv("AGENT_KERNEL_AUTHORITY_MODE", "local")
    monkeypatch.setenv("AGENT_KERNEL_STORE_DRIVER", "memory")
    monkeypatch.setenv("AGENT_INSTANCE_ID", AGENT)
    monkeypatch.setenv("AGENT_KERNEL_CONTRACT_DIGEST", CONTRACT_DIGEST)
    monkeypatch.setenv("AGENT_KERNEL_CAPABILITY_DIGEST", CAPABILITY_DIGEST)
    monkeypatch.setenv("AGENT_BUNDLE_DIGEST", BUNDLE_DIGEST)
    monkeypatch.setenv("KSADK_SESSION_BACKEND", "memory")

    adapter = FakeAdapter()
    app = create_runtime_app(
        RuntimeAppConfig(kernel_adapter_provider=lambda: adapter),
        configure_runtime_app,
    )
    try:
        with TestClient(app):
            runtime = app.state.agent_kernel_runtime
            assert runtime is not None
            assert runtime.worker_running is True
            assert ingress.get_agent_kernel() is runtime.kernel
            assert get_agent_kernel_runtime() is runtime
            assert (
                app.state.runtime.resolve_session_service()
                is runtime.config.session_service
            )
    finally:
        # TestClient 会执行 lifespan teardown；即便 startup 失败也清除进程级
        # global，避免污染随后 ingress/worker 测试。
        clear_agent_kernel_runtime()
        ingress.clear_agent_kernel()

    assert get_agent_kernel_runtime() is None
    assert ingress.get_agent_kernel() is None
