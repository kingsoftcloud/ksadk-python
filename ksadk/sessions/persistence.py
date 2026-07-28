"""Runtime-safe PostgreSQL persistence readiness diagnostics."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from copy import deepcopy
from typing import Any, Awaitable, Callable, Mapping

ConnectCallable = Callable[..., Awaitable[Any]]

_STATUS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = asyncio.Lock()


def _base_status(*, backend: str, configured: bool) -> dict[str, Any]:
    return {
        "Configured": configured,
        "Status": "checking" if configured else "not_configured",
        "Ready": False,
        "Backend": backend,
        "SharedAcrossPods": False,
        "EffectiveFor": "new_runs_only",
        "ReasonCode": "" if configured else "NOT_CONFIGURED",
        "Reason": "" if configured else "PostgreSQL persistence is not configured",
    }


def _error_status(*, code: str, reason: str) -> dict[str, Any]:
    status = _base_status(backend="postgres", configured=True)
    status.update({"Status": "error", "ReasonCode": code, "Reason": reason})
    return status


def _classify_probe_error(exc: BaseException) -> tuple[str, str]:
    name = type(exc).__name__.lower()
    if isinstance(exc, (ModuleNotFoundError, ImportError)):
        return "DEPENDENCY_MISSING", "asyncpg is required for PostgreSQL persistence"
    if "password" in name or "authorization" in name or "authentication" in name:
        return "AUTH_FAILED", "PostgreSQL authentication failed"
    if "privilege" in name or "permission" in name:
        return "SCHEMA_PERMISSION_DENIED", "PostgreSQL schema permissions are insufficient"
    return "DB_UNREACHABLE", "PostgreSQL persistence is unreachable"


def _cache_key(backend: str, dsn: str) -> str:
    digest = hashlib.sha256(dsn.encode("utf-8")).hexdigest()
    return f"{backend}:{digest}"


async def _default_connect(**kwargs: Any) -> Any:
    import asyncpg

    return await asyncpg.connect(**kwargs)


async def get_persistence_status(
    *,
    connect: ConnectCallable | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Return a credential-free persistence diagnostic for bootstrap consumers."""

    backend = str(os.getenv("KSADK_SESSION_BACKEND") or "local").strip().lower()
    if backend == "sqlite":
        backend = "local"
    dsn = str(os.getenv("KSADK_SESSION_DSN") or "").strip()
    if backend != "postgres":
        return _base_status(backend=backend or "local", configured=False)
    if not dsn:
        return _error_status(
            code="DB_UNREACHABLE",
            reason="PostgreSQL session DSN is missing",
        )

    timeout = max(0.1, float(os.getenv("KSADK_PERSISTENCE_PROBE_TIMEOUT") or "2"))
    ttl = max(0.0, float(os.getenv("KSADK_PERSISTENCE_PROBE_CACHE_TTL") or "30"))
    key = _cache_key(backend, dsn)
    now = time.monotonic()
    if use_cache:
        cached = _STATUS_CACHE.get(key)
        if cached and now - cached[0] < ttl:
            return deepcopy(cached[1])

    connector = connect or _default_connect
    connection = None
    try:
        async with _CACHE_LOCK:
            if use_cache:
                cached = _STATUS_CACHE.get(key)
                if cached and time.monotonic() - cached[0] < ttl:
                    return deepcopy(cached[1])
            connection = await asyncio.wait_for(
                connector(dsn=dsn, timeout=timeout, command_timeout=timeout),
                timeout=timeout,
            )
            await asyncio.wait_for(connection.fetchval("SELECT 1"), timeout=timeout)
            can_create = await asyncio.wait_for(
                connection.fetchval(
                    "SELECT has_schema_privilege(current_user, current_schema(), 'CREATE')"
                ),
                timeout=timeout,
            )
            if can_create is not True:
                result = _error_status(
                    code="SCHEMA_PERMISSION_DENIED",
                    reason=(
                        "PostgreSQL account cannot create persistence tables "
                        "in the current schema"
                    ),
                )
            else:
                result = _base_status(backend="postgres", configured=True)
                result.update(
                    {
                        "Status": "ready",
                        "Ready": True,
                        "SharedAcrossPods": True,
                        "ReasonCode": "READY",
                        "Reason": "",
                    }
                )
            if use_cache:
                _STATUS_CACHE[key] = (time.monotonic(), deepcopy(result))
            return result
    except Exception as exc:
        code, reason = _classify_probe_error(exc)
        result = _error_status(code=code, reason=reason)
        if use_cache:
            _STATUS_CACHE[key] = (time.monotonic(), deepcopy(result))
        return result
    finally:
        if connection is not None:
            try:
                await asyncio.wait_for(connection.close(), timeout=timeout)
            except Exception:
                pass


def gate_runtime_capabilities(
    capabilities: Mapping[str, Any] | None,
    persistence: Mapping[str, Any],
) -> dict[str, Any]:
    """Fail closed unless PostgreSQL persistence is positively ready."""

    gated = deepcopy(dict(capabilities or {}))
    if persistence.get("Ready") is True:
        return gated

    reason = str(persistence.get("Reason") or "PostgreSQL persistence is unavailable")
    reason_code = str(
        persistence.get("ReasonCode") or "RUNTIME_CAPABILITY_UNAVAILABLE"
    )
    checkpoint = dict(gated.get("Checkpoint") or {})
    checkpoint.update(
        {
            "Supported": False,
            "Durable": False,
            "SharedAcrossPods": False,
            "ResumeMode": "none",
            "ReasonCode": reason_code,
            "Reason": reason,
        }
    )
    gated["Checkpoint"] = checkpoint
    gated["ResumeRun"] = {
        "Supported": False,
        "ResumeMode": "none",
        "ReasonCode": reason_code,
        "Reason": reason,
    }
    return gated


__all__ = ["gate_runtime_capabilities", "get_persistence_status"]
