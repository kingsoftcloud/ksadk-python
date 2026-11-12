"""Environment assembly and lifecycle for the Kernel runtime."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ksadk.events.session_event import SessionServiceEventStore
from ksadk.kernel.authorization import InMemoryNonceStore
from ksadk.kernel.contract_fingerprints import AGENT_KERNEL_V1_AGGREGATE_DIGEST
from ksadk.kernel.runtime_config import AuthorityMode, DurabilityTier
from ksadk.kernel.session_storage import select_kernel_session_storage
from ksadk.kernel.store import AgentKernelStore
from ksadk.runtime.adapter import RuntimeAdapter
from ksadk.runtime.session_storage import bind_session_adapter_provider, close_preflight_adapter

if TYPE_CHECKING:
    from ksadk.kernel.bootstrap import AgentKernelRuntime

logger = logging.getLogger(__name__)
_bootstrap_in_progress = False


async def bootstrap_runtime_from_env(
    *,
    adapter_provider: Callable[[], RuntimeAdapter] | None = None,
    runtime_executor: Any | None = None,
    launch_context: Any | None = None,
    start_request_defaults: dict[str, Any] | None = None,
    session_service: Any | None = None,
    session_service_explicit: bool | None = None,
    storage_owner: Any | None = None,
    before_start: Any | None = None,
) -> AgentKernelRuntime | None:
    """Select storage once, then assemble; failed startup never changes stores."""
    global _bootstrap_in_progress
    from ksadk.kernel.bootstrap import get_agent_kernel_runtime

    enabled = os.getenv("AGENT_KERNEL_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return None
    driver = os.getenv("AGENT_KERNEL_STORE_DRIVER", "memory").strip().lower()
    selection = None
    if driver == "postgres":
        selection = select_kernel_session_storage(
            os.getenv("AGENT_KERNEL_STORE_DSN", ""),
            session_service=session_service,
            session_service_explicit=(
                session_service is not None
                if session_service_explicit is None
                else session_service_explicit
            ),
        )
    existing = get_agent_kernel_runtime()
    if existing is not None:
        same_target = existing.config.session_storage_target == (
            selection.target if selection else None
        )
        same_scope = selection is None or all(
            getattr(existing.config.session_service, name) == getattr(selection.service, name)
            for name in ("namespace", "tenant_id", "workspace_id")
        )
        if (
            storage_owner is None
            or existing.config.storage_owner is not storage_owner
            or existing.config.driver != driver
            or not same_target
            or not same_scope
        ):
            raise RuntimeError(
                "KERNEL_RUNTIME_CONFLICT: an existing Kernel belongs to another app "
                "or storage target"
            )
        return existing
    if _bootstrap_in_progress:
        raise RuntimeError("KERNEL_RUNTIME_CONFLICT: another Kernel app is initializing")
    _bootstrap_in_progress = True
    adapters = []
    if selection is not None:
        session_service = selection.service
        adapter_provider = bind_session_adapter_provider(
            adapter_provider, selection.target, adapters
        )
    try:
        if selection is not None and runtime_executor is not None and launch_context is not None:
            bind = getattr(runtime_executor, "bind_effective_session_target", None)
            if callable(bind):
                bind(launch_context, selection.target)
        return await _assemble_runtime_from_env(
            adapter_provider=adapter_provider,
            runtime_executor=runtime_executor,
            launch_context=launch_context,
            start_request_defaults=start_request_defaults,
            session_service=session_service,
            selection=selection,
            storage_owner=storage_owner,
            before_start=before_start,
        )
    except BaseException:
        if adapters:
            await close_preflight_adapter(adapters[0])
        if selection is not None and selection.owns_pool:
            await selection.service.aclose()
        raise
    finally:
        _bootstrap_in_progress = False


async def _assemble_runtime_from_env(
    *,
    adapter_provider,
    runtime_executor,
    launch_context,
    start_request_defaults,
    session_service,
    selection,
    storage_owner,
    before_start,
):
    """Operator env 投影 -> 生产 runtime（AGENT_KERNEL_ENABLED=1 时）。

    hosted 部署（AGENT_KERNEL_STORE_DRIVER=postgres + JWKS URL）装配并启动
    worker/lease/recovery，同时注册 kernel ingress 与 runtime health。
    """

    from ksadk.kernel.bootstrap import (
        AgentKernelRuntimeConfig,
        build_agent_kernel_runtime,
        set_agent_kernel_runtime,
    )
    from ksadk.kernel.ingress import (
        ENV_JWKS_URL,
        _remote_jwks_source,
        authority_mode,
        set_agent_kernel,
    )

    driver = os.environ.get("AGENT_KERNEL_STORE_DRIVER", "memory").strip().lower()
    dsn = os.environ.get("AGENT_KERNEL_STORE_DSN", "").strip()
    jwks_url = os.environ.get(ENV_JWKS_URL, "").strip()
    session_namespace = os.environ.get("KSADK_SESSION_NAMESPACE", "default").strip() or "default"
    tenant_id = (
        os.environ.get("KSADK_TENANT_ID") or os.environ.get("AGENTENGINE_TENANT_ID") or "default"
    ).strip()
    workspace_id = (
        os.environ.get("KSADK_WORKSPACE_ID")
        or os.environ.get("AGENTENGINE_WORKSPACE_ID")
        or "default"
    ).strip()
    mode: AuthorityMode = authority_mode()  # type: ignore[assignment]
    if selection is not None:
        dsn = selection.target.dsn
        session_namespace = session_service.namespace
        tenant_id, workspace_id = session_service.tenant_id, session_service.workspace_id
    durability_tier: DurabilityTier = (
        os.environ.get("AGENT_KERNEL_DURABILITY_TIER", "durable").strip().lower()
    )  # type: ignore[assignment]
    injected_contract_digest = os.environ.get("AGENT_KERNEL_CONTRACT_DIGEST", "").strip()
    if mode == "hosted" and injected_contract_digest != AGENT_KERNEL_V1_AGGREGATE_DIGEST:
        raise RuntimeError(
            "contract_digest_mismatch: hosted agent kernel runtime image "
            "does not support the control-plane contract digest"
        )

    if selection is not None:
        try:
            await session_service._ensure_schema()
        except Exception:
            raise RuntimeError(
                "KERNEL_SESSION_STORE_UNAVAILABLE: selected PostgreSQL Session store "
                "could not be initialized"
            ) from None
        logger.info(
            "Kernel Session storage backend=postgres source=%s namespace=%s "
            "shared_across_pods=true",
            selection.target.source,
            session_namespace,
        )

    # Lazy framework adapters learn their native checkpoint capabilities during
    # preflight. Snapshotting before it permanently disables cold recovery even
    # when the deployment has configured durable native sessions.
    capabilities = None
    capability_adapter = None
    if adapter_provider is not None:
        capability_adapter = adapter_provider()
        adopt = getattr(capability_adapter, "adopt_app_lifetime", None)
        if selection is not None and callable(adopt):
            adopt()
        await capability_adapter.preflight()
        capabilities = capability_adapter.capabilities

    pool = None
    owns_pool = False
    if driver == "postgres":
        from ksadk.kernel.postgres_store import (
            PostgresAgentKernelStore,
            PostgresFencedSessionEventStore,
            PostgresKernelEventLog,
            PostgresNonceStore,
        )

        pool = session_service._pool
        event_log = PostgresKernelEventLog(
            pool,
            namespace=session_service.namespace,
            tenant_id=session_service.tenant_id,
            workspace_id=session_service.workspace_id,
        )
        store: AgentKernelStore = PostgresAgentKernelStore(
            pool, event_log, tenant_id=session_service.tenant_id, owns_pool=selection.owns_pool
        )
        await store.ensure_schema()
        session_events: Any = PostgresFencedSessionEventStore(store)
        nonce_store: Any = PostgresNonceStore(pool)
        owns_pool = selection.owns_pool
    elif driver == "sqlite":
        # Let the composition root own/close the actual SQLite store. Supplying
        # an in-memory store here silently discarded the configured database.
        store = None
        session_events = None
        nonce_store = InMemoryNonceStore()
    else:
        from ksadk.kernel.memory_store import InMemoryAgentKernelStore
        from ksadk.sessions.in_memory import InMemorySessionService

        # The HTTP Session routes and the Kernel worker must project through
        # one physical Session log.  The Runtime App passes its owned service
        # here so a foreground kernel stream remains replayable through
        # ListSessionEvents/ListSessionMessages after refresh.  Standalone
        # bootstrap callers retain the historical in-memory default.
        if session_service is None:
            session_service = InMemorySessionService()
        session_events = SessionServiceEventStore(session_service)
        store = InMemoryAgentKernelStore(session_events)
        # Explicit ephemeral hosted mode still needs replay protection while
        # this process lives. It does not promise restart durability.
        nonce_store = InMemoryNonceStore()

    agent_instance_id = os.environ.get("AGENT_INSTANCE_ID", "").strip()
    if not agent_instance_id and mode != "hosted":
        agent_instance_id = "local-agent"
    pod_uid = os.environ.get("POD_UID", "").strip()
    config = AgentKernelRuntimeConfig(
        agent_instance_id=agent_instance_id,
        authority_mode=mode,
        driver=driver,
        durability_tier=durability_tier,
        dsn=dsn,
        jwks=_remote_jwks_source(jwks_url) if mode == "hosted" else None,
        permit_issuer=os.environ.get("AGENT_CONTROL_PERMIT_ISSUER", ""),
        nonce_store=nonce_store,
        adapter_provider=adapter_provider,
        capabilities=capabilities,
        start_request_defaults=dict(start_request_defaults or {}),
        contract_digest=(
            AGENT_KERNEL_V1_AGGREGATE_DIGEST if mode == "hosted" else injected_contract_digest
        ),
        capability_digest=os.environ.get("AGENT_KERNEL_CAPABILITY_DIGEST", ""),
        bundle_digest=os.environ.get("AGENT_BUNDLE_DIGEST", ""),
        session_namespace=session_namespace,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        store=store,
        session_events=session_events,
        session_service=session_service,
        session_storage_target=selection.target if selection is not None else None,
        storage_owner=storage_owner,
        preflight_adapter=capability_adapter if selection is not None else None,
        runtime_executor=runtime_executor,
        launch_context=launch_context,
        pool=pool,
        owns_pool=owns_pool,
        # Operator 通过 downward API 注入 POD_UID。与 stable instance id
        # 组合保留 workload 来源；同 Pod 内进程重启仍是新的 owner，不能
        # 静默续用旧 fence。hosted 缺 POD_UID 继续拒绝启动。
        activation_id=f"{agent_instance_id}:{pod_uid}:runtime:{uuid4().hex}" if pod_uid else None,
        lease_ttl_seconds=float(os.environ.get("AGENT_KERNEL_LEASE_TTL_SECONDS", "90") or "90"),
    )
    runtime = build_agent_kernel_runtime(config)
    try:
        if os.getenv("KSADK_MCP_OAUTH_PROJECTION") or os.getenv("KSADK_MCP_OAUTH_CONFIG_FILE"):
            try:
                from ksadk.mcp_runtime.oauth.bootstrap import configure_runtime
                await configure_runtime(runtime)
            except Exception as exc:
                if not os.getenv("AGENT_RUNTIME_ID"):
                    raise
                logger.warning("Hosted MCP OAuth extension unavailable (%s)", type(exc).__name__)
        if before_start is not None:
            await before_start(runtime)
        await runtime.start()
    except BaseException:
        await runtime.close()
        raise
    set_agent_kernel(runtime.kernel)
    set_agent_kernel_runtime(runtime)
    return runtime
