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
import hashlib
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from ksadk.events.session_event import SessionServiceEventStore
from ksadk.kernel.authorization import AgentControlPermitVerifier, InMemoryNonceStore
from ksadk.kernel.contract_fingerprints import (
    AGENT_KERNEL_V1_AGGREGATE_DIGEST,
    runtime_capability_matrix_digest,
    runtime_capability_matrix_wire_value,
)
from ksadk.kernel.contracts import RuntimeCapabilityMatrix
from ksadk.kernel.control import AgentKernel, default_capability_matrix
from ksadk.kernel.errors import InvalidCommandError
from ksadk.kernel.recovery import RecoveryCoordinator
from ksadk.kernel.runtime_identity import runtime_identity
from ksadk.kernel.store import AgentKernelStore, now_utc
from ksadk.kernel.worker import AgentKernelWorker
from ksadk.runtime.adapter import RuntimeAdapter

AuthorityMode = Literal["local", "hosted"]
DurabilityTier = Literal["durable", "ephemeral"]

logger = logging.getLogger(__name__)


@dataclass
class AgentKernelRuntimeConfig:
    """生产装配配置（Operator env 投影或测试注入 fake PG provider）。"""

    agent_instance_id: str
    authority_mode: AuthorityMode = "local"
    driver: str = "memory"  # postgres | sqlite | memory
    # durable: PG-backed inbox/lease/nonce + recovery; ephemeral: one-pod
    # runtime that intentionally loses kernel state on restart.
    durability_tier: DurabilityTier = "durable"
    dsn: str = ""
    # server authority（hosted 必填）
    jwks: Any | None = None
    permit_issuer: str | None = None
    nonce_store: Any | None = None
    # 运行时
    adapter_provider: Callable[[], RuntimeAdapter] | None = None
    capabilities: Callable[[], RuntimeCapabilityMatrix] | None = None
    start_request_defaults: dict[str, Any] = field(default_factory=dict)
    # 契约 digest（hosted 必填 contract_digest）
    contract_digest: str = ""
    capability_digest: str = ""
    bundle_digest: str = ""
    # Session log 的 scope 必须与普通 SessionService、canonical event log
    # 完全一致；否则 worker 可启动却会在首条 command 后看不到 session。
    session_namespace: str = "default"
    tenant_id: str = "default"
    workspace_id: str = "default"
    # 测试注入的 fake PG provider：提供时不再从 dsn 建真实连接，
    # 但 hosted 模式的 dsn 必填校验仍然生效。
    store: AgentKernelStore | None = None
    session_events: Any | None = None
    session_service: Any | None = None
    # Runtime App composition root supplies these so recovery can attach via
    # the same RuntimeAdapter registry rather than creating an unrelated path.
    runtime_executor: Any | None = None
    launch_context: Any | None = None
    pool: Any | None = None
    owns_pool: bool = False
    # 生命周期参数
    queue_limit: int = 100
    lease_ttl_seconds: float = 60.0
    poll_interval: float = 0.25
    activation_id: str | None = None
    runtime_type: str = "ksadk-agent-kernel"
    clock: Callable[[], datetime] = now_utc
    # 容错粒度：连续多少个不同 session 恢复失败才认为全局性故障（进程级
    # degraded）；store 连续多少个 poll 周期不可达才整体降级。
    quarantine_degrade_threshold: int = 5
    store_failure_degrade_threshold: int = 10


class LeaseHeartbeat:
    """activation lease 的获取 / 续约 / takeover 检测。

    同一 workload activation 在每个 session 有一个派生且稳定的
    ``activation_id``。这样 Store 的 ``renew_activation(id)`` / fenced event
    guard 可以无歧义定位一行 lease；不能把单个 Pod id 原样复用于多行
    session activation。token 变化（> 已知值）说明发生过 takeover，调用方
    应触发 RecoveryCoordinator 对 open run 做确定性收口。
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
            runtime_type=runtime_type,
            bundle_digest=bundle_digest or "unknown",
            capability_digest=capability_digest or "unknown",
            lease_ttl_seconds=lease_ttl_seconds,
        )
        self._last_tokens: dict[str, int] = {}

    def activation_id_for_session(self, session_id: str) -> str:
        """Return the opaque per-session lease owner id for this workload."""

        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
        return f"{self.activation_id}:s:{digest}"

    def owns_lease(self, session_id: str, lease: Any) -> bool:
        return str(getattr(lease, "activation_id", "")) == self.activation_id_for_session(
            session_id
        )

    async def ensure_lease(self, session_id: str) -> tuple[Any, bool]:
        """获取（或幂等续约）session 的 lease。

        返回 ``(lease, took_over)``：lease 为 None 表示被其它 owner 持有；
        ``took_over`` 表示本次拿到的 fencing token 比已知值新（发生过
        takeover，需要 recovery）。
        """

        from ksadk.kernel.store import ActivationLeaseRequest

        try:
            lease = await self._store.acquire_activation(
                ActivationLeaseRequest(
                    session_id=session_id,
                    activation_id=self.activation_id_for_session(session_id),
                    **self._request,
                )
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
            if not self.runtime.lease_heartbeat.owns_lease(session_id, lease):
                # lease 存在但已被其它 activation 接管：对本 runtime 而言
                # 等价于丢失，必须如实上报 not-ready。
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
        degraded = self.runtime.degraded
        quarantined = self.runtime.quarantined_sessions()
        capability = self.runtime.kernel.capabilities()
        capability_matrix = runtime_capability_matrix_wire_value(capability)
        computed_capability_digest = runtime_capability_matrix_digest(capability)
        # The control plane compares all three digests before declaring an
        # AgentInstance ready.  Reporting ready with only a contract digest
        # would conceal a missing bundle/capability projection and make the
        # runtime's health endpoint more optimistic than Server readiness.
        digests_match = all(
            (
                config.contract_digest == AGENT_KERNEL_V1_AGGREGATE_DIGEST,
                config.capability_digest == computed_capability_digest,
                config.bundle_digest,
            )
        )
        ready = (
            store_ok and worker_running and lease_healthy and digests_match
            and not degraded
        )
        health = {
            "ready": ready,
            "store_ok": store_ok,
            "worker_running": worker_running,
            "degraded": degraded,
            # additive：被隔离（恢复失败）的 session 数量；隔离本身不影响
            # ready，其余 session 照常服务。
            "quarantined_sessions": len(quarantined),
            "lease_healthy": lease_healthy,
            "activation_id": activation_id,
            # Contract support is packaged with this KsADK image; never echo
            # an unverified control-plane environment value as evidence.
            "contract_digest": AGENT_KERNEL_V1_AGGREGATE_DIGEST,
            # Likewise derive capabilities from the actual Adapter matrix,
            # rather than trusting the requested deployment digest.
            "capability_digest": computed_capability_digest,
            # Runtime/Operator/Server readiness chain must carry the actual
            # typed capability facts, not merely a digest supplied at deploy
            # time. Server admission uses these to reject unsupported control
            # operations before they enter the durable inbox.
            "capabilities": capability_matrix,
            "bundle_digest": config.bundle_digest,
            "durability_tier": config.durability_tier,
            # Identity is derived from the KsADK source Python imported, not
            # ``importlib.metadata`` for the base image distribution.
            "runtime_identity": runtime_identity(),
        }
        # 诊断字段（additive，runtime 内部端点非 wire 冻结合同）：
        # degraded 时必须能从 health 直接回答 "为什么降级、何时降级"，
        # 出问题的 session 明细同样可见，运维不必再对着布尔值猜。
        if quarantined:
            health["quarantined_session_ids"] = sorted(quarantined)
        if degraded:
            health["degradation_reason"] = self.runtime._degradation_reason
            health["degraded_at"] = self.runtime._degraded_at
            health["degradation_last_error"] = (
                self.runtime._degraded_last_error
            )
        return health


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
        self._last_renewed: dict[str, float] = {}
        self._degraded = False
        # P0-1 粒度修正：单个 session 恢复失败不再拖垮整个 runtime。
        self._quarantined: set[str] = set()
        self._recovery_failed_sessions: set[str] = set()
        self._store_failures = 0
        # 诊断状态：degraded 必须能回答 "为什么、什么时候、哪些 session"，
        # 让 kubectl logs 与 health 端点一眼可见（此前只有 degraded 布尔值）。
        self._degradation_reason: str | None = None
        self._degraded_at: str | None = None
        self._degraded_last_error: str | None = None

    # ------------------------------------------------------------ degradation

    def _mark_degraded(
        self, reason: str, exc: BaseException | None = None
    ) -> None:
        """统一降级入口：醒目 ERROR 日志 + 可供 health 端点回读的诊断状态。"""

        self._degraded = True
        if self._degradation_reason is None:
            self._degradation_reason = reason
        last_error = (
            f"{type(exc).__name__}: {exc}" if exc is not None else "n/a"
        )
        self._degraded_last_error = last_error
        try:
            self._degraded_at = self.config.clock().isoformat()
        except Exception:  # pragma: no cover - clock 异常不应影响降级本身
            self._degraded_at = None
        logger.error(
            "agent kernel degraded: agent_instance_id=%s reason=%s "
            "failed_sessions=%d quarantined=%d last_error=%s",
            self.config.agent_instance_id,
            self._degradation_reason,
            len(self._recovery_failed_sessions),
            len(self._quarantined),
            last_error,
        )

    # ------------------------------------------------------------ properties

    @property
    def worker_running(self) -> bool:
        return self._worker_running

    @property
    def degraded(self) -> bool:
        """P0-1：takeover 收口彻底失败后 runtime 显式降级（停止消费 Inbox）。"""

        return self._degraded

    def heartbeat_sessions(self) -> set[str]:
        return set(self._heartbeat_sessions)

    def quarantined_sessions(self) -> set[str]:
        """被隔离的 session：恢复失败且不再被本 runtime 消费。"""

        return set(self._quarantined)

    @property
    def background_tasks(self) -> list[asyncio.Task]:
        return list(self._tasks)

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._tasks:
            return
        self._worker_running = True
        self._tasks.append(asyncio.create_task(self._run_loop(), name="kernel-runtime"))
        # 心跳续约必须是独立任务：run loop 可能长时间阻塞在某个 session 的
        # adapter.start()（hosted pod 上 codex 握手可超过 lease TTL），内联
        # 续约会把其它已持有 lease 的 session 拖过期（stream guard StaleFence）。
        self._tasks.append(
            asyncio.create_task(self._heartbeat_loop(), name="kernel-heartbeat")
        )

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
        for session_id in list(self._heartbeat_sessions | self._quarantined):
            try:
                lease = await self.kernel_store.current_lease(
                    self.config.agent_instance_id, session_id
                )
                if lease is not None and self.lease_heartbeat.owns_lease(
                    session_id, lease
                ):
                    await self.kernel_store.release_activation(
                        lease.activation_id, expected_fence=lease.fencing_token
                    )
            except Exception:
                pass
            self.lease_heartbeat.forget(session_id)
        self._heartbeat_sessions.clear()
        self._last_renewed.clear()
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
                if self._degraded:
                    return
                progressed = False
                try:
                    sessions = await self._pending_sessions()
                    for session_id in sorted(sessions):
                        if session_id in self._quarantined:
                            # 隔离中的 session：不 claim inbox、不恢复、
                            # 不写任何 canonical 事件（等待人工清理）。
                            continue
                        lease, took_over = await self.lease_heartbeat.ensure_lease(
                            session_id
                        )
                        if lease is None:
                            continue
                        self._heartbeat_sessions.add(session_id)
                        self._last_renewed[session_id] = time.monotonic()
                        if took_over:
                            # takeover：对 open run 做确定性收口（attach /
                            # resume / interrupted），再继续消费 inbox。
                            # P0-1：recover 抛错不得静默吞掉——先尝试
                            # durable 兜底收口；连收口都失败则隔离该
                            # session 并上报，其余 session 继续服务；只有
                            # 全局性故障（store 不可达或失败扩散到阈值）
                            # 才进程级 degraded。
                            failure = await self._recover_safely(
                                lease, session_id
                            )
                            if failure is not None:
                                if not await self._quarantine_session(
                                    session_id, failure
                                ):
                                    return
                                continue
                        result = await self.worker.run_once(
                            self.config.agent_instance_id,
                            lease,
                            session_id=session_id,
                        )
                        if result.outcome != "idle":
                            progressed = True
                    self._store_failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._store_failures += 1
                    if (
                        self._store_failures
                        >= self.config.store_failure_degrade_threshold
                        and not await self._store_reachable()
                    ):
                        # store 持续不可达是全局性故障：宁降级不静默。
                        self._mark_degraded("store_unreachable")
                        return
                    await asyncio.sleep(self.config.poll_interval * 4)
                    continue
                if not progressed:
                    await asyncio.sleep(self.config.poll_interval)
        finally:
            self._worker_running = False

    async def _heartbeat_loop(self) -> None:
        """独立心跳任务：按 TTL/3 节奏续约所有已持有的 lease。"""

        interval = max(
            self.config.lease_ttl_seconds / 3.0, self.config.poll_interval
        )
        while True:
            await asyncio.sleep(interval / 2.0)
            await self._renew_leased_sessions()
            if self._degraded:
                return

    async def _renew_leased_sessions(self) -> None:
        """P0：已持有 lease 的 session 在固定间隔上持续续约。

        之前续约只发生在有 pending inbox 工作（accepted/claimed 消息）时，
        run 完成 / 等待审批的 session 不再续约但留在 heartbeat 集合里，
        lease TTL 过后 readiness 误判 not-ready（预发需重启 pod 才恢复）。
        readiness 语义应是 "runtime 存活且能服务"，不是 "正在忙"：只要
        activation lease 仍由本 runtime 持有，就以 TTL/3 的节奏幂等续约。
        """

        interval = max(
            self.config.lease_ttl_seconds / 3.0, self.config.poll_interval
        )
        now = time.monotonic()
        for session_id in sorted(self._heartbeat_sessions):
            if session_id in self._quarantined:
                continue
            if now - self._last_renewed.get(session_id, 0.0) < interval:
                continue
            self._last_renewed[session_id] = now
            try:
                lease, took_over = await self.lease_heartbeat.ensure_lease(
                    session_id
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # 瞬时 store 错误：保留 session，下个续约周期重试。
                continue
            if lease is None:
                # lease 被其它 activation 持有（真正丢失）：保留在集合里，
                # readiness 如实上报 not-ready。
                continue
            failure = None
            if took_over:
                failure = await self._recover_safely(lease, session_id)
            if failure is not None:
                if not await self._quarantine_session(session_id, failure):
                    if not self._degraded:
                        self._mark_degraded(
                            "renew_recovery_failure", failure
                        )
                    return

    async def _recover_safely(
        self, lease, session_id: str | None = None
    ) -> Exception | None:
        """takeover 后的安全恢复：失败必须持久化收口，否则返回失败原因。

        返回 None 表示恢复路径已收口（含 durable interrupted 兜底），
        可以继续消费 Inbox；返回异常表示连兜底收口都失败，由调用方决定
        隔离该 session 还是进程级 degraded。
        """

        try:
            await self.recovery.recover(self.config.agent_instance_id, lease)
            return None
        except Exception as exc:
            first_failure = exc
            # 恢复主路径失败：降级/quarantine 决策前必须先留下完整现场
            # （此前这里静默吞掉，坏 session 全程零日志）。
            logger.exception(
                "agent kernel takeover recovery failed: "
                "agent_instance_id=%s session_id=%s activation_id=%s "
                "error=%s: %s",
                self.config.agent_instance_id,
                session_id or getattr(lease, "session_id", None),
                getattr(lease, "activation_id", None),
                type(exc).__name__,
                exc,
            )
        try:
            await self.recovery.settle_interrupted(
                self.config.agent_instance_id, lease
            )
            # 主恢复失败但 durable interrupted 兜底收口成功：半恢复状态，
            # 运维需要可见（事件流里会出现确定性的 interrupted 收口）。
            logger.warning(
                "agent kernel settled interrupted after recovery failure: "
                "agent_instance_id=%s session_id=%s activation_id=%s "
                "recovery_error=%s: %s",
                self.config.agent_instance_id,
                session_id or getattr(lease, "session_id", None),
                getattr(lease, "activation_id", None),
                type(first_failure).__name__,
                first_failure,
            )
            return None
        except Exception as exc:
            logger.exception(
                "agent kernel interrupted-settlement fallback failed: "
                "agent_instance_id=%s session_id=%s activation_id=%s "
                "error=%s: %s",
                self.config.agent_instance_id,
                session_id or getattr(lease, "session_id", None),
                getattr(lease, "activation_id", None),
                type(exc).__name__,
                exc,
            )
            return first_failure or exc

    async def _store_reachable(self) -> bool:
        """store 是否仍可用：用于区分 session 级故障与全局连接故障。"""

        try:
            await self.kernel_store.list_messages(self.config.agent_instance_id)
        except Exception:
            return False
        return True

    async def _quarantine_session(self, session_id: str, exc: Exception) -> bool:
        """隔离一个恢复失败的 session；返回 False 表示已触发进程级降级。

        被隔离的 session 不再被本 runtime claim / 恢复 / 续约，其 inbox
        消息保持 accepted（人工清理后可被新 activation 恢复）；不写任何
        canonical 事件，避免污染日志。只有全局性故障——store 不可达或
        恢复失败扩散到 ``quarantine_degrade_threshold`` 个不同 session——
        才升级为进程级 degraded。
        """

        if not await self._store_reachable():
            # store 本身不可达：这不是单个 session 的问题。
            self._mark_degraded(
                "store_unreachable_during_recovery", exc
            )
            return False
        self._quarantined.add(session_id)
        self._recovery_failed_sessions.add(session_id)
        self._heartbeat_sessions.discard(session_id)
        self._last_renewed.pop(session_id, None)
        self.lease_heartbeat.forget(session_id)
        logger.warning(
            "agent kernel session %s quarantined after takeover recovery "
            "failed: agent_instance_id=%s error=%s: %s",
            session_id,
            self.config.agent_instance_id,
            type(exc).__name__,
            exc,
        )
        if (
            len(self._recovery_failed_sessions)
            >= self.config.quarantine_degrade_threshold
        ):
            self._mark_degraded(
                "recovery_failures_spread_to_%d_sessions" % len(
                    self._recovery_failed_sessions
                ),
                exc,
            )
            return False
        return True

    async def _pending_sessions(self) -> set[str]:
        messages = await self.kernel_store.list_messages(
            self.config.agent_instance_id
        )
        inbox_sessions = {
            message.session_id
            for message in messages
            if message.status.value in ("accepted", "claimed")
        }
        # Inbox is completed as soon as a stream is launched.  Keep renewing
        # the owning lease after the independent live execution finishes too:
        # this runtime remains the session's activation owner while the Pod is
        # healthy, so a later control command stays on the same fenced owner
        # and readiness can truthfully detect an external takeover.  ``close``
        # releases the retained leases; an ungraceful stop lets their TTL
        # expire for recovery by a new activation.
        active_sessions = self.worker.active_session_ids()
        return inbox_sessions | active_sessions


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
    if not config.agent_instance_id or config.agent_instance_id == "local-agent":
        missing.append("agent_instance_id")
    if config.driver not in {"postgres", "memory"}:
        missing.append("driver(postgres|memory)")
    if config.driver == "postgres" and not config.dsn:
        missing.append("dsn")
    if config.driver == "postgres" and config.durability_tier != "durable":
        missing.append("durability_tier=durable for postgres")
    if config.driver == "memory" and config.durability_tier != "ephemeral":
        missing.append("durability_tier=ephemeral for memory")
    if config.jwks is None:
        missing.append("jwks")
    if not config.permit_issuer:
        missing.append("permit_issuer")
    if config.adapter_provider is None:
        missing.append("adapter_provider")
    if not config.contract_digest:
        missing.append("contract_digest")
    if not config.capability_digest:
        missing.append("capability_digest")
    if not config.bundle_digest:
        missing.append("bundle_digest")
    if config.nonce_store is None:
        missing.append("nonce_store")
    # 租约的 owner 必须是实际 workload identity。固定的 instance-level
    # fallback 会把多 Pod 误识别为同一个 activation，破坏 fencing/takeover。
    if not config.activation_id:
        missing.append("activation_id")
    if missing:
        raise RuntimeError(
            "hosted agent kernel runtime requires "
            + ", ".join(missing)
            + "; refusing to bootstrap (fail closed)"
        )
    if config.contract_digest != AGENT_KERNEL_V1_AGGREGATE_DIGEST:
        raise RuntimeError(
            "contract_digest_mismatch: hosted agent kernel runtime image "
            "does not support the control-plane contract digest"
        )


def build_agent_kernel_runtime(
    config: AgentKernelRuntimeConfig,
) -> AgentKernelRuntime:
    """组装生产 kernel runtime；hosted 模式缺依赖时 fail loud。"""

    _validate_hosted(config)

    store = config.store
    session_events = config.session_events
    session_service = config.session_service
    owns_pool = config.owns_pool
    pool = config.pool

    if store is None or session_events is None:
        if config.driver == "postgres":
            from ksadk.kernel.postgres_store import (
                PostgresAgentKernelStore,
                PostgresFencedSessionEventStore,
                PostgresKernelEventLog,
            )
            from ksadk.sessions.postgres_service import PostgresSessionService

            if not config.dsn:
                raise RuntimeError(
                    "postgres agent kernel runtime requires a store DSN"
                )
            if session_service is None:
                session_service = PostgresSessionService(
                    dsn=config.dsn,
                    namespace=config.session_namespace,
                    tenant_id=config.tenant_id,
                    workspace_id=config.workspace_id,
                )
            pool = getattr(session_service, "_pool", None)
            event_log = PostgresKernelEventLog(
                pool,
                namespace=session_service.namespace,
                tenant_id=session_service.tenant_id,
                workspace_id=session_service.workspace_id,
            )
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
    if config.authority_mode == "hosted":
        # Snapshot the actual adapter declaration before accepting work.  A
        # hosted pod must not downgrade to the default matrix if its adapter
        # fails to describe itself: that could make Server's capability
        # admission disagree with the execution owner.
        try:
            capability_snapshot = (
                capabilities() if capabilities is not None else adapter_provider().capabilities()
            )
        except Exception as exc:
            raise RuntimeError(
                "hosted agent kernel runtime cannot determine adapter capabilities"
            ) from exc
        computed_capability_digest = runtime_capability_matrix_digest(
            capability_snapshot
        )
        if config.capability_digest != computed_capability_digest:
            raise RuntimeError(
                "capability_digest_mismatch: hosted adapter capabilities do not "
                "match the control-plane deployment digest"
            )

        def capabilities() -> RuntimeCapabilityMatrix:  # type: ignore[misc]
            return capability_snapshot

    elif capabilities is None:
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
        store,
        adapter_factory=adapter_provider,
        session_events=session_events,
        start_request_defaults=config.start_request_defaults,
    )
    recovery = RecoveryCoordinator(
        store,
        session_events,
        capabilities,
        executor=config.runtime_executor,
        launch_context=config.launch_context,
        adapter_factory=adapter_provider,
        # takeover 重建的 live execution 交还 worker（ActiveExecution 归
        # 当前 activation 持有，Interaction 回包才能打到同一 client 实例）。
        execution_sink=worker.adopt_execution,
    )
    heartbeat = LeaseHeartbeat(
        store,
        agent_instance_id=config.agent_instance_id,
        activation_id=config.activation_id
        or f"{config.agent_instance_id}:kernel-runtime",
        runtime_type=config.runtime_type,
        bundle_digest=config.bundle_digest,
        # Lease metadata must represent the exact matrix this owner executes,
        # not an unverified environment projection.
        capability_digest=runtime_capability_matrix_digest(capabilities()),
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


async def bootstrap_agent_kernel_runtime_from_env(
    *,
    adapter_provider: Callable[[], RuntimeAdapter] | None = None,
    runtime_executor: Any | None = None,
    launch_context: Any | None = None,
    start_request_defaults: dict[str, Any] | None = None,
) -> AgentKernelRuntime | None:
    """Operator env 投影 -> 生产 runtime（AGENT_KERNEL_ENABLED=1 时）。

    hosted 部署（AGENT_KERNEL_STORE_DRIVER=postgres + JWKS URL）装配并启动
    worker/lease/recovery，同时注册 kernel ingress 与 runtime health。
    """

    from ksadk.kernel.ingress import (
        ENV_JWKS_URL,
        _remote_jwks_source,
        authority_mode,
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

    driver = os.environ.get("AGENT_KERNEL_STORE_DRIVER", "memory").strip().lower()
    dsn = os.environ.get("AGENT_KERNEL_STORE_DSN", "").strip()
    jwks_url = os.environ.get(ENV_JWKS_URL, "").strip()
    session_namespace = (
        os.environ.get("KSADK_SESSION_NAMESPACE", "default").strip() or "default"
    )
    tenant_id = (
        os.environ.get("KSADK_TENANT_ID")
        or os.environ.get("AGENTENGINE_TENANT_ID")
        or "default"
    ).strip()
    workspace_id = (
        os.environ.get("KSADK_WORKSPACE_ID")
        or os.environ.get("AGENTENGINE_WORKSPACE_ID")
        or "default"
    ).strip()
    mode: AuthorityMode = authority_mode()  # type: ignore[assignment]
    durability_tier: DurabilityTier = os.environ.get(
        "AGENT_KERNEL_DURABILITY_TIER", "durable"
    ).strip().lower()  # type: ignore[assignment]
    injected_contract_digest = os.environ.get(
        "AGENT_KERNEL_CONTRACT_DIGEST", ""
    ).strip()
    if (
        mode == "hosted"
        and injected_contract_digest != AGENT_KERNEL_V1_AGGREGATE_DIGEST
    ):
        raise RuntimeError(
            "contract_digest_mismatch: hosted agent kernel runtime image "
            "does not support the control-plane contract digest"
        )

    pool = None
    owns_pool = False
    if driver == "postgres":
        from ksadk.kernel.postgres_store import (
            PostgresAgentKernelStore,
            PostgresFencedSessionEventStore,
            PostgresKernelEventLog,
            PostgresNonceStore,
        )
        from ksadk.sessions.postgres_service import PostgresSessionService

        if not dsn:
            raise RuntimeError("postgres kernel store requires AGENT_KERNEL_STORE_DSN")
        session_service = PostgresSessionService(
            dsn=dsn,
            namespace=session_namespace,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )
        await session_service._ensure_pool()
        pool = session_service._pool
        event_log = PostgresKernelEventLog(
            pool,
            namespace=session_service.namespace,
            tenant_id=session_service.tenant_id,
            workspace_id=session_service.workspace_id,
        )
        store: AgentKernelStore = PostgresAgentKernelStore(
            pool, event_log, owns_pool=True
        )
        await store.ensure_schema()
        session_events: Any = PostgresFencedSessionEventStore(store)
        nonce_store: Any = PostgresNonceStore(pool)
        owns_pool = True
    else:
        from ksadk.kernel.memory_store import InMemoryAgentKernelStore
        from ksadk.sessions.in_memory import InMemorySessionService

        session_service = InMemorySessionService()
        session_events = SessionServiceEventStore(session_service)
        store = InMemoryAgentKernelStore(session_events)
        # Explicit ephemeral hosted mode still needs replay protection while
        # this process lives. It does not promise restart durability.
        nonce_store = InMemoryNonceStore()

    agent_instance_id = os.environ.get("AGENT_INSTANCE_ID", "").strip()
    if not agent_instance_id and mode != "hosted":
        agent_instance_id = "local-agent"
    pod_uid = os.environ.get("POD_UID", "").strip()
    config = AgentKernelRuntimeConfig(
        agent_instance_id=agent_instance_id,
        authority_mode=mode,
        driver=driver,
        durability_tier=durability_tier,
        dsn=dsn,
        jwks=_remote_jwks_source(jwks_url) if mode == "hosted" else None,
        permit_issuer=os.environ.get("AGENT_CONTROL_PERMIT_ISSUER", ""),
        nonce_store=nonce_store,
        adapter_provider=adapter_provider,
        start_request_defaults=dict(start_request_defaults or {}),
        contract_digest=(
            AGENT_KERNEL_V1_AGGREGATE_DIGEST
            if mode == "hosted"
            else injected_contract_digest
        ),
        capability_digest=os.environ.get("AGENT_KERNEL_CAPABILITY_DIGEST", ""),
        bundle_digest=os.environ.get("AGENT_BUNDLE_DIGEST", ""),
        session_namespace=session_namespace,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        store=store,
        session_events=session_events,
        session_service=session_service,
        runtime_executor=runtime_executor,
        launch_context=launch_context,
        pool=pool,
        owns_pool=owns_pool,
        # Operator 通过 downward API 注入 POD_UID。与 stable instance id
        # 组合才是 activation owner；hosted 少了它必须拒绝启动，不能退回到
        # 所有副本共享的固定字符串。
        activation_id=f"{agent_instance_id}:{pod_uid}" if pod_uid else None,
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
