from __future__ import annotations

import os

from ksadk.sessions.base import BaseSessionService, Session, SessionEvent, SessionState
from ksadk.sessions.engine_service import EngineSessionService
from ksadk.sessions.in_memory import InMemorySessionService

_service_instance: BaseSessionService | None = None


def get_session_service() -> BaseSessionService:
    global _service_instance
    if _service_instance is not None:
        return _service_instance

    endpoint = os.getenv("AGENTENGINE_SESSION_ENDPOINT", "").strip()
    if endpoint:
        _service_instance = EngineSessionService(endpoint=endpoint)
    else:
        _service_instance = InMemorySessionService()
    return _service_instance


async def close_session_service() -> None:
    global _service_instance
    if _service_instance is None:
        return

    close = getattr(_service_instance, "aclose", None)
    if close is not None:
        await close()
    _service_instance = None


__all__ = [
    "BaseSessionService",
    "EngineSessionService",
    "InMemorySessionService",
    "Session",
    "SessionEvent",
    "SessionState",
    "close_session_service",
    "get_session_service",
]
