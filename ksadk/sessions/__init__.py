from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable
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
        backend
        or os.getenv("KSADK_SESSION_BACKEND")
        or os.getenv("AGENTENGINE_SESSION_BACKEND")
        or os.getenv("KSADK_STM_BACKEND")
        or ""
    ).strip().lower()
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
        os.getenv("KSADK_TENANT_ID")
        or os.getenv("AGENTENGINE_TENANT_ID")
        or "default"
    ).strip()
    workspace_id = (
        os.getenv("KSADK_WORKSPACE_ID")
        or os.getenv("AGENTENGINE_WORKSPACE_ID")
        or "default"
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

    return PostgresSessionService(
        dsn=config.dsn,
        namespace=config.namespace,
        tenant_id=config.tenant_id,
        workspace_id=config.workspace_id,
    )


def _register_builtin_backends() -> None:
    _backend_factories.setdefault("memory", _create_memory_backend)
    _backend_factories.setdefault("local", _create_local_backend)
    _backend_factories.setdefault("postgres", _create_postgres_backend)


def describe_session_backend(*, backend: str | None = None) -> dict[str, object]:
    config = resolve_session_backend_config(backend=backend)
    return {
        "Backend": config.backend,
        "Shared": config.backend == "postgres",
        "ProductionSafe": config.backend == "postgres",
        "ContinuityDefault": "semantic/replay" if config.backend == "postgres" else "local_only",
    }


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
            "KSADK session backend %s is not cross-pod recoverable; use postgres for K8s multi-replica deployments.",
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


def resolve_session_service() -> BaseSessionService:
    global _cached_session_service
    if _cached_session_service is not None:
        return _cached_session_service
    _cached_session_service = create_session_service()
    return _cached_session_service


async def reset_session_service() -> None:
    global _cached_session_service
    if _cached_session_service is None:
        return

    close = getattr(_cached_session_service, "aclose", None)
    if close is not None:
        await close()
    _cached_session_service = None


def get_session_service() -> BaseSessionService:
    return resolve_session_service()


async def close_session_service() -> None:
    await reset_session_service()


__all__ = [
    "ADKSessionAdapter",
    "BaseSessionService",
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
