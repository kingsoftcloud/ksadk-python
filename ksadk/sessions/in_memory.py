from __future__ import annotations

import asyncio
import copy
import time
from typing import Optional

from ksadk.ids import new_session_id
from ksadk.sessions.base import (
    CANONICAL_EVENT_STORAGE_CAPABILITIES,
    BaseSessionService,
    Session,
    SessionEvent,
    SessionState,
    generate_id,
)


class InMemorySessionService(BaseSessionService):
    storage_capabilities = CANONICAL_EVENT_STORAGE_CAPABILITIES

    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._events_by_id: dict[str, SessionEvent] = {}
        self._events_by_invocation: dict[tuple[str, str], list[SessionEvent]] = {}
        self._states: dict[tuple[str, str, str, str], SessionState] = {}
        self._lock = asyncio.Lock()

    async def create_session(
        self,
        agent_id: str,
        user_id: str,
        session_id: Optional[str] = None,
    ) -> Session:
        async with self._lock:
            if session_id and session_id in self._sessions:
                return copy.deepcopy(self._sessions[session_id])

            session = Session(
                id=session_id or new_session_id(),
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

    async def get_session_metadata(self, session_id: str) -> Optional[Session]:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            metadata = copy.deepcopy(session)
            metadata.events = []
            return metadata

    async def list_sessions(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[Session]:
        async with self._lock:
            sessions = [
                copy.deepcopy(session)
                for session in self._sessions.values()
                if session.agent_id == agent_id and (user_id is None or session.user_id == user_id)
            ]
            sessions.sort(
                key=lambda item: (item.updated_at, item.created_at, item.id),
                reverse=True,
            )
            start = offset or 0
            end = None if limit is None else start + limit
            return sessions[start:end]

    async def count_sessions(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
    ) -> int:
        async with self._lock:
            return sum(
                1
                for session in self._sessions.values()
                if session.agent_id == agent_id and (user_id is None or session.user_id == user_id)
            )

    async def delete_session(self, session_id: str) -> bool:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            if not session:
                return False
            for event in session.events:
                self._events_by_id.pop(event.id, None)
                if event.invocation_id is not None:
                    self._events_by_invocation.pop((session_id, event.invocation_id), None)
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

    async def update_session_metadata(
        self,
        session_id: str,
        *,
        title: Optional[str] = None,
        title_source: Optional[str] = None,
        summary: Optional[str] = None,
        first_prompt: Optional[str] = None,
        last_prompt: Optional[str] = None,
    ) -> Session:
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise ValueError(f"Session {session_id} not found")
            if title is not None:
                session.title = title
            if title_source is not None:
                session.title_source = title_source
            if summary is not None:
                session.summary = summary
            if first_prompt is not None:
                session.first_prompt = first_prompt
            if last_prompt is not None:
                session.last_prompt = last_prompt
            session.updated_at = time.time()
            return copy.deepcopy(session)

    async def append_event(self, session_id: str, event: SessionEvent) -> SessionEvent:
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                raise ValueError(f"Session {session_id} not found")

            # Match the durable Local/Postgres physical primary-key contract.
            # Canonical RuntimeEvent storage relies on a deterministic
            # session+event storage id so concurrent insert losers cannot
            # allocate another seq.  Auto-generated ids retain their existing
            # behavior because SessionEvent always supplies a fresh id.
            if event.id in self._events_by_id:
                raise ValueError(f"SessionEvent id {event.id!r} already exists")

            stored = copy.deepcopy(event)
            stored.session_id = session_id
            stored.bind_seq_id(len(session.events) + 1)
            if not stored.id:
                stored.id = generate_id()
            session.events.append(stored)
            self._events_by_id[stored.id] = stored
            if stored.invocation_id is not None:
                self._events_by_invocation.setdefault(
                    (session_id, stored.invocation_id), []
                ).append(stored)
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

    async def get_event_by_id(self, session_id: str, event_id: str) -> Optional[SessionEvent]:
        async with self._lock:
            event = self._events_by_id.get(event_id)
            if event is None or event.session_id != session_id:
                return None
            return copy.deepcopy(event)

    async def get_events_by_invocation_id(
        self,
        session_id: str,
        invocation_id: str,
        *,
        after_seq_id: Optional[int] = None,
        before_seq_id: Optional[int] = None,
    ) -> list[SessionEvent]:
        async with self._lock:
            events = list(self._events_by_invocation.get((session_id, invocation_id), ()))
            if after_seq_id is not None:
                events = [event for event in events if event.seq_id > after_seq_id]
            if before_seq_id is not None:
                events = [event for event in events if event.seq_id < before_seq_id]
            return copy.deepcopy(events)

    async def get_events(
        self,
        session_id: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        after_seq_id: Optional[int] = None,
        before_seq_id: Optional[int] = None,
    ) -> list[SessionEvent]:
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return []
            events = list(session.events)
            if after_seq_id is not None:
                events = [event for event in events if event.seq_id > after_seq_id]
            if before_seq_id is not None:
                events = [event for event in events if event.seq_id < before_seq_id]
            end = max(len(events) - (offset or 0), 0)
            start = 0 if limit is None else max(end - limit, 0)
            sliced = events[start:end]
            return copy.deepcopy(sliced)

    async def count_events(
        self,
        session_id: str,
        after_seq_id: Optional[int] = None,
        before_seq_id: Optional[int] = None,
    ) -> int:
        async with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return 0
            events = list(session.events)
            if after_seq_id is not None:
                events = [event for event in events if event.seq_id > after_seq_id]
            if before_seq_id is not None:
                events = [event for event in events if event.seq_id < before_seq_id]
            return len(events)

    async def get_events_for_agent(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[SessionEvent]:
        async with self._lock:
            merged = [
                copy.deepcopy(event)
                for session in self._sessions.values()
                if session.agent_id == agent_id and (user_id is None or session.user_id == user_id)
                for event in session.events
            ]
            merged.sort(key=lambda event: (event.timestamp, event.seq_id, event.id))
            end = max(len(merged) - (offset or 0), 0)
            start = 0 if limit is None else max(end - limit, 0)
            return merged[start:end]

    async def count_events_for_agent(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
    ) -> int:
        async with self._lock:
            return sum(
                len(session.events)
                for session in self._sessions.values()
                if session.agent_id == agent_id and (user_id is None or session.user_id == user_id)
            )

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
