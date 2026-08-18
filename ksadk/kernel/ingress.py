# -*- coding: utf-8 -*-
"""Agent Kernel ingress 收敛层（Phase 1 Task 8）。

把 KsADK 现有五个入口（RunAgent / Responses / AG-UI / A2A / Studio）的
mutation 统一收敛到 ``AgentKernel.submit``：

- **opt-in 灰度**：只有 ``KSADK_AGENT_KERNEL=1`` 且进程内注册了 kernel
  （``set_agent_kernel``）时才走 kernel 路径；默认保持旧 executor 路径，
  保证既有 public fixtures 不破。
- **mapper 只做 public request -> canonical command**：tenant / agent_instance /
  authorization_ref 全部由 trusted runtime context 注入，不来自 public payload。
  Responses request id、A2A task id、AG-UI run id 等保存为 correlation/source
  ref，不改变 Session/Run canonical identity。
- **receipt -> HTTP**：``RECEIPT_HTTP_STATUS`` 是唯一映射表。
- **统一 cursor**：kernel 路径的 SSE 一律从
  ``SessionEventSubscription(after_seq)`` 读取，reconnect cursor 源自同一
  Session seq；各协议自己的 event shape 由 surface 内的 public projector
  保留，禁止第二个自增序列。

kernel 路径下命令的实际执行由 ``AgentWorker``（Task 6/7 交付）认领并驱动
RuntimeAdapter；ingress 只 submit + 订阅投影，不直接触碰 RuntimeExecutor。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ksadk.kernel.authorization import (
    AgentControlPermitVerifier,
    JwksSource,
    sign_permit,
)
from ksadk.kernel.contracts import (
    AgentControlCommand,
    AgentControlPermit,
    AgentControlReceipt,
    ControlSource,
    SessionEventEnvelope,
    SessionEventSubscription,
)

# ---------------------------------------------------------------------------
# opt-in 开关与 kernel 注册
# ---------------------------------------------------------------------------

ENV_KERNEL_ENABLED = "KSADK_AGENT_KERNEL"
# Operator 注入的开关名（AGENT_KERNEL_ENABLED=1）；与 SDK 本地灰度开关等价。
ENV_KERNEL_ENABLED_PLATFORM = "AGENT_KERNEL_ENABLED"

_TRUTHY = {"1", "true", "yes", "on"}

_kernel: Any | None = None


def kernel_ingress_enabled() -> bool:
    """kernel 路径是灰度 opt-in：默认关闭，旧路径不变。

    认 ``KSADK_AGENT_KERNEL``（SDK 本地）或 ``AGENT_KERNEL_ENABLED``
    （Operator 平台注入）任一为真。
    """

    for name in (ENV_KERNEL_ENABLED, ENV_KERNEL_ENABLED_PLATFORM):
        if os.environ.get(name, "").strip().lower() in _TRUTHY:
            return True
    return False


def set_agent_kernel(kernel: Any) -> None:
    """注册进程级 AgentKernel（server bootstrap / 测试 harness 调用）。"""

    global _kernel
    _kernel = kernel


def clear_agent_kernel() -> None:
    global _kernel
    _kernel = None


def get_agent_kernel() -> Any | None:
    return _kernel


def kernel_route_active() -> bool:
    """当前请求是否走 kernel ingress（开关开 且 kernel 已注册）。"""

    return kernel_ingress_enabled() and get_agent_kernel() is not None


# ---------------------------------------------------------------------------
# trusted runtime context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrustedRuntimeContext:
    """由 runtime 注入的信任事实；public request 永远不提供这些字段。"""

    tenant_id: str
    agent_instance_id: str
    source: ControlSource
    permit: AgentControlPermit
    received_at: str


class _LocalJwks:
    def __init__(self, key_id: str, public_b64: str) -> None:
        self._keys = {key_id: public_b64}

    async def fetch_verification_keys(self) -> Mapping[str, str]:
        return dict(self._keys)


class InProcessPermitIssuer:
    """本地 opt-in 模式的进程内签发方（Ed25519，密钥不落盘）。

    托管部署（agentengine-server Task 9+）会换成 server 签发的 permit；
    SDK 本地灰度只需要一个诚实的、可被同一个 kernel verifier 验签的 issuer。
    """

    # TTL 与 kernel verifier 的 PERMIT_MAX_TTL_SECONDS（300s）对齐；
    # 超过 300s 的 permit 在严格 verifier 下必然被拒。
    def __init__(self, *, ttl_seconds: int = 300, key_id: str = "ksadk-local-kernel") -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from ksadk.kernel.authorization import b64url_encode

        self._private = Ed25519PrivateKey.generate()
        self.key_id = key_id
        self._public_b64 = b64url_encode(self._private.public_key().public_bytes_raw())
        self._ttl = int(ttl_seconds)
        self._jwks: JwksSource = _LocalJwks(key_id, self._public_b64)

    def verifier(self, **kwargs: Any) -> AgentControlPermitVerifier:
        return AgentControlPermitVerifier(self._jwks, **kwargs)

    def issue(
        self,
        *,
        tenant_id: str,
        agent_instance_id: str,
        operations: tuple[str, ...] | list[str],
        session_id: str | None = None,
        subject_ref: str = "ksadk-local-runtime",
        now: datetime | None = None,
    ) -> AgentControlPermit:
        issued = now or datetime.now(UTC)
        expires = issued + timedelta(seconds=self._ttl)
        claims = {
            "tenant_id": tenant_id,
            "agent_instance_id": agent_instance_id,
            "session_id": session_id,
            "operations": sorted(operations),
        }
        unsigned = AgentControlPermit(
            permit_id=f"permit_{uuid.uuid4().hex}",
            subject_ref=subject_ref,
            tenant_id=tenant_id,
            agent_instance_id=agent_instance_id,
            session_id=session_id,
            allowed_operations=list(operations),
            issued_at=_rfc3339(issued),
            expires_at=_rfc3339(expires),
            nonce=uuid.uuid4().hex,
            key_id=self.key_id,
            claims_digest=hashlib.sha256(
                json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            signature="",
        )
        return unsigned.model_copy(update={"signature": sign_permit(unsigned, self._private)})


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def trusted_context(
    *,
    source_kind: str,
    source_ref: str,
    tenant_id: str = "local",
    agent_instance_id: str = "local-agent",
    session_id: str | None = None,
    operations: tuple[str, ...] | list[str] = ("enqueue",),
    issuer: InProcessPermitIssuer | None = None,
    launch_context: Any | None = None,
) -> TrustedRuntimeContext:
    """从 trusted runtime 侧（env / launch config）构造上下文并签 permit。"""

    if launch_context is not None:
        config = getattr(launch_context, "config", None) or {}
        tenant_id = str(config.get("tenant_id") or tenant_id)
        agent_instance_id = str(
            config.get("agent_instance_id") or agent_instance_id
        )
    issuer = issuer or _default_issuer()
    permit = issuer.issue(
        tenant_id=tenant_id,
        agent_instance_id=agent_instance_id,
        operations=operations,
        session_id=session_id,
    )
    return TrustedRuntimeContext(
        tenant_id=tenant_id,
        agent_instance_id=agent_instance_id,
        source=ControlSource(kind=source_kind, ref=source_ref),
        permit=permit,
        received_at=_rfc3339(datetime.now(UTC)),
    )


_default_issuer_singleton: InProcessPermitIssuer | None = None


def _default_issuer() -> InProcessPermitIssuer:
    global _default_issuer_singleton
    if _default_issuer_singleton is None:
        _default_issuer_singleton = InProcessPermitIssuer()
    return _default_issuer_singleton


# ---------------------------------------------------------------------------
# receipt -> HTTP
# ---------------------------------------------------------------------------

RECEIPT_HTTP_STATUS: dict[str, int] = {
    "accepted": 202,
    "duplicate": 200,
    "rejected": 400,
    "unsupported": 409,
    "queue_full": 429,
    "persistence_uncertain": 503,
}


def receipt_http_status(receipt: AgentControlReceipt) -> int:
    return RECEIPT_HTTP_STATUS.get(receipt.status, 400)


def receipt_response_headers(receipt: AgentControlReceipt) -> dict[str, str]:
    """新 header：contract/capability 语义由 kernel digest header 承载。"""

    headers = {
        "X-Ksadk-Agent-Kernel": "1",
        "X-Ksadk-Control-Status": receipt.status,
        "X-Ksadk-Command-Id": str(receipt.command_id),
    }
    if receipt.message_id is not None:
        headers["X-Ksadk-Control-Message-Id"] = str(receipt.message_id)
    return headers


def receipt_error_payload(receipt: AgentControlReceipt) -> dict[str, Any]:
    error = receipt.error
    return {
        "Code": (error.code if error else receipt.status),
        "Message": (error.message if error else receipt.status),
        "Retryable": bool(error.retryable) if error else False,
        "ControlStatus": receipt.status,
        "CommandId": str(receipt.command_id),
    }


# ---------------------------------------------------------------------------
# mappers: public request -> AgentControlCommand
# ---------------------------------------------------------------------------


def _command(
    *,
    trusted: TrustedRuntimeContext,
    command_type: str,
    session_id: str,
    idempotency_key: str,
    payload: dict[str, Any],
    correlation_id: str | None = None,
) -> AgentControlCommand:
    return AgentControlCommand(
        command_id=uuid.uuid4(),
        idempotency_key=idempotency_key,
        tenant_id=trusted.tenant_id,
        agent_instance_id=trusted.agent_instance_id,
        session_id=session_id,
        command_type=command_type,
        payload=payload,
        source=trusted.source,
        authorization_ref=trusted.permit.permit_id,
        submitted_at=trusted.received_at,
        correlation_id=correlation_id,
    )


def map_run_request(
    *,
    session_id: str,
    idempotency_key: str,
    content: Any,
    invocation_id: str | None = None,
    trusted: TrustedRuntimeContext,
) -> AgentControlCommand:
    """RunAgent（agentengine API）-> enqueue。InvocationId 是 source/correlation ref。"""

    return _command(
        trusted=trusted,
        command_type="enqueue",
        session_id=session_id,
        idempotency_key=idempotency_key,
        payload={"content": content},
        correlation_id=invocation_id,
    )


def map_responses_request(
    *,
    session_id: str,
    idempotency_key: str,
    content: Any,
    response_id: str | None = None,
    trusted: TrustedRuntimeContext,
) -> AgentControlCommand:
    """OpenAI Responses 兼容入口 -> enqueue；response id 保存在 correlation ref。"""

    return _command(
        trusted=trusted,
        command_type="enqueue",
        session_id=session_id,
        idempotency_key=idempotency_key,
        payload={"content": content},
        correlation_id=response_id,
    )


def map_agui_request(
    *,
    session_id: str,
    idempotency_key: str,
    content: Any,
    run_id: str | None = None,
    trusted: TrustedRuntimeContext,
) -> AgentControlCommand:
    """AG-UI run -> enqueue；AG-UI run id 保存在 correlation ref。"""

    return _command(
        trusted=trusted,
        command_type="enqueue",
        session_id=session_id,
        idempotency_key=idempotency_key,
        payload={"content": content},
        correlation_id=run_id,
    )


def map_a2a_task(
    *,
    session_id: str,
    idempotency_key: str,
    content: Any,
    task_id: str | None = None,
    trusted: TrustedRuntimeContext,
) -> AgentControlCommand:
    """A2A task -> enqueue；A2A task id 保存在 correlation ref。"""

    return _command(
        trusted=trusted,
        command_type="enqueue",
        session_id=session_id,
        idempotency_key=idempotency_key,
        payload={"content": content},
        correlation_id=task_id,
    )


def map_studio_request(
    *,
    session_id: str,
    idempotency_key: str,
    content: Any,
    run_id: str | None = None,
    trusted: TrustedRuntimeContext,
) -> AgentControlCommand:
    """Studio run -> enqueue；studio run id 保存在 correlation ref。"""

    return _command(
        trusted=trusted,
        command_type="enqueue",
        session_id=session_id,
        idempotency_key=idempotency_key,
        payload={"content": content},
        correlation_id=run_id,
    )


def map_control_request(
    *,
    command_type: str,
    session_id: str,
    idempotency_key: str,
    payload: dict[str, Any],
    trusted: TrustedRuntimeContext,
    run_id: str | None = None,
) -> AgentControlCommand:
    """Cancel/Resume/Pause 等 control 动作 -> 对应 command_type。"""

    return _command(
        trusted=trusted,
        command_type=command_type,
        session_id=session_id,
        idempotency_key=idempotency_key,
        payload=payload,
        correlation_id=run_id,
    )


# ---------------------------------------------------------------------------
# submit + 统一 cursor 订阅
# ---------------------------------------------------------------------------


async def submit_command(
    command: AgentControlCommand, *, permit: AgentControlPermit
) -> AgentControlReceipt:
    kernel = get_agent_kernel()
    if kernel is None:
        raise RuntimeError("agent kernel ingress is active but no kernel is registered")
    return await kernel.submit(command, permit=permit)


async def subscribe_projected(
    session_id: str,
    *,
    trusted: TrustedRuntimeContext,
    after_seq: int = 0,
    projector: Callable[[SessionEventEnvelope], Any] | None = None,
) -> AsyncIterator[tuple[int, Any]]:
    """统一 cursor 订阅：所有 SSE 的 reconnect cursor 都源自同一 Session seq。

    projector 返回 None 表示该 envelope 在该协议下不投影（跳过但 cursor 仍推进）。
    """

    kernel = get_agent_kernel()
    if kernel is None:
        raise RuntimeError("agent kernel ingress is active but no kernel is registered")
    subscription = SessionEventSubscription(
        tenant_id=trusted.tenant_id,
        agent_instance_id=trusted.agent_instance_id,
        session_id=session_id,
        authorization_ref=trusted.permit.permit_id,
        after_seq=after_seq,
    )
    async for envelope in kernel.subscribe(subscription, permit=trusted.permit):
        projected = envelope if projector is None else projector(envelope)
        if projected is None:
            continue
        yield int(envelope.seq), projected


# ---------------------------------------------------------------------------
# canonical kernel HTTP ingress: /agent-kernel/v1/*
# ---------------------------------------------------------------------------

# 三边（agentengine-gateway 转发、agentengine-server runtime client、KsADK
# runtime）唯一一致的 kernel ingress 路径常量；契约测试锁定。
KERNEL_INGRESS_BASE_PATH = "/agent-kernel/v1"
KERNEL_INGRESS_SUBMIT_PATH = f"{KERNEL_INGRESS_BASE_PATH}/SubmitAgentControl"
KERNEL_INGRESS_STATUS_PATH = f"{KERNEL_INGRESS_BASE_PATH}/GetAgentStatus"
KERNEL_INGRESS_SESSION_EVENTS_PATH = f"{KERNEL_INGRESS_BASE_PATH}/SubscribeSessionEvents"
KERNEL_INGRESS_HEALTH_PATH = f"{KERNEL_INGRESS_BASE_PATH}/health"

ENV_KERNEL_STORE_DRIVER = "AGENT_KERNEL_STORE_DRIVER"
ENV_KERNEL_STORE_DSN = "AGENT_KERNEL_STORE_DSN"
ENV_JWKS_URL = "AGENT_CONTROL_JWKS_URL"


async def bootstrap_agent_kernel_from_env() -> Any | None:
    """``AGENT_KERNEL_ENABLED=1`` 且能装配 store 时自动 ``set_agent_kernel``。

    避免"开了 env 也不生效"：server lifespan 启动时调用；装配失败抛异常
    （fail loud），不静默降级。已注册 kernel 时幂等返回。
    """

    if get_agent_kernel() is not None:
        return get_agent_kernel()
    if not kernel_ingress_enabled():
        return None

    from ksadk.events.session_event import SessionServiceEventStore
    from ksadk.kernel.control import AgentKernel
    from ksadk.sessions.in_memory import InMemorySessionService

    driver = os.environ.get(ENV_KERNEL_STORE_DRIVER, "memory").strip().lower()
    dsn = os.environ.get(ENV_KERNEL_STORE_DSN, "").strip()
    session_service: Any = InMemorySessionService()
    events = SessionServiceEventStore(session_service)
    store: Any = None
    nonce_store: Any = None

    if driver == "postgres":
        from ksadk.kernel.postgres_store import (
            PostgresAgentKernelStore,
            PostgresNonceStore,
        )
        from ksadk.sessions.postgres_service import PostgresSessionService

        if not dsn:
            raise RuntimeError("postgres kernel store requires AGENT_KERNEL_STORE_DSN")
        # 事件与 session 走同一 PG（PG-backed SessionServiceEventStore），
        # 使 worker 产生的 family=runtime/v2 事件对 canonical SSE 可见；
        # nonce 用 PG durable 存储，跨 Pod / 重启防重放。
        session_service = PostgresSessionService(dsn=dsn)
        await session_service._ensure_pool()
        events = SessionServiceEventStore(session_service)
        pool = session_service._pool
        store = PostgresAgentKernelStore(pool, None, owns_pool=True)
        nonce_store = PostgresNonceStore(pool)
    elif driver == "sqlite":
        if dsn:
            from ksadk.kernel.sqlite_store import SQLiteAgentKernelStore

            store = SQLiteAgentKernelStore(dsn, events)
    if store is None:
        from ksadk.kernel.memory_store import InMemoryAgentKernelStore

        store = InMemoryAgentKernelStore(events)

    kernel = AgentKernel(
        store, events, permit_verifier=_env_permit_verifier(nonce_store=nonce_store)
    )
    if hasattr(store, "ensure_schema"):
        try:
            await store.ensure_schema()
        except Exception:  # pragma: no cover - schema 已存在等场景
            pass
    set_agent_kernel(kernel)
    return kernel


def _env_permit_verifier(*, nonce_store: Any = None) -> Any:
    """JWKS URL 配置时用远端 verifier；否则用进程内 issuer（本地/灰度）。

    远端模式下 JWKS 源会合并进程内 issuer 的公钥：canonical ingress 的
    status/subscribe 等本地 trusted-context permit（进程内签发）与 server
    签发的 permit（远端 JWKS）都能被同一个 verifier 验签，fail closed 语义
    不变（两把 key 都必须真实签名）。
    """

    jwks_url = os.environ.get(ENV_JWKS_URL, "").strip()
    if jwks_url:
        from ksadk.kernel.authorization import AgentControlPermitVerifier

        class _HttpJwks:
            def __init__(self, url: str) -> None:
                self._url = url

            async def fetch_verification_keys(self) -> Mapping[str, str]:
                import httpx

                async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
                    response = await client.get(self._url)
                    response.raise_for_status()
                    raw = response.json().get("keys") or {}
                    if isinstance(raw, Mapping):
                        merged = {str(k): str(v) for k, v in raw.items()}
                    else:
                        # 标准 JWKS shape：[{"kty","crv","kid","x"}, ...]
                        merged = {
                            str(j["kid"]): str(j["x"])
                            for j in raw
                            if "kid" in j and "x" in j
                        }
                local = _default_issuer()
                merged[local.key_id] = local._public_b64
                return merged

        return AgentControlPermitVerifier(
            _HttpJwks(jwks_url), nonce_store=nonce_store
        )
    return _default_issuer().verifier(nonce_store=nonce_store)


def _build_kernel_router() -> Any:
    from ksadk.kernel.contracts import (
        AgentControlPermit,
        AgentStatusQuery,
        SessionEventSubscription,
    )

    router = APIRouter()

    def _unavailable() -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"error": {"Code": "kernel_not_enabled", "Message": "agent kernel is not registered"}},
        )

    @router.post(KERNEL_INGRESS_SUBMIT_PATH)
    async def submit_agent_control(request: Request) -> Any:
        kernel = get_agent_kernel()
        if kernel is None:
            return _unavailable()
        body = await request.json()
        from ksadk.kernel.contracts import AgentControlCommand

        permit_data = body.get("permit")
        try:
            command = AgentControlCommand.model_validate(body.get("command") or body)
        except Exception as exc:
            return JSONResponse(
                status_code=400,
                content={"error": {"Code": "invalid_command", "Message": str(exc)}},
            )
        if permit_data:
            permit = AgentControlPermit.model_validate(permit_data)
        else:
            # 无 permit（gateway 内网转发 / 本地灰度）：trusted context 进程内签发。
            trusted = trusted_context(
                source_kind="system",
                source_ref=str(command.command_id),
                session_id=command.session_id or None,
                operations=(command.command_type,),
            )
            permit = trusted.permit
            command = command.model_copy(
                update={
                    "tenant_id": trusted.tenant_id,
                    "agent_instance_id": trusted.agent_instance_id,
                    "authorization_ref": permit.permit_id,
                }
            )
        receipt = await kernel.submit(command, permit=permit)
        return JSONResponse(
            status_code=receipt_http_status(receipt),
            content=json.loads(receipt.model_dump_json()),
            headers=receipt_response_headers(receipt),
        )

    @router.post(KERNEL_INGRESS_STATUS_PATH)
    async def get_agent_status(request: Request) -> Any:
        kernel = get_agent_kernel()
        if kernel is None:
            return _unavailable()
        body = await request.json()
        try:
            query = AgentStatusQuery.model_validate(body)
        except Exception as exc:
            return JSONResponse(
                status_code=400,
                content={"error": {"Code": "invalid_query", "Message": str(exc)}},
            )
        trusted = trusted_context(
            source_kind="system",
            source_ref="status",
            tenant_id=query.tenant_id,
            agent_instance_id=query.agent_instance_id,
            session_id=query.session_id,
            operations=("get_status",),
        )
        snapshot = await kernel.status(query, permit=trusted.permit)
        return JSONResponse(json.loads(snapshot.model_dump_json()))

    @router.get(KERNEL_INGRESS_SESSION_EVENTS_PATH)
    async def subscribe_session_events(request: Request) -> Any:
        kernel = get_agent_kernel()
        if kernel is None:
            return _unavailable()
        params = request.query_params
        session_id = str(params.get("session_id") or "")
        if not session_id:
            return JSONResponse(
                status_code=400,
                content={"error": {"Code": "missing_session_id", "Message": "session_id is required"}},
            )
        instance_id = str(params.get("agent_instance_id") or "local-agent")
        tenant_id = str(params.get("tenant_id") or "local")
        try:
            after_seq = int(params.get("after_seq") or 0)
        except ValueError:
            after_seq = 0
        trusted = trusted_context(
            source_kind="system",
            source_ref="events",
            tenant_id=tenant_id,
            agent_instance_id=instance_id,
            session_id=session_id,
            operations=("subscribe_events",),
        )

        async def generator():
            async for seq, envelope in subscribe_projected(
                session_id, trusted=trusted, after_seq=after_seq
            ):
                payload = (
                    envelope.payload
                    if isinstance(envelope, dict)
                    else getattr(envelope, "payload", {})
                ) or {}
                # SSE 消费方（gateway / hosted UI）需要 family/event_type/seq
                # 判别事件流类别，payload 原样内嵌。
                frame = dict(payload)
                frame.setdefault("seq", seq)
                if not isinstance(envelope, dict):
                    frame.setdefault("family", getattr(envelope, "family", None))
                    frame.setdefault(
                        "family_version", getattr(envelope, "family_version", None)
                    )
                    frame.setdefault("event_type", getattr(envelope, "event_type", None))
                    if getattr(envelope, "run_id", None):
                        frame.setdefault("run_id", envelope.run_id)
                yield f"id: {seq}\ndata: {json.dumps(frame, ensure_ascii=False)}\n\n"

        return StreamingResponse(generator(), media_type="text/event-stream")

    @router.get(KERNEL_INGRESS_HEALTH_PATH)
    async def kernel_health() -> Any:
        kernel = get_agent_kernel()
        return JSONResponse(
            {
                "enabled": kernel_ingress_enabled(),
                "ready": kernel is not None,
                "store_driver": os.environ.get(ENV_KERNEL_STORE_DRIVER, "memory"),
                "contract_digest": os.environ.get("AGENT_KERNEL_CONTRACT_DIGEST", ""),
                "capability_digest": os.environ.get("AGENT_KERNEL_CAPABILITY_DIGEST", ""),
            }
        )

    return router


_agent_kernel_router: Any | None = None


def agent_kernel_router() -> Any:
    """kernel ingress HTTP 路由（/agent-kernel/v1/*）；由 server 装配层 include。"""

    global _agent_kernel_router
    if _agent_kernel_router is None:
        _agent_kernel_router = _build_kernel_router()
    return _agent_kernel_router


__all__ = [
    "ENV_KERNEL_ENABLED",
    "InProcessPermitIssuer",
    "KERNEL_INGRESS_BASE_PATH",
    "KERNEL_INGRESS_HEALTH_PATH",
    "KERNEL_INGRESS_SESSION_EVENTS_PATH",
    "KERNEL_INGRESS_STATUS_PATH",
    "KERNEL_INGRESS_SUBMIT_PATH",
    "RECEIPT_HTTP_STATUS",
    "TrustedRuntimeContext",
    "agent_kernel_router",
    "bootstrap_agent_kernel_from_env",
    "clear_agent_kernel",
    "get_agent_kernel",
    "kernel_ingress_enabled",
    "kernel_route_active",
    "map_a2a_task",
    "map_agui_request",
    "map_control_request",
    "map_responses_request",
    "map_run_request",
    "map_studio_request",
    "receipt_error_payload",
    "receipt_http_status",
    "receipt_response_headers",
    "set_agent_kernel",
    "submit_command",
    "subscribe_projected",
    "trusted_context",
]
