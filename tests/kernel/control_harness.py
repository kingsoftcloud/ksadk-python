# -*- coding: utf-8 -*-
"""AgentKernel control/worker 测试共用 harness（Phase 1 Task 6）。

提供：
- ``PermitAuthority``：Ed25519 签发方 + 静态 JWKS 源（记录 fetch 次数）。
- ``FakeAdapter``：可配置 capability 的最小 ``RuntimeAdapter``，记录调用时序。
- ``build_kernel`` / ``build_worker``：内存栈 + 固定时钟的 kernel/worker 组装。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from ksadk.events.session_event import SessionServiceEventStore
from ksadk.kernel.authorization import (
    AgentControlPermitVerifier,
    sign_permit,
)
from ksadk.kernel.contracts import (
    ActivationLease,
    AgentControlCommand,
    AgentControlPermit,
    ControlSource,
    RuntimeCapability,
    RuntimeCapabilityMatrix,
)
from ksadk.kernel.memory_store import InMemoryAgentKernelStore
from ksadk.kernel.store import ActivationLeaseRequest
from ksadk.runtime.adapter import (
    BaseRuntime,
    CancelResult,
    PauseResult,
    RunHandle,
    RuntimeAdapter,
    StartRequest,
)
from ksadk.sessions.in_memory import InMemorySessionService

TENANT = "tenant-1"
AGENT = "agent-1"
KEY_ID = "key-1"
PERMIT_REF = f"permit-{KEY_ID}"
# permit TTL 上限 300s（与 server PERMIT_MAX_TTL_SECONDS 对齐）。时间线：
# issued 00:00:00 -> expires 00:05:00（恰好 300s），CLOCK_AT 00:03:00，
# EXPIRED_AT 00:02:00。
CLOCK_AT = datetime(2026, 8, 18, 0, 3, 0, tzinfo=UTC)
ISSUED_AT = "2026-08-18T00:00:00Z"
EXPIRES_AT = "2026-08-18T00:05:00Z"
EXPIRED_AT = "2026-08-18T00:02:00Z"


class StaticJwks:
    """记录 fetch 次数的静态 JWKS 源。"""

    def __init__(self, keys: Mapping[str, str]) -> None:
        self._keys = dict(keys)
        self.fetch_count = 0

    async def fetch_verification_keys(self) -> Mapping[str, str]:
        self.fetch_count += 1
        return dict(self._keys)


class PermitAuthority:
    def __init__(self, key_id: str = KEY_ID) -> None:
        self.key_id = key_id
        self._private = Ed25519PrivateKey.generate()
        self._public: Ed25519PublicKey = self._private.public_key()

    def jwks(self) -> StaticJwks:
        from ksadk.kernel.authorization import b64url_encode

        raw = self._public.public_bytes_raw()
        return StaticJwks({self.key_id: b64url_encode(raw)})

    def permit(
        self,
        *,
        operations: tuple[str, ...] = ("enqueue",),
        session_id: str | None = "s1",
        tenant_id: str = TENANT,
        agent_instance_id: str = AGENT,
        expires_at: str = EXPIRES_AT,
        issued_at: str = ISSUED_AT,
        nonce: str | None = None,
        subject_ref: str = "user-1",
        permit_id: str | None = None,
    ) -> AgentControlPermit:
        import hashlib
        import json

        claims = {
            "subject": subject_ref,
            "operations": list(operations),
            "session_id": session_id,
        }
        claims_digest = hashlib.sha256(
            json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        unsigned = AgentControlPermit(
            permit_id=permit_id or f"permit-{self.key_id}",
            subject_ref=subject_ref,
            tenant_id=tenant_id,
            agent_instance_id=agent_instance_id,
            session_id=session_id,
            allowed_operations=list(operations),
            issued_at=issued_at,
            expires_at=expires_at,
            nonce=nonce or f"nonce-{uuid4().hex[:8]}",
            key_id=self.key_id,
            claims_digest=claims_digest,
            signature="",  # placeholder, excluded from canonical bytes
        )
        return unsigned.model_copy(
            update={"signature": sign_permit(unsigned, self._private)}
        )


def command(
    command_type: str = "enqueue",
    *,
    session_id: str = "s1",
    idempotency_key: str | None = None,
    content: str = "hello",
    tenant_id: str = TENANT,
    authorization_ref: str = PERMIT_REF,
    payload: dict | None = None,
) -> AgentControlCommand:
    if payload is None:
        payload = {"content": {"text": content}}
        if command_type == "inject":
            payload = {"context": {"text": content}}
        elif command_type == "resume":
            payload = {"target": {"kind": "run", "id": "run-1"}, "input": None}
        elif command_type == "submit_interaction":
            payload = {
                "run_id": "run-1",
                "interaction_id": "it-1",
                "token_ref": "tok-ref-1",
                "response": {"ok": True},
            }
        elif command_type in ("interrupt", "pause"):
            payload = {"run_id": None, "reason": None}
    return AgentControlCommand(
        command_id=uuid4(),
        idempotency_key=idempotency_key or f"key-{uuid4().hex[:8]}",
        tenant_id=tenant_id,
        agent_instance_id=AGENT,
        session_id=session_id,
        command_type=command_type,
        payload=payload,
        source=ControlSource(kind="studio", ref="local-studio"),
        authorization_ref=authorization_ref,
        submitted_at="2026-08-18T00:09:00Z",
    )


class _FakeRuntime(BaseRuntime):
    runtime_type = "fake"

    def native_capabilities(self) -> dict:
        return {}


def unavailable(reason: str = "not_implemented") -> RuntimeCapability:
    return RuntimeCapability(supported=False, mode="unavailable", reason=reason)


def native() -> RuntimeCapability:
    return RuntimeCapability(supported=True, mode="native")


def default_matrix() -> RuntimeCapabilityMatrix:
    return RuntimeCapabilityMatrix(
        cancel=unavailable(),
        pause=unavailable(),
        resume=unavailable(),
        submit_interaction=unavailable(),
        attach=unavailable(),
        steer=unavailable("runtime_no_native_steer"),
        inject=unavailable("runtime_no_native_inject"),
        checkpoint=unavailable(),
        durable_restore=unavailable(),
        interaction_mode="unavailable",
    )


class FakeAdapter(RuntimeAdapter):
    """记录 start 进入/退出时序，可注入失败与 capability。"""

    def __init__(self, *, matrix: RuntimeCapabilityMatrix | None = None) -> None:
        super().__init__(_FakeRuntime())
        self._matrix = matrix or default_matrix()
        self.start_intervals: list[tuple[str, float, float]] = []
        self.start_requests: list[StartRequest] = []
        self.calls: list[tuple[str, str]] = []
        self.start_delay = 0.0
        self.start_error: Exception | None = None
        self.stream_events: list = []
        self.stream_error: Exception | None = None
        self.block_after_stream_events = False
        self.handle_run_id: str | None = None
        self.streams: list[str] = []
        self.cancel_result = CancelResult.INTERRUPTED_ACTIVE_TURN
        self.pause_result = PauseResult.PAUSED_ACTIVE_TURN

    def capabilities(self) -> RuntimeCapabilityMatrix:
        return self._matrix

    async def start(self, request: StartRequest) -> RunHandle:
        entered = time.monotonic()
        self.start_requests.append(request)
        self.calls.append(("start", request.session_id))
        if self.start_delay:
            await asyncio.sleep(self.start_delay)
        if self.start_error is not None:
            raise self.start_error
        self.start_intervals.append(
            (request.session_id, entered, time.monotonic())
        )
        return RunHandle(
            run_id=self.handle_run_id or f"run-{uuid4().hex[:8]}",
            session_id=request.session_id,
            runtime_type="fake",
        )

    def stream(self, handle: RunHandle) -> AsyncIterator:
        self.streams.append(handle.run_id)

        async def _gen():
            for event in self.stream_events:
                yield event
            if self.block_after_stream_events:
                await asyncio.Event().wait()
            if self.stream_error is not None:
                raise self.stream_error

        return _gen()

    async def cancel(self, handle: RunHandle) -> CancelResult:
        self.calls.append(("cancel", handle.session_id))
        return self.cancel_result

    async def pause(self, handle: RunHandle) -> PauseResult:
        self.calls.append(("pause", handle.session_id))
        return self.pause_result

    async def submit(self, handle, payload) -> None:
        self.calls.append(("submit", handle.session_id))

    async def resume(self, handle, target, payload) -> RunHandle:
        self.calls.append(("resume", handle.session_id))
        return handle

    async def checkpoint(self, handle):
        raise NotImplementedError

    async def close(self, handle) -> None:
        self.calls.append(("close", handle.session_id))


async def _seed(service: InMemorySessionService, *session_ids: str) -> None:
    for session_id in session_ids:
        if await service.get_session(session_id) is None:
            await service.create_session(
                agent_id=AGENT, user_id="kernel-user", session_id=session_id
            )


class KernelStack:
    """内存栈：session service + event store + kernel store + verifier。"""

    def __init__(
        self,
        authority: PermitAuthority | None = None,
        *,
        sessions: tuple[str, ...] = ("s1", "s2"),
        queue_limit: int = 100,
        adapter: FakeAdapter | None = None,
        verifier_cache_max_age: float = 300.0,
    ) -> None:
        self.authority = authority or PermitAuthority()
        self.session_service = InMemorySessionService()
        self.events = SessionServiceEventStore(self.session_service)
        self.store = InMemoryAgentKernelStore(self.events)
        self.jwks = self.authority.jwks()
        self.verifier = AgentControlPermitVerifier(
            self.jwks, cache_max_age_seconds=verifier_cache_max_age
        )
        self.adapter = adapter or FakeAdapter()
        from ksadk.kernel.control import AgentKernel

        self.kernel = AgentKernel(
            self.store,
            self.events,
            self.verifier,
            queue_limit=queue_limit,
            capabilities=self.adapter.capabilities,
            clock=lambda: CLOCK_AT,
        )
        self._seed_task = _seed(self.session_service, *sessions)

    async def setup(self) -> "KernelStack":
        await self._seed_task
        return self

    def permit(self, *operations: str, **kwargs) -> AgentControlPermit:
        ops = operations or ("enqueue",)
        return self.authority.permit(operations=ops, **kwargs)

    async def lease(self, session_id: str = "s1", activation_id: str = "act-1") -> ActivationLease:
        return await self.store.acquire_activation(
            ActivationLeaseRequest(
                agent_instance_id=AGENT,
                session_id=session_id,
                activation_id=activation_id,
                runtime_type="fake",
                bundle_digest="bundle-1",
                capability_digest="capdigest-1",
                lease_ttl_seconds=60.0,
            )
        )


async def kernel_stack(**kwargs) -> KernelStack:
    return await KernelStack(**kwargs).setup()
