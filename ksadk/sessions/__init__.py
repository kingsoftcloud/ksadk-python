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


__all__ = [
    "BaseSessionService",
    "EngineSessionService",
    "InMemorySessionService",
    "Session",
    "SessionEvent",
    "SessionState",
    "get_session_service",
]
