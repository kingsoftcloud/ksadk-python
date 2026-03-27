# ksadk/server/session_store.py
"""
Deprecated compatibility layer for the legacy in-process session store.
"""

from __future__ import annotations

import asyncio
import threading
import time
import warnings
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ksadk.sessions import SessionEvent, get_session_service


@dataclass
class Message:
    """Legacy message structure kept for compatibility."""

    role: str
    parts: List[Dict[str, Any]]
    event_id: Optional[str] = None
    invocation_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class Session:
    """Legacy session object exposed to older callers."""

    id: str
    app_name: str
    user_id: str
    events: List[Dict[str, Any]] = field(default_factory=list)
    state: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "appName": self.app_name,
            "userId": self.user_id,
            "events": self.events,
            "state": self.state,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


def _run_sync(awaitable):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    result: dict[str, Any] = {}

    def _runner():
        try:
            result["value"] = asyncio.run(awaitable)
        except BaseException as exc:  # pragma: no cover - defensive propagation
            result["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()

    if "error" in result:
        raise result["error"]
    return result.get("value")


def _legacy_session_from_new(session) -> Session:
    return Session(
        id=session.id,
        app_name=session.agent_id,
        user_id=session.user_id,
        events=[event.to_legacy_dict() for event in session.events],
        state=dict(session.state),
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


class InMemorySessionStore:
    """Legacy sync facade backed by the new session service."""

    _instance: Optional["InMemorySessionStore"] = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def create_session(self, app_name: str, user_id: str, events: List[Dict] = None) -> Session:
        service = get_session_service()
        session = _run_sync(service.create_session(app_name, user_id))
        for raw_event in events or []:
            event = SessionEvent.from_dict(raw_event, session_id=session.id)
            _run_sync(service.append_event(session.id, event))
        hydrated = _run_sync(service.get_session(session.id))
        if hydrated:
            hydrated.events = _run_sync(service.get_events(session.id))
            return _legacy_session_from_new(hydrated)
        return _legacy_session_from_new(session)

    def get_session(self, session_id: str) -> Optional[Session]:
        service = get_session_service()
        session = _run_sync(service.get_session(session_id))
        if not session:
            return None
        session.events = _run_sync(service.get_events(session.id))
        return _legacy_session_from_new(session)

    def list_sessions(self, app_name: str, user_id: str) -> List[Session]:
        service = get_session_service()
        sessions = _run_sync(service.list_sessions(app_name, user_id))
        items: List[Session] = []
        for session in sessions:
            session.events = _run_sync(service.get_events(session.id))
            items.append(_legacy_session_from_new(session))
        return items

    def delete_session(self, session_id: str) -> bool:
        return bool(_run_sync(get_session_service().delete_session(session_id)))

    def add_event(self, session_id: str, event: Dict[str, Any]) -> bool:
        try:
            _run_sync(
                get_session_service().append_event(
                    session_id,
                    SessionEvent.from_dict(event, session_id=session_id),
                )
            )
            return True
        except ValueError:
            return False

    def update_state(self, session_id: str, state_delta: Dict[str, Any]) -> bool:
        service = get_session_service()
        session = _run_sync(service.get_session(session_id))
        if not session:
            return False
        _run_sync(
            service.update_state(
                agent_id=session.agent_id,
                user_id=session.user_id,
                session_id=session.id,
                scope="session",
                state_delta=state_delta,
            )
        )
        return True


def get_session_store() -> InMemorySessionStore:
    warnings.warn(
        (
            "ksadk.server.session_store is deprecated; use "
            "ksadk.sessions.get_session_service() instead."
        ),
        DeprecationWarning,
        stacklevel=2,
    )
    return InMemorySessionStore()
