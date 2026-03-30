from __future__ import annotations

import os

from ksadk.sessions.base import BaseSessionService, Session, SessionEvent, SessionState
from ksadk.sessions.engine_service import EngineSessionService
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.sessions.local_service import create_local_session_service

_cached_session_service: BaseSessionService | None = None


def create_session_service(
    endpoint: str | None = None,
    *,
    backend: str | None = None,
    project_dir: str | None = None,
) -> BaseSessionService:
    resolved_endpoint = (endpoint or os.getenv("AGENTENGINE_SESSION_ENDPOINT", "")).strip()
    resolved_backend = (backend or os.getenv("AGENTENGINE_SESSION_BACKEND", "")).strip().lower()

    if resolved_endpoint or resolved_backend == "engine":
        return EngineSessionService(endpoint=resolved_endpoint)
    if resolved_backend == "memory":
        return InMemorySessionService()
    return create_local_session_service(project_dir=project_dir)


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
    "BaseSessionService",
    "EngineSessionService",
    "InMemorySessionService",
    "Session",
    "SessionEvent",
    "SessionState",
    "close_session_service",
    "create_session_service",
    "get_session_service",
    "reset_session_service",
    "resolve_session_service",
]
