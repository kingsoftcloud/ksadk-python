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

from ksadk.kernel.authorization import InMemoryNonceStore
from ksadk.kernel.bootstrap import (
    AgentKernelRuntimeConfig,
    build_agent_kernel_runtime,
)
from ksadk.kernel.control import AgentKernel
from ksadk.kernel.recovery import RecoveryCoordinator
from ksadk.kernel.worker import AgentKernelWorker
from tests.kernel.control_harness import (
    AGENT,
    CLOCK_AT,
    command,
    kernel_stack,
)

FAKE_DSN = "postgres://kernel-test:user@fake-host/kernel"
CONTRACT_DIGEST = "c" * 64
CAPABILITY_DIGEST = "p" * 64
BUNDLE_DIGEST = "b" * 64


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
    finally:
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
        assert health["bundle_digest"] == BUNDLE_DIGEST
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
        "nonce_store",
    ],
)
async def test_hosted_bootstrap_fails_closed_on_missing_dependencies(missing):
    overrides: dict[str, Any] = {missing: None}
    if missing == "dsn":
        overrides["dsn"] = ""
    with pytest.raises(RuntimeError, match=missing):
        await _runtime(**overrides)


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
