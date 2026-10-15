"""Read runtime readiness from storage, leases, worker state, and contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ksadk.kernel.bootstrap import AgentKernelRuntime

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ksadk.kernel.contract_fingerprints import (
    AGENT_KERNEL_V1_AGGREGATE_DIGEST,
    runtime_capability_admission_digest,
    runtime_capability_matrix_wire_value,
)
from ksadk.kernel.runtime_identity import runtime_identity


@dataclass
class AgentKernelReadiness:
    """truthful readiness probe：每个维度都是真实查询，不是配置回显。"""

    runtime: "AgentKernelRuntime"

    async def check(self) -> dict[str, Any]:
        config = self.runtime.config
        store_ok = False
        try:
            # 真实 store 查询（PG driver 即真实 SQL round-trip）。
            await self.runtime.kernel_store.list_messages(config.agent_instance_id)
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

                expires_at = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
                if expires_at <= datetime.now(expires_at.tzinfo):
                    lease_healthy = False
            except ValueError:
                lease_healthy = False

        worker_running = self.runtime.worker_running
        degraded = self.runtime.degraded
        quarantined = self.runtime.quarantined_sessions()
        capability = self.runtime.kernel.capabilities()
        capability_matrix = runtime_capability_matrix_wire_value(capability)
        computed_capability_digest = runtime_capability_admission_digest(
            capability, config.capability_digest
        )
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
        ready = store_ok and worker_running and lease_healthy and digests_match and not degraded
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
            # Derive the admission proof from the actual Adapter matrix and the
            # selected policy rather than echoing the deployment value blindly.
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
        teams_descriptor = getattr(self.runtime, "teams_host_descriptor", None)
        if teams_descriptor is not None:
            health["teams_host"] = await teams_descriptor()
        if quarantined:
            health["quarantined_session_ids"] = sorted(quarantined)
        if degraded:
            health["degradation_reason"] = self.runtime._degradation_reason
            health["degraded_at"] = self.runtime._degraded_at
            health["degradation_last_error"] = self.runtime._degraded_last_error
        return health
