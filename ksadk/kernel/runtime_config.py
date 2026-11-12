"""Configuration for the existing Kernel composition root."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from ksadk.kernel.contracts import RuntimeCapabilityMatrix
from ksadk.kernel.store import AgentKernelStore, now_utc
from ksadk.runtime.adapter import RuntimeAdapter
from ksadk.sessions.topology import StorageTarget

AuthorityMode = Literal["local", "hosted"]
DurabilityTier = Literal["durable", "ephemeral"]


@dataclass
class AgentKernelRuntimeConfig:
    """生产装配配置（Operator env 投影或测试注入 fake PG provider）。"""

    agent_instance_id: str
    authority_mode: AuthorityMode = "local"
    local_interactions: bool = False
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
    session_storage_target: StorageTarget | None = field(default=None, repr=False)
    storage_owner: Any | None = field(default=None, repr=False)
    preflight_adapter: Any | None = field(default=None, repr=False)
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
    lease_ttl_seconds: float = 90.0
    poll_interval: float = 0.25
    activation_id: str | None = None
    runtime_type: str = "ksadk-agent-kernel"
    clock: Callable[[], datetime] = now_utc
    # 容错粒度：连续多少个不同 session 恢复失败才认为全局性故障（进程级
    # degraded）；store 连续多少个 poll 周期不可达才整体降级。
    quarantine_degrade_threshold: int = 5
    store_failure_degrade_threshold: int = 10
