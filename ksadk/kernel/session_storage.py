"""Select one PostgreSQL authority for Kernel state and Session events."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from ksadk.sessions import resolve_session_backend_config
from ksadk.sessions.postgres_service import PostgresSessionService
from ksadk.sessions.topology import StorageTarget, resolve_persistence_topology


def postgres_kernel_enabled() -> bool:
    return (
        os.getenv("AGENT_KERNEL_ENABLED", "").strip().lower()
        in {
            "1",
            "true",
            "yes",
            "on",
        }
        and os.getenv("AGENT_KERNEL_STORE_DRIVER", "memory").strip().lower() == "postgres"
    )


@dataclass(frozen=True)
class KernelSessionStorage:
    service: PostgresSessionService
    target: StorageTarget
    owns_pool: bool


async def close_owned_kernel_pool(service: Any, pool: Any) -> None:
    """Release a managed Session pool through its one owning service."""
    if isinstance(service, PostgresSessionService) and service._pool is pool:
        await service.aclose()
    else:
        await pool.close()


def _validate_pg_target(dsn: str) -> None:
    try:
        valid = urlsplit(dsn).scheme.lower() in {"postgres", "postgresql"}
    except ValueError:
        valid = False
    if not valid:
        raise RuntimeError(
            "KERNEL_SESSION_CONFIG_INVALID: a PostgreSQL Session address is required"
        )


def _validate_service_identity(service: PostgresSessionService) -> None:
    for field, names in (
        ("namespace", ("KSADK_SESSION_NAMESPACE",)),
        ("tenant_id", ("KSADK_TENANT_ID", "AGENTENGINE_TENANT_ID")),
        ("workspace_id", ("KSADK_WORKSPACE_ID", "AGENTENGINE_WORKSPACE_ID")),
    ):
        expected = next(
            (os.getenv(name, "").strip() for name in names if os.getenv(name, "").strip()), ""
        )
        if expected and getattr(service, field) != expected:
            raise RuntimeError(
                f"KERNEL_SESSION_IDENTITY_CONFLICT: supplied Session {field} "
                "conflicts with runtime identity"
            )


def select_kernel_session_storage(
    platform_dsn: str,
    *,
    session_service: Any = None,
    session_service_explicit: bool = False,
) -> KernelSessionStorage:
    """Only a Session-specific configuration may displace the platform store.

    The legacy generic-checkpoint -> Session fallback deliberately remains a
    non-Kernel feature. A supplied service is caller-owned and must be atomic.
    """
    topology = resolve_persistence_topology()
    if topology.session.explicit_local:
        raise RuntimeError(
            "KERNEL_SESSION_REQUIRES_POSTGRES: explicit local or memory Session "
            "conflicts with PostgreSQL Kernel"
        )
    if topology.session.backend not in {"local", "postgres"}:
        raise RuntimeError(
            "KERNEL_SESSION_REQUIRES_POSTGRES: custom Session backend is not "
            "an atomic PostgreSQL authority"
        )

    if session_service_explicit:
        if not isinstance(session_service, PostgresSessionService):
            raise RuntimeError(
                "KERNEL_SESSION_REQUIRES_POSTGRES: supplied Session must be "
                "a PostgreSQL Session service"
            )
        _validate_pg_target(session_service.dsn)
        _validate_service_identity(session_service)
        if topology.session.source == "explicit" and topology.session.dsn != session_service.dsn:
            raise RuntimeError(
                "KERNEL_SESSION_CONFIG_CONFLICT: supplied Session and Session environment "
                "select different stores"
            )
        return KernelSessionStorage(
            session_service,
            StorageTarget(backend="postgres", dsn=session_service.dsn, source="session_service"),
            False,
        )

    if topology.session.source == "explicit":
        config = resolve_session_backend_config()
        dsn = config.dsn
        namespace, tenant_id, workspace_id = config.namespace, config.tenant_id, config.workspace_id
        source = "explicit"
    else:
        dsn = platform_dsn.strip()
        # Preserve the platform Kernel's existing scope defaults.
        namespace = os.getenv("KSADK_SESSION_NAMESPACE", "default").strip() or "default"
        tenant_id = (
            os.getenv("KSADK_TENANT_ID") or os.getenv("AGENTENGINE_TENANT_ID") or "default"
        ).strip()
        workspace_id = (
            os.getenv("KSADK_WORKSPACE_ID") or os.getenv("AGENTENGINE_WORKSPACE_ID") or "default"
        ).strip()
        source = "platform_kernel_fallback"
    if not dsn:
        raise RuntimeError(
            "KERNEL_STORE_DSN_REQUIRED: configure a PostgreSQL Session store "
            "or platform Kernel store"
        )
    _validate_pg_target(dsn)
    service = PostgresSessionService(
        dsn=dsn,
        namespace=namespace,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )
    return KernelSessionStorage(
        service,
        StorageTarget(backend="postgres", dsn=dsn, source=source),
        True,
    )
