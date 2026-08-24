# -*- coding: utf-8 -*-
"""深模块 AgentKernel control facade（Phase 1 Task 6 Step 3）。

小接口 ``submit`` / ``status`` / ``subscribe``：

- ``submit``：先 permit 验证（fail closed）、capability 判定、queue limit，
  再进入单个 Store transaction（accept_command 内部完成 Inbox 行 +
  ``control.command_accepted`` 事件的 persist-before-ack）。
- ``status``：只读 Store（active Run / Inbox depth / lease）+ capability
  matrix，不创建 Run。
- ``subscribe``：直接委托 SessionEventStore cursor（replay 后 live）。
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Any

from ksadk.events.session_event import SessionEventStore
from ksadk.kernel.authorization import (
    AgentControlPermitVerifier,
    PermitExpiredError,
)
from ksadk.kernel.contracts import (
    AgentControlCommand,
    AgentControlPermit,
    AgentControlReceipt,
    AgentStatusQuery,
    AgentStatusSnapshot,
    RuntimeCapability,
    RuntimeCapabilityMatrix,
    SessionEventEnvelope,
    SessionEventSubscription,
)
from ksadk.kernel.errors import InvalidPermitError
from ksadk.kernel.mapping import (
    CapabilityProvider,
    capability_of,
)
from ksadk.kernel.store import AgentKernelStore, command_digest, now_utc


def _unavailable(reason: str = "not_implemented") -> RuntimeCapability:
    return RuntimeCapability(supported=False, mode="unavailable", reason=reason)


def default_capability_matrix() -> RuntimeCapabilityMatrix:
    """未提供 adapter capability 时的诚实默认值（全部 unavailable）。"""

    return RuntimeCapabilityMatrix(
        cancel=_unavailable(),
        pause=_unavailable(),
        resume=_unavailable(),
        submit_interaction=_unavailable(),
        attach=_unavailable(),
        steer=_unavailable("runtime_no_native_steer"),
        inject=_unavailable("runtime_no_native_inject"),
        checkpoint=_unavailable(),
        durable_restore=_unavailable(),
    )


class AgentKernel:
    def __init__(
        self,
        store: AgentKernelStore,
        session_events: SessionEventStore,
        permit_verifier: AgentControlPermitVerifier,
        *,
        queue_limit: int = 100,
        capabilities: CapabilityProvider | None = None,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self._store = store
        self._events = session_events
        self._permit_verifier = permit_verifier
        self._queue_limit = int(queue_limit)
        self._capabilities = capabilities or default_capability_matrix
        self._clock = clock

    def capabilities(self) -> RuntimeCapabilityMatrix:
        """Return the runtime's current typed capability snapshot.

        Readiness propagation uses this same source as admission, so an
        Operator/Server never treats a deploy-time digest as a substitute for
        the actual operation support matrix.
        """

        return self._capabilities()

    # ---------------------------------------------------------------- submit

    async def submit(
        self, command: AgentControlCommand, *, permit: AgentControlPermit
    ) -> AgentControlReceipt:
        try:
            await self._permit_verifier.verify(
                permit, command, command.command_type, self._clock()
            )
        except PermitExpiredError:
            return await self._expired_permit_receipt(command)
        except InvalidPermitError as error:
            return await self._store.reject_command(
                command,
                status="rejected",
                code="invalid_permit",
                message=error.message,
            )

        field, capability = capability_of(command.command_type, self._capabilities())
        if field is not None and not capability.supported:
            # steer/inject 等绝不降级为 enqueue：直接 unsupported。
            return await self._store.reject_command(
                command,
                status="unsupported",
                code=capability.reason or "not_implemented",
                message=f"runtime capability {field} is unavailable",
            )

        return await self._store.accept_command(
            command, queue_limit=self._queue_limit
        )

    async def _expired_permit_receipt(
        self, command: AgentControlCommand
    ) -> AgentControlReceipt:
        """permit 过期：只有 Store 已有完全相同请求（同 digest/幂等域）才 duplicate。"""

        existing = await self._store.load_by_idempotency(
            command.session_id, command.idempotency_key
        )
        if existing is not None and existing.request_digest == command_digest(command):
            return AgentControlReceipt(
                command_id=command.command_id,
                status="duplicate",
                message_id=existing.message_id,
                accepted_seq=existing.accepted_seq,
            )
        return await self._store.reject_command(
            command,
            status="rejected",
            code="invalid_permit",
            message="permit_expired",
        )

    # ---------------------------------------------------------------- status

    async def status(
        self, query: AgentStatusQuery, *, permit: AgentControlPermit
    ) -> AgentStatusSnapshot:
        try:
            await self._permit_verifier.verify(
                permit, query, "get_status", self._clock()
            )
        except (InvalidPermitError, PermitExpiredError):
            return AgentStatusSnapshot(
                agent_instance_id=query.agent_instance_id,
                instance_state="unavailable",
                session_id=query.session_id,
                inbox_depth=0,
                capability=self._capabilities(),
            )
        active = await self._store.find_active_run(
            query.agent_instance_id, query.session_id
        )
        lease = await self._store.current_lease(
            query.agent_instance_id, query.session_id
        )
        return AgentStatusSnapshot(
            agent_instance_id=query.agent_instance_id,
            instance_state="ready" if lease is not None else "degraded",
            session_id=query.session_id or (active.session_id if active else None),
            active_run_id=active.run_id if active else None,
            active_run_state=active.state.value if active else None,
            inbox_depth=await self._store.inbox_depth(
                query.agent_instance_id, query.session_id
            ),
            activation_id=lease.activation_id if lease else None,
            lease_expires_at=lease.lease_expires_at if lease else None,
            capability=self._capabilities(),
        )

    # ------------------------------------------------------------- subscribe

    async def subscribe(
        self,
        subscription: SessionEventSubscription,
        *,
        permit: AgentControlPermit,
        should_stop: Callable[[], Awaitable[bool]] | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[SessionEventEnvelope]:
        await self._permit_verifier.verify(
            permit, subscription, "subscribe_events", self._clock()
        )
        subscribe = self._events.subscribe
        kwargs: dict[str, Any] = {}
        try:
            signature = signature_of(subscribe)
        except (TypeError, ValueError):
            signature = None
        parameters = getattr(signature, "parameters", {}) or {}
        if should_stop is not None and "should_stop" in parameters:
            kwargs["should_stop"] = should_stop
        if timeout is not None and "timeout" in parameters:
            kwargs["timeout"] = timeout
        async for envelope in subscribe(
            subscription.session_id, subscription.after_seq, **kwargs
        ):
            yield envelope


def signature_of(func: Any) -> Any:
    import inspect

    return inspect.signature(func)


__all__ = ["AgentKernel", "default_capability_matrix"]
