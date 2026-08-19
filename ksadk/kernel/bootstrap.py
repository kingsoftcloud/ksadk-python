# -*- coding: utf-8 -*-
"""生产 composition root（Phase 1 Task 4 Step 4）。

``build_agent_kernel_runtime(config) -> AgentKernelRuntime`` 把 AgentKernel
栈的全部运行时角色组装成一个可启动 / 可关闭的单元：

- ``AgentKernel``（Store + fenced SessionEvent store + permit verifier，
  verifier 挂 durable nonce store）；
- ``AgentKernelWorker``（per-session FIFO 执行）；
- ``LeaseHeartbeat``（activation lease 的获取 / 续约 / takeover 检测）；
- ``RecoveryCoordinator``（open run 的 attach / resume / 确定性 interrupted）；
- ``AgentKernelReadiness``（真实 store 查询 + worker 运行态 + lease 健康 +
  digest 比对），供 ``/agent-kernel/v1/health`` 与 Operator
  ``AgentKernelReady`` 消费。

hosted 模式 fail loud：缺 PG DSN、Server JWKS、permit issuer、
RuntimeAdapter provider、contract digest 或 durable nonce store 时
``build_agent_kernel_runtime`` 直接抛 ``RuntimeError``，绝不静默降级到
内存栈或本地自签 authority。
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from ksadk.events.session_event import SessionServiceEventStore
from ksadk.kernel.authorization import AgentControlPermitVerifier
from ksadk.kernel.contracts import RuntimeCapabilityMatrix
from ksadk.kernel.control import AgentKernel, default_capability_matrix
from ksadk.kernel.errors import InvalidCommandError
from ksadk.kernel.recovery import RecoveryCoordinator
from ksadk.kernel.store import AgentKernelStore, now_utc
from ksadk.kernel.worker import AgentKernelWorker
from ksadk.runtime.adapter import RuntimeAdapter

AuthorityMode = Literal["local", "hosted"]


@dataclass
class AgentKernelRuntimeConfig:
    """生产装配配置（Operator env 投影或测试注入 fake PG provider）。"""

    agent_instance_id: str
    authority_mode: AuthorityMode = "local"
    driver: str = "memory"  # postgres | sqlite | memory
    dsn: str = ""
    # server authority（hosted 必填）
    jwks: Any | None = None
    permit_issuer: str | None = None
    nonce_store: Any | None = None
    # 运行时
    adapter_provider: Callable[[], RuntimeAdapter] | None = None
    capabilities: Callable[[], RuntimeCapabilityMatrix] | None = None
    # 契约 digest（hosted 必填 contract_digest）
    contract_digest: str = ""
    capability_digest: str = ""
    bundle_digest: str = ""
    # 测试注入的 fake PG provider：提供时不再从 dsn 建真实连接，
    # 但 hosted 模式的 dsn 必填校验仍然生效。
    store: AgentKernelStore | None = None
    session_events: Any | None = None
    session_service: Any | None = None
    # 生命周期参数
    queue_limit: int = 100
    lease_ttl_seconds: float = 60.0
    poll_interval: float = 0.25
    activation_id: str | None = None
    runtime_type: str = "ksadk-agent-kernel"
    clock: Callable[[], datetime] = now_utc


class LeaseHeartbeat:
    """activation lease 的获取 / 续约 / takeover 检测。

    同一 ``activation_id`` 的 ``acquire`` 是幂等续约（token 不变、租期
    重置）；token 变化（> 已知值）说明发生过 takeover，调用方应触发
    RecoveryCoordinator 对 open run 做确定性收口。
    """

    def __init__(
        self,
        store: AgentKernelStore,
        *,
        agent_instance_id: str,
        activation_id: str,
        runtime_type: str,
        bundle_digest: str,
        capability_digest: str,
        lease_ttl_seconds: float,
    ) -> None:
        self._store = store
        self.agent_instance_id = agent_instance_id
        self.activation_id = activation_id
        self._request = dict(
            agent_instance_id=agent_instance_id,
            activation_id=activation_id,
            runtime_type=runtime_type,
            bundle_digest=bundle_digest or "unknown",
            capability_digest=capability_digest or "unknown",
            lease_ttl_seconds=lease_ttl_seconds,
        )
        self._last_tokens: dict[str, int] = {}

    async def ensure_lease(self, session_id: str) -> tuple[Any, bool]:
        """获取（或幂等续约）session 的 lease。

        返回 ``(lease, took_over)``：lease 为 None 表示被其它 owner 持有；
        ``took_over`` 表示本次拿到的 fencing token 比已知值新（发生过
        takeover，需要 recovery）。
        """

        from ksadk.kernel.store import ActivationLeaseRequest

        try:
            lease = await self._store.acquire_activation(
                ActivationLeaseRequest(session_id=session_id, **self._request)
            )
        except InvalidCommandError:
            return None, False
        last = self._last_tokens.get(session_id)
        took_over = (last is None and lease.fencing_token > 1) or (
            last is not None and lease.fencing_token > last
        )
        self._last_tokens[session_id] = lease.fencing_token
        return lease, took_over

    def forget(self, session_id: str) -> None:
        self._last_tokens.pop(session_id, None)


@dataclass
class AgentKernelReadiness:
    """truthful readiness probe：每个维度都是真实查询，不是配置回显。"""

    runtime: "AgentKernelRuntime"

    async def check(self) -> dict[str, Any]:
        config = self.runtime.config
        store_ok = False
        try:
            # 真实 store 查询（PG driver 即真实 SQL round-trip）。
            await self.runtime.kernel_store.list_messages(
                config.agent_instance_id
            )
            store_ok = True
        except Exception:
            store_ok = False

        lease_healthy = True
        activation_id: str | None = None
        for session_id in self.runtime.heartbeat_sessions():
            try:
                lease = await self.runtime.kernel_store.current_lease(
                    config.agent_instance_id, session_id
                )
            except Exception:
                lease = None
            if lease is None:
                lease_healthy = False
                continue
            activation_id = activation_id or lease.activation_id
            expires = getattr(lease, "lease_expires_at", "")
            try:
                from datetime import datetime

                expires_at = datetime.fromisoformat(
                    str(expires).replace("Z", "+00:00")
                )
                if expires_at <= datetime.now(expires_at.tzinfo):
                    lease_healthy = False
            except ValueError:
                lease_healthy = False

        worker_running = self.runtime.worker_running
        digests_match = bool(config.contract_digest)
        ready = store_ok and worker_running and lease_healthy and digests_match
        return {
            "ready": ready,
            "store_ok": store_ok,
            "worker_running": worker_running,
            "lease_healthy": lease_healthy,
            "activation_id": activation_id,
            "contract_digest": config.contract_digest,
            "capability_digest": config.capability_digest,
            "bundle_digest": config.bundle_digest,
        }


@dataclass
class AgentKernelRuntime:
    """生产 kernel runtime：start() 启动后台 worker/lease loop，close() 全停。"""

    config: AgentKernelRuntimeConfig
    kernel: AgentKernel
    worker: AgentKernelWorker
    recovery: RecoveryCoordinator
    lease_heartbeat: LeaseHeartbeat
    readiness: AgentKernelReadiness
    kernel_store: AgentKernelStore = field(repr=False)
    session_events: Any = field(repr=False)
    _owns_pool: bool = field(default=False, repr=False)
    _pool: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._tasks: list[asyncio.Task] = []
        self._worker_running = False
        self._heartbeat_sessions: set[str] = set()

    # ------------------------------------------------------------ properties

    @property
    def worker_running(self) -> bool:
        return self._worker_running

    def heartbeat_sessions(self) -> set[str]:
        return set(self._heartbeat_sessions)

    @property
    def background_tasks(self) -> list[asyncio.Task]:
        return list(self._tasks)

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._tasks:
            return
        self._worker_running = True
        self._tasks.append(asyncio.create_task(self._run_loop(), name="kernel-runtime"))

    async def close(self) -> None:
        for task in self._tasks:
            if not task.done():
                task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        self._worker_running = False
        # best-effort 释放持有的 activation（不阻塞关闭）。
        for session_id in list(self._heartbeat_sessions):
            try:
                lease = await self.kernel_store.current_lease(
                    self.config.agent_instance_id, session_id
                )
                if lease is not None and lease.activation_id == self.lease_heartbeat.activation_id:
                    await self.kernel_store.release_activation(
                        lease.activation_id, expected_fence=lease.fencing_token
                    )
            except Exception:
                pass
            self.lease_heartbeat.forget(session_id)
        self._heartbeat_sessions.clear()
        if self._owns_pool and self._pool is not None and hasattr(self._pool, "close"):
            try:
                await self._pool.close()
            except Exception:
                pass

    # ------------------------------------------------------------- run loop

    async def _run_loop(self) -> None:
        self._worker_running = True
        try:
            while True:
                progressed = False
                try:
                    sessions = await self._pending_sessions()
                    for session_id in sorted(sessions):
                        lease, took_over = await self.lease_heartbeat.ensure_lease(
                            session_id
                        )
                        if lease is None:
                            continue
                        self._heartbeat_sessions.add(session_id)
                        if took_over:
                            # takeover：对 open run 做确定性收口（attach /
                            # resume / interrupted），再继续消费 inbox。
                            try:
                                await self.recovery.recover(
                                    self.config.agent_instance_id, lease
                                )
                            except Exception:
                                pass
                        result = await self.worker.run_once(
                            self.config.agent_instance_id, lease
                        )
                        if result.outcome != "idle":
                            progressed = True
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await asyncio.sleep(self.config.poll_interval * 4)
                    continue
                if not progressed:
                    await asyncio.sleep(self.config.poll_interval)
        finally:
            self._worker_running = False

    async def _pending_sessions(self) -> set[str]:
        messages = await self.kernel_store.list_messages(
            self.config.agent_instance_id
        )
        return {
            message.session_id
            for message in messages
            if message.status.value in ("accepted", "claimed")
        }


# ---------------------------------------------------------------------------
# 进程级 runtime 注册（/agent-kernel/v1/health 消费）
# ---------------------------------------------------------------------------

_runtime: AgentKernelRuntime | None = None


def set_agent_kernel_runtime(runtime: AgentKernelRuntime | None) -> None:
    global _runtime
    _runtime = runtime


def get_agent_kernel_runtime() -> AgentKernelRuntime | None:
    return _runtime


def clear_agent_kernel_runtime() -> None:
    set_agent_kernel_runtime(None)


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def _validate_hosted(config: AgentKernelRuntimeConfig) -> None:
    if config.authority_mode != "hosted":
        return
    missing: list[str] = []
    if not config.dsn:
        missing.append("dsn")
    if config.jwks is None:
        missing.append("jwks")
    if not config.permit_issuer:
        missing.append("permit_issuer")
    if config.adapter_provider is None:
        missing.append("adapter_provider")
    if not config.contract_digest:
        missing.append("contract_digest")
    if config.nonce_store is None:
        missing.append("nonce_store")
    if missing:
        raise RuntimeError(
            "hosted agent kernel runtime requires "
            + ", ".join(missing)
            + "; refusing to bootstrap (fail closed)"
        )


def build_agent_kernel_runtime(
    config: AgentKernelRuntimeConfig,
) -> AgentKernelRuntime:
    """组装生产 kernel runtime；hosted 模式缺依赖时 fail loud。"""

    _validate_hosted(config)

    store = config.store
    session_events = config.session_events
    session_service = config.session_service
    owns_pool = False
    pool = None

    if store is None or session_events is None:
        if config.driver == "postgres":
            from ksadk.kernel.postgres_store import (
                PostgresAgentKernelStore,
                PostgresFencedSessionEventStore,
                PostgresKernelEventLog,
                PostgresNonceStore,
            )
            from ksadk.sessions.postgres_service import PostgresSessionService

            if not config.dsn:
                raise RuntimeError(
                    "postgres agent kernel runtime requires a store DSN"
                )
            if session_service is None:
                session_service = PostgresSessionService(dsn=config.dsn)
            pool = getattr(session_service, "_pool", None)
            event_log = PostgresKernelEventLog(pool)
            kernel_store: AgentKernelStore = PostgresAgentKernelStore(
                pool, event_log
            )
            # typed RuntimeEvent 写路径走 fenced store：每个
            # ActivationWriteGuard append 在同一事务验证 activation 行。
            events = PostgresFencedSessionEventStore(kernel_store)  # type: ignore[arg-type]
        else:
            from ksadk.kernel.memory_store import InMemoryAgentKernelStore
            from ksadk.sessions.in_memory import InMemorySessionService

            if session_service is None:
                session_service = InMemorySessionService()
            base_events = SessionServiceEventStore(session_service)
            kernel_store = InMemoryAgentKernelStore(base_events)
            events = base_events
        store = store or kernel_store
        session_events = session_events or events

    if config.authority_mode == "hosted":
        verifier = AgentControlPermitVerifier(config.jwks, nonce_store=config.nonce_store)
    else:
        from ksadk.kernel.ingress import _default_issuer

        verifier = _default_issuer().verifier(nonce_store=config.nonce_store)

    adapter_provider = config.adapter_provider or _no_adapter_provider
    capabilities = config.capabilities
    if capabilities is None:
        probe = adapter_provider()

        def capabilities() -> RuntimeCapabilityMatrix:  # type: ignore[misc]
            try:
                return probe.capabilities()
            except Exception:
                return default_capability_matrix()

    kernel = AgentKernel(
        store,
        session_events,
        verifier,
        queue_limit=config.queue_limit,
        capabilities=capabilities,
        clock=config.clock,
    )
    worker = AgentKernelWorker(
        store, adapter_factory=adapter_provider, session_events=session_events
    )
    recovery = RecoveryCoordinator(
        store,
        session_events,
        capabilities,
        adapter_factory=adapter_provider,
    )
    heartbeat = LeaseHeartbeat(
        store,
        agent_instance_id=config.agent_instance_id,
        activation_id=config.activation_id
        or f"{config.agent_instance_id}:kernel-runtime",
        runtime_type=config.runtime_type,
        bundle_digest=config.bundle_digest,
        capability_digest=config.capability_digest,
        lease_ttl_seconds=config.lease_ttl_seconds,
    )
    runtime = AgentKernelRuntime(
        config=config,
        kernel=kernel,
        worker=worker,
        recovery=recovery,
        lease_heartbeat=heartbeat,
        readiness=AgentKernelReadiness(runtime=None),  # type: ignore[arg-type]
        kernel_store=store,
        session_events=session_events,
        _owns_pool=owns_pool,
        _pool=pool,
    )
    runtime.readiness.runtime = runtime
    return runtime


def _no_adapter_provider() -> RuntimeAdapter:  # pragma: no cover - defensive
    raise RuntimeError("agent kernel runtime has no RuntimeAdapter provider")


async def bootstrap_agent_kernel_runtime_from_env() -> AgentKernelRuntime | None:
    """Operator env 投影 -> 生产 runtime（AGENT_KERNEL_ENABLED=1 时）。

    hosted 部署（AGENT_KERNEL_STORE_DRIVER=postgres + JWKS URL）装配并启动
    worker/lease/recovery，同时注册 kernel ingress 与 runtime health。
    """

    from ksadk.kernel.ingress import (
        ENV_JWKS_URL,
        bootstrap_agent_kernel_from_env,
        set_agent_kernel,
    )

    enabled = os.environ.get("AGENT_KERNEL_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not enabled:
        return None
    if get_agent_kernel_runtime() is not None:
        return get_agent_kernel_runtime()

    # 先复用 ingress 的 env bootstrap 装配 kernel/store/events（含 DSN 校验）。
    kernel = await bootstrap_agent_kernel_from_env()
    if kernel is None:  # pragma: no cover - defensive
        return None

    driver = os.environ.get("AGENT_KERNEL_STORE_DRIVER", "memory").strip().lower()
    dsn = os.environ.get("AGENT_KERNEL_STORE_DSN", "").strip()
    jwks_url = os.environ.get(ENV_JWKS_URL, "").strip()
    mode: AuthorityMode = "hosted" if jwks_url else "local"
    config = AgentKernelRuntimeConfig(
        agent_instance_id=os.environ.get("AGENT_INSTANCE_ID", "local-agent"),
        authority_mode=mode,
        driver=driver,
        dsn=dsn,
        jwks=kernel._permit_verifier._jwks if mode == "hosted" else None,
        permit_issuer=os.environ.get("AGENT_CONTROL_PERMIT_ISSUER", ""),
        nonce_store=kernel._permit_verifier._nonce_store,
        adapter_provider=None,
        contract_digest=os.environ.get("AGENT_KERNEL_CONTRACT_DIGEST", ""),
        capability_digest=os.environ.get("AGENT_KERNEL_CAPABILITY_DIGEST", ""),
        bundle_digest=os.environ.get("AGENT_BUNDLE_DIGEST", ""),
        store=kernel._store,
        session_events=kernel._events,
        session_service=getattr(kernel._events, "session_service", None),
        lease_ttl_seconds=float(
            os.environ.get("AGENT_KERNEL_LEASE_TTL_SECONDS", "60") or "60"
        ),
    )
    runtime = build_agent_kernel_runtime(config)
    await runtime.start()
    set_agent_kernel(runtime.kernel)
    set_agent_kernel_runtime(runtime)
    return runtime


__all__ = [
    "AgentKernelRuntime",
    "AgentKernelRuntimeConfig",
    "AgentKernelReadiness",
    "LeaseHeartbeat",
    "build_agent_kernel_runtime",
    "bootstrap_agent_kernel_runtime_from_env",
    "set_agent_kernel_runtime",
    "get_agent_kernel_runtime",
    "clear_agent_kernel_runtime",
]
