from __future__ import annotations

import asyncio
import contextvars
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator
from urllib.parse import urlsplit, urlunsplit

from ksadk.sessions.base import BaseSessionService, Session, SessionEvent, SessionState
from ksadk.sessions.continuity import (
    ADKSessionAdapter,
    ConversationSessionCore,
    LangChainSessionAdapter,
    LangGraphSessionAdapter,
    RunnerSessionAdapter,
    SessionContinuityLevel,
    SessionContinuityStatus,
    TranscriptReplayAdapter,
)
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.sessions.local_service import create_local_session_service

_cached_session_service: BaseSessionService | None = None
_cached_session_service_loop: asyncio.AbstractEventLoop | None = None
_session_service_override: contextvars.ContextVar[BaseSessionService | None] = (
    contextvars.ContextVar("ksadk_session_service", default=None)
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionBackendConfig:
    backend: str
    dsn: str = ""
    path: str = ""
    namespace: str = "default"
    tenant_id: str = "default"
    workspace_id: str = "default"


SessionBackendFactory = Callable[[SessionBackendConfig, str | None], BaseSessionService]
_backend_factories: dict[str, SessionBackendFactory] = {}


def register_session_backend(name: str, factory: SessionBackendFactory) -> None:
    normalized = name.strip().lower()
    if not normalized:
        raise ValueError("Session backend name must not be empty")
    _backend_factories[normalized] = factory


def resolve_session_backend_config(*, backend: str | None = None) -> SessionBackendConfig:
    _register_builtin_backends()
    resolved_backend = (
        (
            backend
            or os.getenv("KSADK_SESSION_BACKEND")
            or os.getenv("AGENTENGINE_SESSION_BACKEND")
            or os.getenv("KSADK_STM_BACKEND")
            or ""
        )
        .strip()
        .lower()
    )
    if not resolved_backend:
        resolved_backend = "local"
    if resolved_backend == "sqlite":
        resolved_backend = "local"
    if resolved_backend not in _backend_factories:
        supported = ", ".join(sorted({*list(_backend_factories), "sqlite"}))
        raise ValueError(
            "Unsupported KSADK session backend "
            f"{resolved_backend!r}; supported backends are {supported}"
        )

    dsn = (
        os.getenv("KSADK_SESSION_DSN")
        or os.getenv("KSADK_STM_URL")
        or os.getenv("KSADK_STM_DB_URL")
        or ""
    ).strip()
    path = (
        os.getenv("KSADK_SESSION_PATH")
        or os.getenv("KSADK_STM_PATH")
        or os.getenv("KSADK_STM_DB_PATH")
        or ""
    ).strip()
    namespace = (
        os.getenv("KSADK_SESSION_NAMESPACE")
        or os.getenv("KSADK_WORKSPACE_ID")
        or os.getenv("AGENTENGINE_WORKSPACE_ID")
        or os.getenv("KSADK_TENANT_ID")
        or os.getenv("AGENTENGINE_TENANT_ID")
        or "default"
    ).strip()
    tenant_id = (
        os.getenv("KSADK_TENANT_ID") or os.getenv("AGENTENGINE_TENANT_ID") or "default"
    ).strip()
    workspace_id = (
        os.getenv("KSADK_WORKSPACE_ID") or os.getenv("AGENTENGINE_WORKSPACE_ID") or "default"
    ).strip()
    return SessionBackendConfig(
        backend=resolved_backend,
        dsn=dsn,
        path=path,
        namespace=namespace or "default",
        tenant_id=tenant_id or "default",
        workspace_id=workspace_id or "default",
    )


def _create_memory_backend(
    config: SessionBackendConfig,
    project_dir: str | None,
) -> BaseSessionService:
    del config, project_dir
    return InMemorySessionService()


def _create_local_backend(
    config: SessionBackendConfig,
    project_dir: str | None,
) -> BaseSessionService:
    if config.path:
        from pathlib import Path

        from ksadk.sessions.local_service import LocalSessionService

        return LocalSessionService(db_path=Path(config.path))
    return create_local_session_service(project_dir=project_dir)


def _create_postgres_backend(
    config: SessionBackendConfig,
    project_dir: str | None,
) -> BaseSessionService:
    del project_dir
    if not config.dsn:
        raise ValueError("KSADK_SESSION_DSN is required when KSADK_SESSION_BACKEND=postgres")
    from ksadk.sessions.postgres_service import PostgresSessionService
    from ksadk.sessions.resilient import ResilientSessionService

    return ResilientSessionService(
        PostgresSessionService(
            dsn=config.dsn,
            namespace=config.namespace,
            tenant_id=config.tenant_id,
            workspace_id=config.workspace_id,
            connect_timeout=_postgres_connect_timeout_seconds(),
        )
    )


def _postgres_connect_timeout_seconds() -> float:
    from ksadk.sessions.resilience import session_backend_timeout_seconds

    return session_backend_timeout_seconds()


def _register_builtin_backends() -> None:
    _backend_factories.setdefault("memory", _create_memory_backend)
    _backend_factories.setdefault("local", _create_local_backend)
    _backend_factories.setdefault("postgres", _create_postgres_backend)


def describe_session_backend(*, backend: str | None = None) -> dict[str, object]:
    config = resolve_session_backend_config(backend=backend)
    payload: dict[str, object] = {
        "Backend": config.backend,
        "Shared": config.backend == "postgres",
        "ProductionSafe": config.backend == "postgres",
        "ContinuityDefault": "semantic/replay" if config.backend == "postgres" else "local_only",
    }
    if config.backend == "postgres":
        payload.update({"FailureMode": "fail_open", "FallbackBackend": "memory"})
    return payload


def log_session_backend_diagnostics(*, backend: str | None = None) -> None:
    config = resolve_session_backend_config(backend=backend)
    payload = describe_session_backend(backend=config.backend)
    if config.backend == "postgres":
        payload = {
            **payload,
            "Dsn": mask_session_dsn(config.dsn),
            "Namespace": config.namespace,
            "TenantId": config.tenant_id,
            "WorkspaceId": config.workspace_id,
        }
    elif config.backend == "local" and config.path:
        payload = {**payload, "Path": config.path}
    logger.info("KSADK session backend: %s", payload)
    if not bool(payload.get("ProductionSafe")):
        logger.warning(
            "KSADK session backend %s is not cross-pod recoverable; "
            "use postgres for K8s multi-replica deployments.",
            payload.get("Backend"),
        )


def mask_session_dsn(dsn: str) -> str:
    if not dsn:
        return ""
    try:
        parts = urlsplit(dsn)
    except ValueError:
        return "***"
    if not parts.password:
        return dsn
    username = parts.username or ""
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    auth = f"{username}:***@" if username else "***@"
    netloc = f"{auth}{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def create_session_service(
    endpoint: str | None = None,
    *,
    backend: str | None = None,
    project_dir: str | None = None,
) -> BaseSessionService:
    del endpoint
    _register_builtin_backends()
    config = resolve_session_backend_config(backend=backend)
    factory = _backend_factories[config.backend]
    service = factory(config, project_dir)
    log_session_backend_diagnostics(backend=config.backend)
    return service


@contextmanager
def bind_session_service(service: BaseSessionService) -> Iterator[None]:
    """Bind a service to the current request/task context."""
    token = _session_service_override.set(service)
    try:
        yield
    finally:
        _session_service_override.reset(token)


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def resolve_session_service() -> BaseSessionService:
    """Resolve a request-bound service or a loop-safe legacy fallback.

    ``LocalSessionService`` owns asyncio synchronization primitives. A process
    cache shared by pytest-asyncio loops can therefore reuse a lock bound to a
    closed peer loop. Legacy non-request callers retain caching, while async
    callers never reuse a cache created by another running loop.
    """
    global _cached_session_service, _cached_session_service_loop

    override = _session_service_override.get()
    if override is not None:
        return override

    loop = _running_loop()
    if _cached_session_service is not None:
        if loop is None or _cached_session_service_loop is loop:
            return _cached_session_service
        if _cached_session_service_loop is None:
            # A synchronous startup path created the service. Associate it
            # with the first async caller rather than replacing it eagerly.
            _cached_session_service_loop = loop
            return _cached_session_service

    _cached_session_service = create_session_service()
    _cached_session_service_loop = loop
    return _cached_session_service


async def reset_session_service() -> None:
    global _cached_session_service, _cached_session_service_loop
    if _cached_session_service is None:
        _cached_session_service_loop = None
        return

    service = _cached_session_service
    _cached_session_service = None
    _cached_session_service_loop = None
    close = getattr(service, "aclose", None)
    if close is not None:
        await close()


def get_session_service() -> BaseSessionService:
    return resolve_session_service()


async def close_session_service() -> None:
    await reset_session_service()


__all__ = [
    "ADKSessionAdapter",
    "BaseSessionService",
    "bind_session_service",
    "ConversationSessionCore",
    "InMemorySessionService",
    "LangChainSessionAdapter",
    "LangGraphSessionAdapter",
    "RunnerSessionAdapter",
    "Session",
    "SessionContinuityLevel",
    "SessionContinuityStatus",
    "SessionEvent",
    "SessionState",
    "TranscriptReplayAdapter",
    "close_session_service",
    "create_session_service",
    "describe_session_backend",
    "get_session_service",
    "log_session_backend_diagnostics",
    "mask_session_dsn",
    "register_session_backend",
    "reset_session_service",
    "resolve_session_backend_config",
    "resolve_session_service",
]
