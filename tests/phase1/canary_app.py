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
from typing import Mapping

import asyncpg
import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from ksadk.events.session_event import SessionServiceEventStore
from ksadk.kernel.authorization import (
    AgentControlPermitVerifier,
    b64url_encode,
    sign_permit,
)
from ksadk.kernel.contract_fingerprints import AGENT_KERNEL_V1_AGGREGATE_DIGEST
from ksadk.kernel.contracts import (
    AgentControlPermit,
    AgentStatusQuery,
    RuntimeCapabilityMatrix,
    SessionEventSubscription,
)
from ksadk.kernel.control import AgentKernel, default_capability_matrix
from ksadk.kernel.ingress import (
    KERNEL_INGRESS_SUBMIT_PATH,
    agent_kernel_router,
    kernel_ingress_enabled,
)
from ksadk.kernel.postgres_store import (
    PostgresAgentKernelStore,
    PostgresKernelEventLog,
)
from ksadk.runtime.adapter import (
    CancelResult,
    PauseResult,
    RunHandle,
    RuntimeAdapter,
    StartRequest,
)
from ksadk.sessions.postgres_service import PostgresSessionService


class StaticJwks:
    """Minimal JwksSource implementation (mirror of tests.kernel.control_harness)."""

    def __init__(self, keys: Mapping[str, str]) -> None:
        self._keys = dict(keys)

    async def fetch_verification_keys(self) -> Mapping[str, str]:
        return self._keys


CONTRACT_DIGEST = AGENT_KERNEL_V1_AGGREGATE_DIGEST
TENANT = "phase1-canary"

app = FastAPI(title="agent-kernel-phase1-canary")

if kernel_ingress_enabled():
    app.include_router(agent_kernel_router())

    @app.middleware("http")
    async def _ensure_kernel_session(request, call_next):
        """canonical submit 前确保 session 存在（共享 event log 的前置条件）。"""
        if request.method == "POST" and request.url.path == KERNEL_INGRESS_SUBMIT_PATH:
            body = await request.body()
            try:
                session_id = str(
                    (json.loads(body) or {}).get("command", {}).get("session_id") or ""
                )
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
            nonce=f"phase1-canary-{agent_instance_id}-{uuid.uuid4().hex[:8]}",
            key_id=self.key_id,
            claims_digest=claims_digest,
            signature="",
        )
        return unsigned.model_copy(update={"signature": sign_permit(unsigned, self._private)})


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
                            activation_id=activation_id(),
                            runtime_type="canary-echo",
                            bundle_digest="phase1-canary",
                            capability_digest="phase1-canary",
                            lease_ttl_seconds=lease_ttl_seconds(),
                        )
                    )
                except Exception:
                    continue  # lease 被其它 owner 持有（split-brain 场景预期）
                result = await worker.run_once(
                    instance_id(), lease, session_id=session_id
                )
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


def activation_id() -> str:
    """Return the workload owner identity used by fencing drills.

    A constant per-instance value makes a replacement Pod look like the old
    owner and turns Pod-kill into a same-owner renew. Hosted canaries therefore
    require the downward-API Pod UID; local runs use a process-scoped fallback.
    """

    return os.environ.get("POD_UID") or f"local-canary-{os.getpid()}"


def lease_ttl_seconds() -> float:
    return float(os.environ.get("AGENT_KERNEL_LEASE_TTL_SECONDS", "30"))


def store_namespace() -> str:
    return os.environ.get("AGENT_KERNEL_STORE_NAMESPACE", "default").strip() or "default"


def _require_test_hooks() -> None:
    """Keep destructive drill helpers absent from non-canary deployments."""

    from fastapi import HTTPException

    if os.environ.get("PHASE1_CANARY_TEST_HOOKS") != "1":
        raise HTTPException(status_code=404, detail="not found")


@app.on_event("startup")
async def startup() -> None:
    dsn = os.environ["AGENT_KERNEL_STORE_DSN"]
    namespace = store_namespace()
    service = PostgresSessionService(
        dsn=dsn,
        namespace=namespace,
        tenant_id=TENANT,
        workspace_id=instance_id(),
    )
    await service._ensure_pool()
    _state["session_service"] = service
    event_log = PostgresKernelEventLog(
        service._pool,
        namespace=namespace,
        tenant_id=TENANT,
        workspace_id=instance_id(),
    )
    kstore = PostgresAgentKernelStore(
        service._pool,
        event_log,
        tenant_id=TENANT,
    )
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
        _state["server_kernel"] = AgentKernel(kstore, events, server_verifier, queue_limit=100)
    _state["store"] = kstore
    _state["authority"] = auth
    _state["pool"] = service._pool
    _state["worker_task"] = asyncio.create_task(_worker_loop())
    # canonical /agent-kernel/v1/* ingress 使用的 kernel（同 PG store/events）。
    from ksadk.kernel.ingress import bootstrap_agent_kernel_from_env

    await bootstrap_agent_kernel_from_env()


@app.on_event("shutdown")
async def shutdown() -> None:
    task = _state.get("worker_task")
    if isinstance(task, asyncio.Task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    service = _state.get("session_service")
    if isinstance(service, PostgresSessionService):
        await service.aclose()
    _state.clear()


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
            agent_id=instance_id(),
            user_id="phase1-canary",
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
    _require_test_hooks()
    _state["worker_enabled"] = bool(body.get("enabled", True))
    return {"worker_enabled": _state["worker_enabled"]}


@app.get("/test/snapshot")
async def test_snapshot(session_id: str) -> dict:
    """Return a redacted, session-scoped durable-state snapshot for drills."""

    _require_test_hooks()
    pool: asyncpg.Pool = _state["pool"]  # type: ignore[assignment]
    inbox = await pool.fetch(
        "SELECT message_id::text AS message_id, accepted_seq, status, claimed_fence"
        " FROM kernel_inbox WHERE agent_instance_id=$1 AND session_id=$2"
        " ORDER BY accepted_seq",
        instance_id(),
        session_id,
    )
    activation = await pool.fetchrow(
        "SELECT activation_id, fencing_token, lease_expires_at, released"
        " FROM kernel_activations WHERE agent_instance_id=$1 AND session_id=$2",
        instance_id(),
        session_id,
    )
    runs = await pool.fetch(
        "SELECT run_id, state, activation_fence FROM kernel_runs"
        " WHERE agent_instance_id=$1 AND session_id=$2 ORDER BY created_at",
        instance_id(),
        session_id,
    )
    envelopes = await store()._events.read(session_id, 0, 10000)
    return {
        "instance_id": instance_id(),
        "pod_uid": activation_id(),
        "session_id": session_id,
        "inbox": [dict(row) for row in inbox],
        "activation": (
            {
                "activation_id": str(activation["activation_id"]),
                "fencing_token": int(activation["fencing_token"]),
                "lease_expires_at": activation["lease_expires_at"].isoformat(),
                "released": bool(activation["released"]),
            }
            if activation is not None
            else None
        ),
        "runs": [dict(row) for row in runs],
        "events": [
            {
                "event_id": str(envelope.event_id),
                "seq": int(envelope.seq),
                "family": envelope.family,
                "event_type": envelope.event_type,
                "payload": {
                    key: envelope.payload[key]
                    for key in (
                        "command_id",
                        "message_id",
                        "fencing_token",
                        "status",
                        "run_id",
                    )
                    if key in envelope.payload
                },
            }
            for envelope in envelopes
        ],
    }


@app.post("/test/drills/stale-fence")
async def test_stale_fence(body: dict) -> dict:
    """Force a logical takeover while the old writer remains connected."""

    _require_test_hooks()
    from ksadk.kernel.errors import StaleFenceError
    from ksadk.kernel.store import ActivationLeaseRequest, RunRecord, RunState

    session_id = str(body.get("session_id") or f"fence-{uuid.uuid4().hex[:12]}")
    old_owner = f"old-{uuid.uuid4().hex[:12]}"
    new_owner = f"new-{uuid.uuid4().hex[:12]}"

    service: PostgresSessionService = _state["session_service"]  # type: ignore[assignment]
    if await service.get_session(session_id) is None:
        await service.create_session(
            agent_id=instance_id(),
            user_id="phase1-canary",
            session_id=session_id,
        )

    def request(owner: str) -> ActivationLeaseRequest:
        return ActivationLeaseRequest(
            agent_instance_id=instance_id(),
            session_id=session_id,
            activation_id=owner,
            runtime_type="canary-echo",
            bundle_digest="phase1-canary",
            capability_digest=CONTRACT_DIGEST,
            lease_ttl_seconds=lease_ttl_seconds(),
        )

    old_lease = await store().acquire_activation(request(old_owner))
    pool: asyncpg.Pool = _state["pool"]  # type: ignore[assignment]
    await pool.execute(
        "UPDATE kernel_activations SET lease_expires_at=now()-interval '1 second'"
        " WHERE agent_instance_id=$1 AND session_id=$2 AND activation_id=$3",
        instance_id(),
        session_id,
        old_owner,
    )
    new_lease = await store().acquire_activation(request(new_owner))
    new_run = RunRecord(
        run_id=f"run-{uuid.uuid4().hex[:12]}",
        agent_instance_id=instance_id(),
        session_id=session_id,
        state=RunState.RUNNING,
        activation_fence=new_lease.fencing_token,
    )
    stored = await store().save_run_transition(new_run, expected_fence=new_lease.fencing_token)
    stale_rejected = False
    try:
        await store().save_run_transition(
            new_run.model_copy(update={"state": RunState.PAUSED}),
            expected_fence=old_lease.fencing_token,
        )
    except StaleFenceError:
        stale_rejected = True
    if not stale_rejected:
        raise RuntimeError("old activation unexpectedly wrote after takeover")
    return {
        "session_id": session_id,
        "command_id": stored.run_id,
        "event_id": stored.run_id,
        "old_activation_id": old_owner,
        "new_activation_id": new_owner,
        "old_fencing_token": old_lease.fencing_token,
        "new_fencing_token": new_lease.fencing_token,
        "stale_writer_rejected": True,
    }


@app.post("/test/cleanup")
async def test_cleanup() -> dict:
    """Delete only rows owned by this ephemeral canary instance/namespace."""

    _require_test_hooks()
    pool: asyncpg.Pool = _state["pool"]  # type: ignore[assignment]
    permit_cache = _state.get("permit_cache")
    nonce_prefix = f"phase1-canary-{instance_id()}-"
    async with pool.acquire() as connection:
        async with connection.transaction():
            session_rows = await connection.fetch(
                "SELECT session_id FROM kernel_inbox WHERE agent_instance_id=$1"
                " UNION SELECT session_id FROM kernel_runs WHERE agent_instance_id=$1"
                " UNION SELECT session_id FROM kernel_activations WHERE agent_instance_id=$1",
                instance_id(),
            )
            sessions = [str(row["session_id"]) for row in session_rows]
            await connection.execute(
                "DELETE FROM kernel_interaction_submissions WHERE tenant_id=$1"
                " AND interaction_id IN (SELECT interaction_id FROM kernel_interactions"
                " WHERE tenant_id=$1 AND agent_instance_id=$2)",
                TENANT,
                instance_id(),
            )
            await connection.execute(
                "DELETE FROM kernel_interactions WHERE tenant_id=$1 AND agent_instance_id=$2",
                TENANT,
                instance_id(),
            )
            for table in ("kernel_inbox", "kernel_runs", "kernel_activations"):
                await connection.execute(
                    f"DELETE FROM {table} WHERE agent_instance_id=$1",
                    instance_id(),
                )
            if sessions:
                await connection.execute(
                    "DELETE FROM kernel_accepted_seq WHERE tenant_id=$1"
                    " AND session_id=ANY($2::text[])",
                    TENANT,
                    sessions,
                )
            deleted_nonces = await connection.fetchval(
                "WITH deleted AS (DELETE FROM kernel_permit_nonces"
                " WHERE position($1 in nonce)=1 RETURNING 1) SELECT count(*) FROM deleted",
                nonce_prefix,
            )
            await connection.execute(
                "DELETE FROM ksadk_states WHERE namespace=$1"
                " AND (agent_id=$2 OR session_id=ANY($3::text[]))",
                store_namespace(),
                instance_id(),
                sessions,
            )
            await connection.execute(
                "DELETE FROM ksadk_sessions WHERE namespace=$1 AND agent_id=$2",
                store_namespace(),
                instance_id(),
            )
    if isinstance(permit_cache, dict):
        permit_cache.clear()
    return {
        "instance_id": instance_id(),
        "sessions_deleted": len(sessions),
        "permit_nonces_deleted": int(deleted_nonces or 0),
    }


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

    command_id = uuid.UUID(str(body.get("command_id") or uuid.uuid4()))
    idempotency_key = body.get("idempotency_key") or f"key-{uuid.uuid4().hex[:12]}"
    service: PostgresSessionService = _state["session_service"]  # type: ignore[assignment]
    if await service.get_session(body["session_id"]) is None:
        await service.create_session(
            agent_id=instance_id(),
            user_id="phase1-canary",
            session_id=body["session_id"],
        )
    permit_cache: dict[tuple[str, str], AgentControlPermit] = _state.setdefault(  # type: ignore[assignment]
        "permit_cache", {}
    )
    permit_key = (str(command_id), str(idempotency_key))
    permit = permit_cache.get(permit_key)
    if permit is None:
        permit = authority().permit(
            agent_instance_id=instance_id(),
            session_id=body["session_id"],
            operations=[body.get("command_type", "enqueue")],
        )
        permit_cache[permit_key] = permit
    command = AgentControlCommand(
        command_id=command_id,
        idempotency_key=idempotency_key,
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
