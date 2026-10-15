# -*- coding: utf-8 -*-
"""Agent Kernel ingress 收敛层（Kernel Task 8）。

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

import os
import ssl
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ksadk.kernel.authorization import (
    JwksSource,
)

ENV_KERNEL_STORE_DRIVER = "AGENT_KERNEL_STORE_DRIVER"
ENV_KERNEL_STORE_DSN = "AGENT_KERNEL_STORE_DSN"
ENV_JWKS_URL = "AGENT_CONTROL_JWKS_URL"
# Projected private CA for Server JWKS; it supplements system roots and does
# not change trust for unrelated HTTP clients in the Agent process.
ENV_JWKS_CA_FILE = "AGENT_CONTROL_JWKS_CA_FILE"
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


async def bootstrap_agent_kernel_from_env(
    *, get_kernel, set_kernel, ingress_enabled, hosted, permit_verifier
) -> Any | None:
    """``AGENT_KERNEL_ENABLED=1`` 且能装配 store 时自动 ``set_agent_kernel``。

    避免"开了 env 也不生效"：server lifespan 启动时调用；装配失败抛异常
    （fail loud），不静默降级。已注册 kernel 时幂等返回。
    """

    existing = get_kernel()
    if existing is not None:
        if hosted():
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
    if not ingress_enabled():
        return None
    # This legacy helper only has enough context to build the ingress facade.
    # In a hosted workload that would create a dangerous half-runtime: it can
    # accept a Server permit, but no worker, lease owner or recovery loop will
    # ever consume the durable command.  Hosted applications must enter via
    # ``bootstrap_agent_kernel_runtime_from_env`` from the FastAPI lifespan,
    # where the real RuntimeAdapter provider is available.
    if hosted():
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
        store = PostgresAgentKernelStore(
            pool, event_log, tenant_id=session_service.tenant_id, owns_pool=True
        )
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
        store, events, permit_verifier=permit_verifier(nonce_store=nonce_store)
    )
    if hasattr(store, "ensure_schema"):
        try:
            await store.ensure_schema()
        except Exception:  # pragma: no cover - schema 已存在等场景
            pass
    set_kernel(kernel)
    return kernel


class _HttpJwks:
    """Server JWKS source used only in hosted deployments."""

    def __init__(self, url: str) -> None:
        self._url = url

    async def fetch_verification_keys(self) -> Mapping[str, str]:
        import httpx

        ca_file = os.environ.get(ENV_JWKS_CA_FILE, "").strip()
        verify: ssl.SSLContext | bool = True
        if ca_file:
            try:
                if not Path(ca_file).is_file():
                    raise OSError("JWKS CA file is not a regular file")
                verify = ssl.create_default_context()
                verify.load_verify_locations(cafile=ca_file)
            except (OSError, ssl.SSLError) as exc:
                raise RuntimeError("AgentControl JWKS CA file is invalid") from exc
        async with httpx.AsyncClient(
            timeout=5.0, follow_redirects=False, trust_env=False, verify=verify
        ) as client:
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


def make_env_permit_verifier(
    *, nonce_store: Any = None, remote_jwks_source, default_issuer, hosted
) -> Any:
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

        source = remote_jwks_source(jwks_url)
        if hosted():
            # hosted 模式：server JWKS 是唯一信任源，绝不合并本地公钥。
            return AgentControlPermitVerifier(source, nonce_store=nonce_store)

        class _LocalCompatibleJwks:
            async def fetch_verification_keys(self) -> Mapping[str, str]:
                merged = dict(await source.fetch_verification_keys())
                local = default_issuer()
                merged[local.key_id] = local._public_b64
                return merged

        return AgentControlPermitVerifier(_LocalCompatibleJwks(), nonce_store=nonce_store)
    if hosted():
        raise RuntimeError("hosted agent kernel runtime requires AGENT_CONTROL_JWKS_URL")
    return default_issuer().verifier(nonce_store=nonce_store)
