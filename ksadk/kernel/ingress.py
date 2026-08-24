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
import logging
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

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

logger = logging.getLogger(__name__)

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
        issued = now or datetime.now(timezone.utc)
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
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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

    # Local web is still a real per-AgentInstance kernel runtime.  Without
    # this projection its compatibility routes self-sign commands for the
    # synthetic ``local-agent`` while the worker owns ``AGENT_INSTANCE_ID``;
    # accepted Inbox rows would then never be leased or consumed.  In hosted
    # mode the local signature remains unverifiable against Server JWKS, so
    # this does not create a Server-admission bypass.
    if agent_instance_id == "local-agent":
        agent_instance_id = (
            os.environ.get("AGENT_INSTANCE_ID", "").strip() or agent_instance_id
        )
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
        received_at=_rfc3339(datetime.now(timezone.utc)),
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
    # The admitted event is written through the Kernel's fenced shared log.
    # Do not assume a legacy HTTP session service used the same namespace or
    # connection pool; direct ingress (and the first RunAgent request) needs
    # the session row in this exact log before the transactional admission.
    await _ensure_shared_log_session(command)
    return await kernel.submit(command, permit=permit)


async def subscribe_projected(
    session_id: str,
    *,
    trusted: TrustedRuntimeContext,
    after_seq: int = 0,
    projector: Callable[[SessionEventEnvelope], Any] | None = None,
    should_stop: Callable[[], Awaitable[bool]] | None = None,
    timeout: float | None = None,
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
    # 兼容不同 AgentKernel 实现（含测试替身）：只传其实际支持的参数。
    import inspect as _inspect

    subscribe_kwargs: dict[str, Any] = {}
    try:
        _params = _inspect.signature(kernel.subscribe).parameters
    except (TypeError, ValueError):  # pragma: no cover - defensive
        _params = {}
    if "should_stop" in _params:
        subscribe_kwargs["should_stop"] = should_stop
    if "timeout" in _params:
        subscribe_kwargs["timeout"] = timeout
    async for envelope in kernel.subscribe(
        subscription, permit=trusted.permit, **subscribe_kwargs
    ):
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
ENV_AUTHORITY_MODE = "AGENT_KERNEL_AUTHORITY_MODE"

_AUTHORITY_LOCAL = "local"
_AUTHORITY_HOSTED = "hosted"


def authority_mode() -> str:
    """当前 permit authority 模式。

    - ``AGENT_KERNEL_AUTHORITY_MODE=local``：显式本地授权（开发 / 灰度 /
      canary）。允许进程内 issuer 自签 trusted-context permit，且 JWKS
      合并本地公钥。
    - ``AGENT_KERNEL_AUTHORITY_MODE=hosted``：托管模式，fail closed——缺
      permit 一律 401，本地自签 / 未知 key 一律 403，JWKS 不得合并本地
      公钥。
    - 未显式配置时：配置了 ``AGENT_CONTROL_JWKS_URL`` 视为 hosted（server
      签发是唯一信任源），否则默认 local（保持本地灰度行为）。
    """

    explicit = os.environ.get(ENV_AUTHORITY_MODE, "").strip().lower()
    if explicit in (_AUTHORITY_LOCAL, _AUTHORITY_HOSTED):
        return explicit
    if os.environ.get(ENV_JWKS_URL, "").strip():
        return _AUTHORITY_HOSTED
    return _AUTHORITY_LOCAL


def _is_hosted() -> bool:
    return authority_mode() == _AUTHORITY_HOSTED


async def bootstrap_agent_kernel_from_env() -> Any | None:
    """``AGENT_KERNEL_ENABLED=1`` 且能装配 store 时自动 ``set_agent_kernel``。

    避免"开了 env 也不生效"：server lifespan 启动时调用；装配失败抛异常
    （fail loud），不静默降级。已注册 kernel 时幂等返回。
    """

    existing = get_agent_kernel()
    if existing is not None:
        if _is_hosted():
            # A caller may have registered a bare AgentKernel before entering
            # this helper.  Treat that exactly like a fresh half-runtime: an
            # ingress facade without the production owner loops is not a
            # healthy hosted deployment.
            from ksadk.kernel.bootstrap import get_agent_kernel_runtime

            runtime = get_agent_kernel_runtime()
            if runtime is None or runtime.kernel is not existing:
                raise RuntimeError(
                    "hosted agent kernel ingress requires the full production "
                    "composition root; a bare kernel is not allowed"
                )
        return existing
    if not kernel_ingress_enabled():
        return None
    # This legacy helper only has enough context to build the ingress facade.
    # In a hosted workload that would create a dangerous half-runtime: it can
    # accept a Server permit, but no worker, lease owner or recovery loop will
    # ever consume the durable command.  Hosted applications must enter via
    # ``bootstrap_agent_kernel_runtime_from_env`` from the FastAPI lifespan,
    # where the real RuntimeAdapter provider is available.
    if _is_hosted():
        raise RuntimeError(
            "hosted agent kernel ingress requires the full production "
            "composition root; use bootstrap_agent_kernel_runtime_from_env"
        )

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
            PostgresFencedSessionEventStore,
            PostgresKernelEventLog,
            PostgresNonceStore,
        )
        from ksadk.sessions.postgres_service import PostgresSessionService

        if not dsn:
            raise RuntimeError("postgres kernel store requires AGENT_KERNEL_STORE_DSN")
        namespace = str(os.environ.get("KSADK_SESSION_NAMESPACE") or "default").strip()
        tenant_id = str(
            os.environ.get("KSADK_TENANT_ID")
            or os.environ.get("AGENTENGINE_TENANT_ID")
            or "default"
        ).strip()
        workspace_id = str(
            os.environ.get("KSADK_WORKSPACE_ID")
            or os.environ.get("AGENTENGINE_WORKSPACE_ID")
            or "default"
        ).strip()
        # 事件与 session 走同一 PG（PG-backed SessionServiceEventStore），
        # 使 worker 产生的 family=runtime/v2 事件对 canonical SSE 可见；
        # nonce 用 PG durable 存储，跨 Pod / 重启防重放。
        session_service = PostgresSessionService(
            dsn=dsn,
            namespace=namespace or "default",
            tenant_id=tenant_id or "default",
            workspace_id=workspace_id or "default",
        )
        await session_service._ensure_pool()
        pool = session_service._pool
        event_log = PostgresKernelEventLog(
            pool,
            namespace=session_service.namespace,
            tenant_id=session_service.tenant_id,
            workspace_id=session_service.workspace_id,
        )
        store = PostgresAgentKernelStore(pool, event_log, owns_pool=True)
        # typed RuntimeEvent 写路径走 fenced store：ActivationWriteGuard
        # append 与 activation 行验证同一事务（Task 4 Step 5）。
        events = PostgresFencedSessionEventStore(store)
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


class _HttpJwks:
    """Server JWKS source used only in hosted deployments."""

    def __init__(self, url: str) -> None:
        self._url = url

    async def fetch_verification_keys(self) -> Mapping[str, str]:
        import httpx

        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
            response = await client.get(self._url)
            response.raise_for_status()
            raw = response.json().get("keys") or {}
        if isinstance(raw, Mapping):
            return {str(k): str(v) for k, v in raw.items()}
        # 标准 JWKS shape：[{"kty","crv","kid","x"}, ...]
        return {
            str(item["kid"]): str(item["x"])
            for item in raw
            if isinstance(item, Mapping) and "kid" in item and "x" in item
        }


def _remote_jwks_source(jwks_url: str | None = None) -> JwksSource:
    """构造唯一的 Server JWKS source；空值绝不回退本地 authority。"""

    url = (jwks_url or os.environ.get(ENV_JWKS_URL) or "").strip()
    if not url:
        raise RuntimeError("hosted agent kernel runtime requires AGENT_CONTROL_JWKS_URL")
    return _HttpJwks(url)


def _env_permit_verifier(*, nonce_store: Any = None) -> Any:
    """JWKS URL 配置时用远端 verifier；否则用进程内 issuer（本地/灰度）。

    authority mode 决定是否合并进程内 issuer 公钥：

    - local：合并本地公钥——canonical ingress 的 status/subscribe 等本地
      trusted-context permit 与 server permit 都能被同一个 verifier 验签，
      fail closed 语义不变（两把 key 都必须真实签名）。
    - hosted：禁止合并本地公钥/自签。JWKS 内的 server key 是唯一信任源，
      本地签发的 permit 得到 unknown_signing_key -> fail closed。
    """

    jwks_url = os.environ.get(ENV_JWKS_URL, "").strip()
    if jwks_url:
        from ksadk.kernel.authorization import AgentControlPermitVerifier
        source = _remote_jwks_source(jwks_url)
        if _is_hosted():
            # hosted 模式：server JWKS 是唯一信任源，绝不合并本地公钥。
            return AgentControlPermitVerifier(source, nonce_store=nonce_store)

        class _LocalCompatibleJwks:
            async def fetch_verification_keys(self) -> Mapping[str, str]:
                merged = dict(await source.fetch_verification_keys())
                local = _default_issuer()
                merged[local.key_id] = local._public_b64
                return merged

        return AgentControlPermitVerifier(_LocalCompatibleJwks(), nonce_store=nonce_store)
    if _is_hosted():
        raise RuntimeError("hosted agent kernel runtime requires AGENT_CONTROL_JWKS_URL")
    return _default_issuer().verifier(nonce_store=nonce_store)


async def _ensure_shared_log_session(command: Any) -> None:
    """canonical submit 前确保 session 存在（共享 event log 前置条件）。

    hosted 链路里会话目录由 server/runtime service 维护；对直接落到本
    runtime ingress 的首个命令（RunAgent enqueue 等），用 kernel runtime 的
    session service 幂等补齐，否则 postgres store 的 accept_command 会在
    第一个事件上以 ``invalid_command: session does not exist`` 拒绝。
    失败时静默放行——store 的显式错误仍是最终裁决。
    """
    session_id = str(getattr(command, "session_id", "") or "")
    if not session_id:
        return
    from ksadk.kernel.bootstrap import get_agent_kernel_runtime

    runtime = get_agent_kernel_runtime()
    service = getattr(getattr(runtime, "config", None), "session_service", None)
    if service is None:
        return
    try:
        if await service.get_session(session_id) is None:
            await service.create_session(
                agent_id=str(getattr(command, "agent_instance_id", "") or "runtime"),
                user_id=str(getattr(command, "tenant_id", "") or "tenant"),
                session_id=session_id,
            )
    except Exception:
        pass


def _build_kernel_router() -> Any:
    from ksadk.kernel.contracts import (
        AgentControlPermit,
        AgentStatusQuery,
    )

    router = APIRouter()

    def _unavailable() -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "Code": "kernel_not_enabled",
                    "Message": "agent kernel is not registered",
                }
            },
        )

    def _hosted_permit(
        request: Request, permit_data: Any | None = None
    ) -> AgentControlPermit | JSONResponse:
        """Hosted ingress accepts only a Server-issued permit.

        POST actions use the wrapper ``permit`` object; GET SSE uses the
        internal ``X-Agent-Control-Permit`` JSON header.  Gateway strips that
        header at the public edge, so it can only originate from Server.
        """

        raw = permit_data
        if raw is None:
            raw_header = request.headers.get("x-agent-control-permit")
            if raw_header:
                try:
                    raw = json.loads(raw_header)
                except json.JSONDecodeError:
                    return JSONResponse(
                        status_code=403,
                        content={
                            "error": {
                                "Code": "invalid_permit",
                                "Message": "invalid permit header",
                            }
                        },
                    )
        if raw is None:
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "Code": "missing_permit",
                        "Message": "hosted authority requires a server-issued permit",
                    }
                },
            )
        try:
            return AgentControlPermit.model_validate(raw)
        except Exception:
            logger.info("rejected malformed hosted permit", exc_info=True)
            return JSONResponse(
                status_code=403,
                content={"error": {"Code": "invalid_permit", "Message": "permit 格式无效"}},
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
        except Exception:
            logger.info("rejected malformed agent control command", exc_info=True)
            return JSONResponse(
                status_code=400,
                content={"error": {"Code": "invalid_command", "Message": "command 格式无效"}},
            )
        if _is_hosted():
            permit = _hosted_permit(request, permit_data)
            if isinstance(permit, JSONResponse):
                return permit
        elif permit_data:
            try:
                permit = AgentControlPermit.model_validate(permit_data)
            except Exception:
                logger.info("rejected malformed local permit", exc_info=True)
                return JSONResponse(
                    status_code=403,
                    content={"error": {"Code": "invalid_permit", "Message": "permit 格式无效"}},
                )
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
        await _ensure_shared_log_session(command)
        receipt = await kernel.submit(command, permit=permit)
        status = receipt_http_status(receipt)
        if (
            _is_hosted()
            and status != 202
            and receipt.error is not None
            and receipt.error.code == "invalid_permit"
        ):
            # hosted 模式 permit 验证失败是鉴权失败（403），不是普通 400。
            status = 403
        return JSONResponse(
            status_code=status,
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
            query = AgentStatusQuery.model_validate(body.get("query") or body)
        except Exception:
            logger.info("rejected malformed agent status query", exc_info=True)
            return JSONResponse(
                status_code=400,
                content={"error": {"Code": "invalid_query", "Message": "query 格式无效"}},
            )
        if _is_hosted():
            permit = _hosted_permit(request, body.get("permit"))
            if isinstance(permit, JSONResponse):
                return permit
        else:
            trusted = trusted_context(
                source_kind="system",
                source_ref="status",
                tenant_id=query.tenant_id,
                agent_instance_id=query.agent_instance_id,
                session_id=query.session_id,
                operations=("get_status",),
            )
            # local 仅为开发便利自签，query 的 authorization_ref 必须同 permit
            # 本体一致，避免错误地用 caller 自报值触发恒 fail-closed。
            query = query.model_copy(
                update={"authorization_ref": trusted.permit.permit_id}
            )
            permit = trusted.permit
        snapshot = await kernel.status(query, permit=permit)
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
                content={
                    "error": {
                        "Code": "missing_session_id",
                        "Message": "session_id is required",
                    }
                },
            )
        instance_id = str(params.get("agent_instance_id") or "").strip()
        tenant_id = str(params.get("tenant_id") or "").strip()
        if _is_hosted() and (not instance_id or not tenant_id):
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "Code": "missing_resource_identity",
                        "Message": (
                            "hosted subscription requires tenant_id and "
                            "agent_instance_id"
                        ),
                    }
                },
            )
        # Local development has no Server-issued identity projection.  Keep
        # its explicit compatibility defaults out of the hosted branch above.
        instance_id = instance_id or "local-agent"
        tenant_id = tenant_id or "local"
        try:
            after_seq = int(params.get("after_seq") or 0)
        except ValueError:
            after_seq = 0
        # 可选订阅时长上限（秒）：调用方（网关/测试）可显式限定 SSE 生命周期。
        try:
            subscribe_timeout = float(params.get("timeout") or 0) or None
        except ValueError:
            subscribe_timeout = None
        if _is_hosted():
            permit = _hosted_permit(request)
            if isinstance(permit, JSONResponse):
                return permit
            trusted = TrustedRuntimeContext(
                tenant_id=tenant_id,
                agent_instance_id=instance_id,
                source=ControlSource(kind="system", ref="server-subscribe"),
                permit=permit,
                received_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
        else:
            trusted = trusted_context(
                source_kind="system",
                source_ref="events",
                tenant_id=tenant_id,
                agent_instance_id=instance_id,
                session_id=session_id,
                operations=("subscribe_events",),
            )

        async def _client_disconnected() -> bool:
            # 客户端断开后及时收口 SSE，而不是轮询到订阅 timeout。
            try:
                return await request.is_disconnected()
            except Exception:  # pragma: no cover - defensive
                return False

        async def generator():
            async for seq, envelope in subscribe_projected(
                session_id,
                trusted=trusted,
                after_seq=after_seq,
                should_stop=_client_disconnected,
                timeout=subscribe_timeout,
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
        from ksadk.kernel.contract_fingerprints import (
            AGENT_KERNEL_V1_AGGREGATE_DIGEST,
        )
        from ksadk.kernel.runtime_identity import runtime_identity

        kernel = get_agent_kernel()
        payload: dict[str, Any] = {
            "enabled": kernel_ingress_enabled(),
            "ready": kernel is not None,
            "store_driver": os.environ.get(ENV_KERNEL_STORE_DRIVER, "memory"),
            # A process without the full runtime cannot calculate Adapter
            # capabilities, so do not echo a caller-controlled env value.
            "contract_digest": AGENT_KERNEL_V1_AGGREGATE_DIGEST,
            "capability_digest": "",
            "authority_mode": authority_mode(),
            "runtime_identity": runtime_identity(),
        }
        from ksadk.kernel.bootstrap import get_agent_kernel_runtime

        runtime = get_agent_kernel_runtime()
        if runtime is not None:
            # 生产 composition root 注册后，health 报告真实运行态：
            # 真实 store 查询 / worker 运行态 / activation lease 健康 / digest。
            health = await runtime.readiness.check()
            payload.update(health)
        return JSONResponse(payload)

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
    "authority_mode",
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
