"""按会话取得、续租并检测 Runtime activation lease 接管。"""

from __future__ import annotations

import hashlib
from typing import Any

from ksadk.kernel.errors import InvalidCommandError
from ksadk.kernel.store import AgentKernelStore


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
