"""Runtime-safe PostgreSQL persistence readiness diagnostics."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from copy import deepcopy
from typing import Any, Awaitable, Callable, Mapping

from ksadk.sessions import resolve_persistence_topology
from ksadk.sessions.topology import StorageTarget

ConnectCallable = Callable[..., Awaitable[Any]]

_STATUS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = asyncio.Lock()


class _NoopAsyncLock:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: Any) -> None:
        return None


def _base_status(*, target: StorageTarget, configured: bool) -> dict[str, Any]:
    return {
        "Configured": configured,
        "Status": "checking" if configured else "not_configured",
        "Ready": False,
        "Backend": target.backend,
        "SharedAcrossPods": False,
        "EffectiveFor": "new_runs_only",
        "Source": target.source,
        "ReasonCode": "",
        "Reason": "",
    }


def _not_configured_target_status(target: StorageTarget, *, store: str) -> dict[str, Any]:
    status = _base_status(target=target, configured=False)
    prefix = f"{store}_" if store else ""
    label = f"{store.title()} storage" if store else "Storage"
    status.update(
        {
            "ReasonCode": f"{prefix}STORE_NOT_CONFIGURED",
            "Reason": f"{label} is not configured",
        }
    )
    return status


def _error_status(target: StorageTarget, *, code: str, reason: str) -> dict[str, Any]:
    status = _base_status(target=target, configured=True)
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
    return "STORE_UNREACHABLE", "PostgreSQL persistence is unreachable"


def _cache_key(backend: str, dsn: str) -> str:
    digest = hashlib.sha256(dsn.encode("utf-8")).hexdigest()
    return f"{backend}:{digest}"


def _asyncpg_dsn(dsn: str) -> str:
    """Translate ADK's SQLAlchemy asyncpg URL to asyncpg's native DSN."""
    prefix = "postgresql+asyncpg://"
    if dsn.lower().startswith(prefix):
        return "postgresql://" + dsn[len(prefix) :]
    return dsn


async def _default_connect(**kwargs: Any) -> Any:
    import asyncpg

    return await asyncpg.connect(**kwargs)


def _status_for_target(
    status: Mapping[str, Any], target: StorageTarget, *, store: str = ""
) -> dict[str, Any]:
    result = deepcopy(dict(status))
    result.update({"Backend": target.backend, "Source": target.source})
    if result.get("ReasonCode") == "STORE_UNREACHABLE" and store:
        result["ReasonCode"] = f"{store}_STORE_UNREACHABLE"
    return result


async def probe_storage_target(
    target: StorageTarget,
    *,
    connect: ConnectCallable | None = None,
    use_cache: bool = True,
    store: str = "",
) -> dict[str, Any]:
    """Probe one storage target without exposing its DSN in the result."""
    if target.backend != "postgres" or not target.dsn:
        return _not_configured_target_status(target, store=store)

    result = await _probe_postgres_target(target, connect=connect, use_cache=use_cache)
    return _status_for_target(result, target, store=store)


async def _probe_postgres_target(
    target: StorageTarget,
    *,
    connect: ConnectCallable | None,
    use_cache: bool,
) -> dict[str, Any]:
    dsn = _asyncpg_dsn(target.dsn)

    timeout = max(0.1, float(os.getenv("KSADK_PERSISTENCE_PROBE_TIMEOUT") or "2"))
    ttl = max(0.0, float(os.getenv("KSADK_PERSISTENCE_PROBE_CACHE_TTL") or "30"))
    key = _cache_key(target.backend, dsn)
    now = time.monotonic()
    if use_cache:
        cached = _STATUS_CACHE.get(key)
        if cached and now - cached[0] < ttl:
            return deepcopy(cached[1])

    connector = connect or _default_connect
    connection = None
    try:
        lock = _CACHE_LOCK if use_cache else _NoopAsyncLock()
        async with lock:
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
                    target,
                    code="SCHEMA_PERMISSION_DENIED",
                    reason=(
                        "PostgreSQL account cannot create persistence tables "
                        "in the current schema"
                    ),
                )
            else:
                result = _base_status(target=target, configured=True)
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
            return deepcopy(result)
    except Exception as exc:
        code, reason = _classify_probe_error(exc)
        result = _error_status(target, code=code, reason=reason)
        if use_cache:
            _STATUS_CACHE[key] = (time.monotonic(), deepcopy(result))
        return deepcopy(result)
    finally:
        if connection is not None:
            try:
                await asyncio.wait_for(connection.close(), timeout=timeout)
            except Exception:
                pass


async def get_persistence_status(
    *,
    framework: str | None = None,
    connect: ConnectCallable | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Return independent credential-free Session and Checkpoint readiness."""
    topology = resolve_persistence_topology(framework=framework)
    probe_tasks: dict[str, asyncio.Task[dict[str, Any]]] = {}

    async def probe(target: StorageTarget, *, store: str) -> dict[str, Any]:
        if target.backend != "postgres" or not target.dsn:
            return _not_configured_target_status(target, store=store)
        key = _cache_key(target.backend, _asyncpg_dsn(target.dsn))
        if key not in probe_tasks:
            probe_tasks[key] = asyncio.create_task(
                _probe_postgres_target(
                    target,
                    connect=connect,
                    use_cache=use_cache,
                )
            )
        return _status_for_target(await probe_tasks[key], target, store=store)

    session_status, checkpoint_status = await asyncio.gather(
        probe(topology.session, store="SESSION"),
        probe(topology.checkpoint, store="CHECKPOINT"),
    )
    return {"Session": session_status, "Checkpoint": checkpoint_status}


def gate_runtime_capabilities(
    capabilities: Mapping[str, Any] | None,
    session_persistence: Mapping[str, Any],
    checkpoint_persistence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fail closed unless Session and Checkpoint persistence are both ready."""

    gated = deepcopy(dict(capabilities or {}))
    checkpoint_persistence = checkpoint_persistence or session_persistence
    blocked_store = (
        "session" if session_persistence.get("Ready") is not True else "checkpoint"
    )
    unavailable = (
        session_persistence if blocked_store == "session" else checkpoint_persistence
    )
    if unavailable.get("Ready") is True:
        return gated

    reason = str(unavailable.get("Reason") or "PostgreSQL persistence is unavailable")
    reason_code = str(
        unavailable.get("ReasonCode") or "RUNTIME_CAPABILITY_UNAVAILABLE"
    )
    checkpoint = dict(gated.get("Checkpoint") or {})
    native_backend = str(checkpoint.get("Backend") or "").strip()
    checkpoint.update(
        {
            "Supported": False,
            "Backend": "none",
            "Durable": False,
            "SharedAcrossPods": False,
            "ResumeMode": "none",
            "ReasonCode": reason_code,
            "Reason": reason,
            "PersistenceGate": {
                "BlockedStore": blocked_store,
                "Source": str(unavailable.get("Source") or "none"),
                "ReasonCode": reason_code,
                "Reason": reason,
            },
        }
    )
    if native_backend:
        checkpoint["NativeBackend"] = native_backend
    gated["Checkpoint"] = checkpoint
    gated["ResumeRun"] = {
        "Supported": False,
        "ResumeMode": "none",
        "ReasonCode": reason_code,
        "Reason": reason,
    }
    return gated


__all__ = ["gate_runtime_capabilities", "get_persistence_status", "probe_storage_target"]
