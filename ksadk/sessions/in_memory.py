from __future__ import annotations

import asyncio
import copy
import time
from typing import Optional

from ksadk.sessions.base import BaseSessionService, Session, SessionEvent, SessionState, generate_id


class InMemorySessionService(BaseSessionService):
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._states: dict[tuple[str, str, str, str], SessionState] = {}
        self._lock = asyncio.Lock()

    async def create_session(
        self,
        agent_id: str,
        user_id: str,
        session_id: Optional[str] = None,
    ) -> Session:
        async with self._lock:
            session = Session(
                id=session_id or generate_id(),
                agent_id=agent_id,
                user_id=user_id,
            )
            self._sessions[session.id] = session
            self._states[self._state_key("session", agent_id, user_id, session.id)] = SessionState(
                scope="session",
                agent_id=agent_id,
                user_id=user_id,
                session_id=session.id,
            )
            return copy.deepcopy(session)

    async def get_session(self, session_id: str) -> Optional[Session]:
        async with self._lock:
            session = self._sessions.get(session_id)
            return copy.deepcopy(session) if session else None

    async def list_sessions(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
    ) -> list[Session]:
        async with self._lock:
            sessions = [
                copy.deepcopy(session)
                for session in self._sessions.values()
                if session.agent_id == agent_id and (user_id is None or session.user_id == user_id)
            ]
            sessions.sort(key=lambda item: item.created_at)
            return sessions

    async def delete_session(self, session_id: str) -> bool:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            if not session:
                return False
            self._states.pop(
                self._state_key(
                    "session",
                    session.agent_id,
                    session.user_id,
                    session_id,
                ),
                None,
            )
            return True

    async def append_event(self, session_id: str, event: SessionEvent) -> SessionEvent:
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise ValueError(f"Session {session_id} not found")

            stored = copy.deepcopy(event)
            stored.session_id = session_id
            stored.seq_id = len(session.events) + 1
            if not stored.id:
                stored.id = generate_id()
            session.events.append(stored)
            session.updated_at = time.time()

            if stored.state_delta:
                session.state.update(stored.state_delta)
                session.version += 1
                self._states[
                    self._state_key(
                        "session",
                        session.agent_id,
                        session.user_id,
                        session.id,
                    )
                ] = SessionState(
                    scope="session",
                    agent_id=session.agent_id,
                    user_id=session.user_id,
                    session_id=session.id,
                    state=copy.deepcopy(session.state),
                    version=session.version,
                    updated_at=session.updated_at,
                )

            return copy.deepcopy(stored)

    async def get_events(
        self,
        session_id: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[SessionEvent]:
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return []
            start = offset or 0
            end = None if limit is None else start + limit
            events = session.events[start:end]
            return copy.deepcopy(events)

    async def get_state(
        self,
        agent_id: str,
        user_id: Optional[str],
        session_id: Optional[str],
        scope: str = "session",
    ) -> Optional[SessionState]:
        async with self._lock:
            if scope == "session" and session_id:
                session = self._sessions.get(session_id)
                if session:
                    return SessionState(
                        scope="session",
                        agent_id=session.agent_id,
                        user_id=session.user_id,
                        session_id=session.id,
                        state=copy.deepcopy(session.state),
                        version=session.version,
                        updated_at=session.updated_at,
                    )
            state = self._states.get(
                self._state_key(scope, agent_id, user_id or "", session_id or "")
            )
            return copy.deepcopy(state) if state else None

    async def update_state(
        self,
        *,
        agent_id: str,
        user_id: Optional[str],
        session_id: Optional[str],
        scope: str,
        state_delta: dict,
    ) -> SessionState:
        async with self._lock:
            user_key = user_id or ""
            session_key = session_id or ""
            state_key = self._state_key(scope, agent_id, user_key, session_key)
            current = self._states.get(state_key)

            if scope == "session":
                session = self._sessions.get(session_key)
                if not session:
                    raise ValueError(f"Session {session_key} not found")
                session.state.update(state_delta)
                session.version += 1
                session.updated_at = time.time()
                updated = SessionState(
                    scope="session",
                    agent_id=session.agent_id,
                    user_id=session.user_id,
                    session_id=session.id,
                    state=copy.deepcopy(session.state),
                    version=session.version,
                    updated_at=session.updated_at,
                )
            else:
                next_state = copy.deepcopy(current.state) if current else {}
                next_state.update(state_delta)
                updated = SessionState(
                    scope=scope,
                    agent_id=agent_id,
                    user_id=user_key,
                    session_id=session_key,
                    state=next_state,
                    version=(current.version if current else 0) + 1,
                    updated_at=time.time(),
                )

            self._states[state_key] = updated
            return copy.deepcopy(updated)

    @staticmethod
    def _state_key(
        scope: str,
        agent_id: str,
        user_id: str,
        session_id: str,
    ) -> tuple[str, str, str, str]:
        return (scope, agent_id, user_id, session_id)
