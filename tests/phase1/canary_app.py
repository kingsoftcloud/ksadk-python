# -*- coding: utf-8 -*-
"""Phase 1 agent-kernel canary runtime app.

Minimal FastAPI surface exposing the kernel control contract:
- GET  /healthz
- GET  /v1/meta/contract-digest
- POST /v1/actions/SubmitAgentControl
- GET  /v1/actions/GetAgentStatus
- GET  /v1/actions/SubscribeSessionEvents (SSE)

Backing store: ksadk.kernel AgentKernel + PostgresAgentKernelStore.
DSN comes from env AGENT_KERNEL_STORE_DSN; instance from AGENT_INSTANCE_ID.
Permits are self-issued by the canary (local Ed25519 authority) — sufficient
for infrastructure canary purposes only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from ksadk.events.session_event import SessionServiceEventStore
from typing import Mapping

from ksadk.kernel.authorization import (
    AgentControlPermitVerifier,
    b64url_encode,
    sign_permit,
)


class StaticJwks:
    """Minimal JwksSource implementation (mirror of tests.kernel.control_harness)."""

    def __init__(self, keys: Mapping[str, str]) -> None:
        self._keys = dict(keys)

    async def fetch_verification_keys(self) -> Mapping[str, str]:
        return self._keys
from ksadk.kernel.contracts import (
    AgentControlPermit,
    AgentStatusQuery,
    SessionEventSubscription,
)
from ksadk.kernel.control import AgentKernel, default_capability_matrix
from ksadk.runtime.adapter import (
    CancelResult,
    PauseResult,
    RunHandle,
    RuntimeAdapter,
    StartRequest,
)
from ksadk.kernel.contracts import RuntimeCapabilityMatrix
from ksadk.kernel.postgres_store import (
    PostgresAgentKernelStore,
    PostgresKernelEventLog,
)
from ksadk.sessions.postgres_service import PostgresSessionService

CONTRACT_DIGEST = "69771d8df4a8811ed6623f26a152c869cfdfd8dbfdcad443b52a9b6403b267e8"
TENANT = "phase1-canary"

app = FastAPI(title="agent-kernel-phase1-canary")

# canonical kernel ingress (/agent-kernel/v1/*)：gateway 转发与 server runtime
# client 的目标路径，通过 bootstrap_agent_kernel_from_env 装配的同一 kernel。
from ksadk.kernel.ingress import agent_kernel_router, kernel_ingress_enabled

if kernel_ingress_enabled():
    app.include_router(agent_kernel_router())

    from ksadk.kernel.ingress import KERNEL_INGRESS_SUBMIT_PATH

    @app.middleware("http")
    async def _ensure_kernel_session(request, call_next):
        """canonical submit 前确保 session 存在（共享 event log 的前置条件）。"""
        if request.method == "POST" and request.url.path == KERNEL_INGRESS_SUBMIT_PATH:
            body = await request.body()
            try:
                session_id = str((json.loads(body) or {}).get("command", {}).get("session_id") or "")
            except Exception:
                session_id = ""
            if session_id and _state.get("session_service") is not None:
                service = _state["session_service"]
                if await service.get_session(session_id) is None:
                    await service.create_session(
                        agent_id=instance_id(),
                        user_id="phase1-canary",
                        session_id=session_id,
                    )
            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}
            request._receive = receive
        return await call_next(request)


class _CanaryAuthority:
    def __init__(self) -> None:
        self.key_id = "phase1-canary-key"
        self._private = Ed25519PrivateKey.generate()
        self._public = self._private.public_key()

    def jwks(self) -> StaticJwks:
        return StaticJwks({self.key_id: b64url_encode(self._public.public_bytes_raw())})

    def permit(
        self,
        *,
        agent_instance_id: str,
        session_id: str | None,
        operations: list[str],
    ) -> AgentControlPermit:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        claims = {
            "subject": "phase1-canary",
            "operations": operations,
            "session_id": session_id,
        }
        claims_digest = hashlib.sha256(
            json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        unsigned = AgentControlPermit(
            permit_id=f"permit-{uuid.uuid4().hex[:8]}",
            subject_ref="phase1-canary",
            tenant_id=TENANT,
            agent_instance_id=agent_instance_id,
            session_id=session_id,
            allowed_operations=operations,
            issued_at=now.isoformat(),
            expires_at=(now + timedelta(minutes=5)).isoformat(),
            nonce=f"nonce-{uuid.uuid4().hex[:8]}",
            key_id=self.key_id,
            claims_digest=claims_digest,
            signature="",
        )
        return unsigned.model_copy(
            update={"signature": sign_permit(unsigned, self._private)}
        )


_state: dict[str, object] = {}


class _EchoRuntime:
    runtime_type = "canary-echo"

    def native_capabilities(self) -> dict:
        return {}


class EchoAdapter(RuntimeAdapter):
    """最小记录型 adapter：start 即完成一个 turn，供 worker 消费 enqueue。"""

    def __init__(self) -> None:
        super().__init__(_EchoRuntime())
        self.calls: list[tuple[str, str]] = []

    def capabilities(self) -> RuntimeCapabilityMatrix:
        # 保持诚实默认：steer/pause 等在 submit 层 unsupported。
        return default_capability_matrix()

    async def start(self, request: StartRequest) -> RunHandle:
        self.calls.append(("start", request.session_id))
        return RunHandle(
            run_id=f"run-{uuid.uuid4().hex[:8]}",
            session_id=request.session_id,
            runtime_type="canary-echo",
        )

    def stream(self, handle: RunHandle) -> object:
        """产生 run.started/run.progress 事实流，供 worker 落 family=runtime/v2。"""

        from ksadk.events.canonical import RunProgress, RunStarted, SourceRef

        run_id = handle.run_id
        source = SourceRef(framework="ksadk")

        async def _events():
            yield RunStarted(
                schema_version=2,
                event_id=f"{run_id}-started",
                seq=0,
                timestamp=time.time(),
                run_id=run_id,
                scope_id=f"run:{run_id}",
                status="running",
                source=source,
            )
            yield RunProgress(
                schema_version=2,
                event_id=f"{run_id}-progress",
                seq=0,
                timestamp=time.time(),
                run_id=run_id,
                scope_id=f"run:{run_id}",
                status="running",
                progress=1.0,
                message="phase1 canary echo turn complete",
                source=source,
            )

        return _events()

    async def cancel(self, handle: RunHandle) -> CancelResult:
        return CancelResult.INTERRUPTED_ACTIVE_TURN

    async def pause(self, handle: RunHandle) -> PauseResult:
        return PauseResult.PAUSED_ACTIVE_TURN

    async def submit(self, handle: RunHandle, payload: object) -> None:
        return None

    async def resume(self, handle: RunHandle, target: object, payload: object) -> RunHandle:
        return handle

    async def checkpoint(self, handle: RunHandle) -> None:
        raise NotImplementedError

    async def close(self, handle: RunHandle) -> None:
        return None


async def _worker_loop() -> None:
    """后台 per-session FIFO worker：acquire lease -> AgentKernelWorker.run_once。"""
    from ksadk.kernel.store import ActivationLeaseRequest
    from ksadk.kernel.worker import AgentKernelWorker

    pool: asyncpg.Pool = _state["pool"]  # type: ignore[assignment]
    adapter = EchoAdapter()
    worker = AgentKernelWorker(
        store(),
        adapter_factory=lambda: adapter,
        session_events=_state["events"],
    )
    kstore = store()
    while True:
        try:
            if not _state.get("worker_enabled", True):
                await asyncio.sleep(0.2)
                continue
            rows = await pool.fetch(
                "SELECT DISTINCT session_id FROM kernel_inbox"
                " WHERE agent_instance_id=$1 AND status='accepted'",
                instance_id(),
            )
            progressed = False
            for row in rows:
                session_id = row["session_id"]
                service = _state["session_service"]
                if await service.get_session(session_id) is None:
                    await service.create_session(
                        agent_id=instance_id(),
                        user_id="phase1-canary",
                        session_id=session_id,
                    )
                try:
                    lease = await kstore.acquire_activation(
                        ActivationLeaseRequest(
                            agent_instance_id=instance_id(),
                            session_id=session_id,
                            activation_id=f"canary-worker-{instance_id()}",
                            runtime_type="canary-echo",
                            bundle_digest="phase1-canary",
                            capability_digest="phase1-canary",
                            lease_ttl_seconds=120.0,
                        )
                    )
                except Exception:
                    continue  # lease 被其它 owner 持有（split-brain 场景预期）
                result = await worker.run_once(instance_id(), lease)
                if result.outcome != "idle":
                    progressed = True
            if not progressed:
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(1.0)


def kernel() -> AgentKernel:
    return _state["kernel"]  # type: ignore[return-value]


def store() -> PostgresAgentKernelStore:
    return _state["store"]  # type: ignore[return-value]


def authority() -> _CanaryAuthority:
    return _state["authority"]  # type: ignore[return-value]


def instance_id() -> str:
    return os.environ.get("AGENT_INSTANCE_ID", "phase1-canary-1")


@app.on_event("startup")
async def startup() -> None:
    dsn = os.environ["AGENT_KERNEL_STORE_DSN"]
    service = PostgresSessionService(dsn=dsn)
    await service._ensure_pool()
    _state["session_service"] = service
    event_log = PostgresKernelEventLog(service._pool)
    kstore = PostgresAgentKernelStore(service._pool, event_log)
    await kstore.ensure_schema()
    from ksadk.kernel.postgres_store import PostgresNonceStore

    auth = _CanaryAuthority()
    events = SessionServiceEventStore(service)
    verifier = AgentControlPermitVerifier(
        auth.jwks(), nonce_store=PostgresNonceStore(service._pool)
    )
    _state["events"] = events
    _state["kernel"] = AgentKernel(
        kstore,
        events,
        verifier,
        queue_limit=100,
    )
    # Server AgentControl ingress 验签器：trusted JWKS + durable nonce store。
    jwks_raw = os.environ.get("AGENT_CONTROL_TRUSTED_JWKS", "").strip()
    if jwks_raw:
        keys = {k["kid"]: k["x"] for k in json.loads(jwks_raw).get("keys", [])}
        server_verifier = AgentControlPermitVerifier(
            StaticJwks(keys), nonce_store=PostgresNonceStore(service._pool)
        )
        _state["server_kernel"] = AgentKernel(
            kstore, events, server_verifier, queue_limit=100
        )
    _state["store"] = kstore
    _state["authority"] = auth
    _state["pool"] = service._pool
    _state["worker_task"] = asyncio.create_task(_worker_loop())
    # canonical /agent-kernel/v1/* ingress 使用的 kernel（同 PG store/events）。
    from ksadk.kernel.ingress import bootstrap_agent_kernel_from_env

    await bootstrap_agent_kernel_from_env()


def _b64url_decode(value: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _parse_rfc3339(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


@app.post("/agentengine/api/v1/agent-control/submit")
async def server_agent_control_submit(body: dict) -> dict:
    """Server AgentControlChannel/v1 ingress：接受 Server admission 签发的 permit。

    body: {"command": {...}, "permit": {...}}（Server AgentControlRuntimeClient 格式）。
    验签走 AgentControlPermitVerifier（trusted JWKS 来自 env
    AGENT_CONTROL_TRUSTED_JWKS，形如 {"keys":[{"kid","x"}]}）：签名、时效
    （issued <= now、TTL <= 300s）、operation/tenant/instance/session/nonce
    绑定与 authorization_ref == permit_id 全部在 verifier 内 fail closed。
    通过后直接以 server permit 进入本地 kernel：accepted 事件的
    AdmissionWriteGuard 绑定 server permit_id，receipt 保持 server 语义，
    不再本地重签 permit。
    """
    from fastapi import HTTPException

    from ksadk.kernel.contracts import AgentControlCommand, AgentControlPermit
    from ksadk.kernel.errors import InvalidPermitError

    if "server_kernel" not in _state:
        raise HTTPException(status_code=503, detail="trusted jwks not configured")

    command, permit_raw = body["command"], body["permit"]
    try:
        server_permit = AgentControlPermit.model_validate(permit_raw)
    except Exception as exc:
        raise HTTPException(status_code=403, detail="invalid permit payload") from exc
    if str(server_permit.agent_instance_id) != instance_id():
        raise HTTPException(status_code=403, detail="instance mismatch")

    service: PostgresSessionService = _state["session_service"]  # type: ignore[assignment]
    session_id = str(command["session_id"])
    if await service.get_session(session_id) is None:
        await service.create_session(
            agent_id=instance_id(), user_id="phase1-canary",
            session_id=session_id,
        )
    kernel_command = AgentControlCommand(
        command_id=uuid.UUID(str(command["command_id"])),
        idempotency_key=str(command["idempotency_key"]),
        tenant_id=str(command["tenant_id"]),
        agent_instance_id=str(command["agent_instance_id"]),
        session_id=session_id,
        command_type=str(command["command_type"]),
        payload=command.get("payload", {"content": {"text": "hi"}}),
        source=command.get("source") or {"kind": "workflow", "ref": "server"},
        authorization_ref=server_permit.permit_id,
        submitted_at=str(command["submitted_at"]),
    )
    server_kernel: "AgentKernel" = _state["server_kernel"]  # type: ignore[assignment]
    # kernel 内部 verifier：签名/时效/绑定/authorization_ref/nonce 一次收口。
    try:
        receipt = await server_kernel.submit(kernel_command, permit=server_permit)
    except InvalidPermitError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if receipt.status == "rejected" and receipt.error is not None:
        raise HTTPException(status_code=403, detail=receipt.error.message)
    out = receipt.model_dump(mode="json", by_alias=True)
    out["request_id"] = f"req-{uuid.uuid4().hex[:16]}"
    return out


@app.post("/test/worker")
async def toggle_worker(body: dict) -> dict:
    """测试用：临时停/启 per-session FIFO worker（queue_full 等场景需要）。"""
    _state["worker_enabled"] = bool(body.get("enabled", True))
    return {"worker_enabled": _state["worker_enabled"]}


@app.get("/healthz")
async def healthz() -> dict:
    pool: asyncpg.Pool = _state["pool"]  # type: ignore[assignment]
    await pool.fetchval("SELECT 1")
    return {"ok": True, "instance": instance_id()}


@app.get("/v1/meta/contract-digest")
async def contract_digest() -> dict:
    return {"digest": CONTRACT_DIGEST, "instance": instance_id()}


@app.post("/v1/actions/SubmitAgentControl")
async def submit_agent_control(body: dict) -> dict:
    from ksadk.kernel.contracts import AgentControlCommand

    service: PostgresSessionService = _state["session_service"]  # type: ignore[assignment]
    if await service.get_session(body["session_id"]) is None:
        await service.create_session(
            agent_id=instance_id(), user_id="phase1-canary",
            session_id=body["session_id"],
        )
    permit = authority().permit(
        agent_instance_id=instance_id(),
        session_id=body["session_id"],
        operations=[body.get("command_type", "enqueue")],
    )
    command = AgentControlCommand(
        command_id=uuid.uuid4(),
        idempotency_key=body.get("idempotency_key") or f"key-{uuid.uuid4().hex[:12]}",
        tenant_id=TENANT,
        agent_instance_id=instance_id(),
        session_id=body["session_id"],
        command_type=body.get("command_type", "enqueue"),
        payload=body.get("payload", {"content": {"text": body.get("text", "hi")}}),
        source={"kind": "workflow", "ref": "canary"},
        authorization_ref=permit.permit_id,
        submitted_at=datetime.now(timezone.utc).isoformat(),
    )
    receipt = await kernel().submit(command, permit=permit)
    return receipt.model_dump(mode="json", by_alias=True)


@app.get("/v1/actions/GetAgentStatus")
async def get_agent_status(session_id: str | None = None) -> dict:
    permit = authority().permit(
        agent_instance_id=instance_id(),
        session_id=session_id,
        operations=["get_status"],
    )
    query = AgentStatusQuery(
        tenant_id=TENANT,
        agent_instance_id=instance_id(),
        authorization_ref=permit.permit_id,
        session_id=session_id,
    )
    snapshot = await kernel().status(query, permit=permit)
    return snapshot.model_dump(mode="json", by_alias=True)


@app.get("/v1/actions/SubscribeSessionEvents")
async def subscribe_session_events(session_id: str, after_seq: int = 0):
    permit = authority().permit(
        agent_instance_id=instance_id(),
        session_id=session_id,
        operations=["subscribe_events"],
    )
    subscription = SessionEventSubscription(
        tenant_id=TENANT,
        agent_instance_id=instance_id(),
        session_id=session_id,
        authorization_ref=permit.permit_id,
        after_seq=after_seq,
    )

    async def gen():
        async for envelope in kernel().subscribe(subscription, permit=permit):
            data = envelope.model_dump(mode="json", by_alias=True)
            yield f"data: {json.dumps(data)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
