from __future__ import annotations

import asyncio
import bisect
import copy
import time
from contextvars import ContextVar
from typing import AsyncIterator, Optional

from ksadk.ids import new_session_id
from ksadk.sessions.base import (
    BaseSessionService,
    CheckpointEventQuery,
    Session,
    SessionEvent,
    SessionEventQuery,
    SessionState,
    generate_id,
)


class InMemorySessionService(BaseSessionService):
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._states: dict[tuple[str, str, str, str], SessionState] = {}
        self._event_order: list[tuple[float, str, int, str, int, SessionEvent]] = []
        self._event_generation = 0
        self._checkpoint_snapshot_generation: ContextVar[int | None] = ContextVar(
            f"checkpoint_snapshot_generation_{id(self)}", default=None
        )
        self._lock = asyncio.Lock()
        self._checkpoint_scan_lock = asyncio.Lock()

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

    async def list_sessions(
        self,
        agent_id: Optional[str],
        user_id: Optional[str] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[Session]:
        async with self._lock:
            sessions = [
                copy.deepcopy(session)
                for session in self._sessions.values()
                if (agent_id is None or session.agent_id == agent_id)
                and (user_id is None or session.user_id == user_id)
            ]
            sessions.sort(
                key=lambda item: (item.updated_at, item.created_at, item.id),
                reverse=True,
            )
            start = offset or 0
            end = None if limit is None else start + limit
            return sessions[start:end]

    async def list_session_metadata(
        self, agent_id: Optional[str] = None, user_id: Optional[str] = None
    ) -> list[Session]:
        async with self._lock:
            sessions = [
                self._session_metadata(session)
                for session in self._sessions.values()
                if (agent_id is None or session.agent_id == agent_id)
                and (user_id is None or session.user_id == user_id)
            ]
            sessions.sort(key=lambda item: (item.updated_at, item.created_at), reverse=True)
            return sessions

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
        async with self._checkpoint_scan_lock:
            async with self._lock:
                session = self._sessions.pop(session_id, None)
                if not session:
                    return False
                self._event_order = [
                    item for item in self._event_order if item[1] != session_id
                ]
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

            stored = copy.deepcopy(event)
            stored.session_id = session_id
            stored.seq_id = len(session.events) + 1
            if not stored.id:
                stored.id = generate_id()
            session.events.append(stored)
            self._event_generation += 1
            bisect.insort(
                self._event_order,
                (
                    stored.timestamp,
                    stored.session_id,
                    stored.seq_id,
                    stored.id,
                    self._event_generation,
                    stored,
                ),
            )
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

    async def get_sessions_by_ids(self, session_ids: list[str]) -> list[Session]:
        async with self._lock:
            return [
                self._session_metadata(self._sessions[session_id])
                for session_id in session_ids
                if session_id in self._sessions
            ]

    async def get_session_metadata(self, session_id: str) -> Optional[Session]:
        async with self._lock:
            session = self._sessions.get(session_id)
            return self._session_metadata(session) if session else None

    @staticmethod
    def _session_metadata(session: Session) -> Session:
        return Session(
            id=session.id, agent_id=session.agent_id, user_id=session.user_id,
            title=session.title, title_source=session.title_source, summary=session.summary,
            first_prompt=session.first_prompt, last_prompt=session.last_prompt,
            state=copy.deepcopy(session.state), events=[], created_at=session.created_at,
            updated_at=session.updated_at, version=session.version,
        )

    async def query_events(self, query: SessionEventQuery) -> list[SessionEvent]:
        return await self._query_events(query, count_only=False)

    async def count_event_query(self, query: SessionEventQuery) -> int:
        return int(await self._query_events(query, count_only=True))

    async def get_checkpoint_lookup_stats(
        self, session_id: str, run_id: str, checkpoint_id: str
    ) -> dict[str, object]:
        async with self._lock:
            session = self._sessions.get(session_id)
            candidate = None
            max_seq_id = 0
            resume_count = 0
            last_resumed_at = None
            for event in (session.events if session else []):
                metadata = event.metadata or {}
                if str(metadata.get("run_id") or "") != run_id:
                    continue
                if event.event_type == "run_checkpoint":
                    max_seq_id = max(max_seq_id, int(event.seq_id or 0))
                    if str(metadata.get("checkpoint_id") or "") == checkpoint_id and (
                        candidate is None or event.seq_id > candidate.seq_id
                    ):
                        candidate = copy.deepcopy(event)
                elif event.event_type == "run_resume" and str(metadata.get("checkpoint_id") or "") == checkpoint_id:
                    resume_count += 1
                    last_resumed_at = max(last_resumed_at or event.timestamp, event.timestamp)
            return {"candidate": candidate, "max_seq_id": max_seq_id,
                    "resume_count": resume_count, "last_resumed_at": last_resumed_at}

    async def scan_checkpoint_events(
        self, query: CheckpointEventQuery
    ) -> list[SessionEvent]:
        if query.limit < 1 or query.limit > 50:
            raise ValueError("checkpoint scan limit must be between 1 and 50")
        async with self._lock:
            selected_ids = None if query.session_ids is None else set(query.session_ids)
            checkpoint_ids = set(query.checkpoint_ids or [])
            framework = str(query.framework or "").lower()
            skipped = 0
            page: list[SessionEvent] = []
            for _, session_id, _, _, _, event in self._event_order:
                session = self._sessions.get(session_id)
                metadata = event.metadata or {}
                if (
                    session is None
                    or event.event_type != "run_checkpoint"
                    or (selected_ids is not None and session_id not in selected_ids)
                    or (query.agent_id is not None and session.agent_id != query.agent_id)
                    or (checkpoint_ids and str(metadata.get("checkpoint_id") or "") not in checkpoint_ids)
                    or (query.run_id is not None and str(metadata.get("run_id") or "") != query.run_id)
                    or (framework and str(metadata.get("framework") or "").lower() != framework)
                ):
                    continue
                if skipped < query.offset:
                    skipped += 1
                    continue
                page.append(copy.deepcopy(event))
                if len(page) == query.limit:
                    break
            return page

    async def iter_checkpoint_event_chunks(
        self, query: CheckpointEventQuery
    ) -> AsyncIterator[list[SessionEvent]]:
        if query.limit < 1 or query.limit > 50:
            raise ValueError("checkpoint scan limit must be between 1 and 50")
        selected_ids = None if query.session_ids is None else set(query.session_ids)
        checkpoint_ids = set(query.checkpoint_ids or [])
        framework = str(query.framework or "").lower()
        cursor: tuple[float, str, int, str] | None = None
        skipped = 0
        async with self._checkpoint_scan_lock:
            async with self._lock:
                snapshot_generation = self._event_generation
            snapshot_token = self._checkpoint_snapshot_generation.set(
                snapshot_generation
            )
            try:
                while True:
                    page: list[SessionEvent] = []
                    async with self._lock:
                        order_index = self._event_order_index_after(cursor)
                        while (
                            order_index < len(self._event_order)
                            and len(page) < query.limit
                        ):
                            (
                                timestamp,
                                session_id,
                                seq_id,
                                event_id,
                                generation,
                                event,
                            ) = self._event_order[order_index]
                            order_index += 1
                            cursor = (timestamp, session_id, seq_id, event_id)
                            session = self._sessions.get(session_id)
                            metadata = event.metadata or {}
                            if (
                                generation > snapshot_generation
                                or session is None
                                or event.event_type != "run_checkpoint"
                                or (
                                    selected_ids is not None
                                    and session_id not in selected_ids
                                )
                                or (
                                    query.agent_id is not None
                                    and session.agent_id != query.agent_id
                                )
                                or (
                                    checkpoint_ids
                                    and str(metadata.get("checkpoint_id") or "")
                                    not in checkpoint_ids
                                )
                                or (
                                    query.run_id is not None
                                    and str(metadata.get("run_id") or "") != query.run_id
                                )
                                or (
                                    framework
                                    and str(metadata.get("framework") or "").lower()
                                    != framework
                                )
                            ):
                                continue
                            if skipped < query.offset:
                                skipped += 1
                                continue
                            page.append(copy.deepcopy(event))
                        exhausted = order_index >= len(self._event_order)
                    if page:
                        yield page
                    if exhausted:
                        break
            finally:
                self._checkpoint_snapshot_generation.reset(snapshot_token)

    def _event_order_index_after(
        self, cursor: tuple[float, str, int, str] | None
    ) -> int:
        if cursor is None:
            return 0
        lower = 0
        upper = len(self._event_order)
        while lower < upper:
            middle = (lower + upper) // 2
            if self._event_order[middle][:4] <= cursor:
                lower = middle + 1
            else:
                upper = middle
        return lower

    async def get_checkpoint_stats(
        self, keys: list[tuple[str, str, str]]
    ) -> dict[str, object]:
        if len(keys) > 50:
            raise ValueError("checkpoint stats batch cannot exceed 50 keys")
        unique_keys = list(dict.fromkeys(keys))
        audits = {
            key: {"resume_count": 0, "last_resumed_at": None}
            for key in unique_keys
        }
        run_keys = {(session_id, run_id) for session_id, run_id, _ in unique_keys}
        latest_seq_ids = {key: 0 for key in run_keys}
        async with self._lock:
            snapshot_generation = self._checkpoint_snapshot_generation.get()
            for _, session_id, _, _, generation, event in self._event_order:
                if snapshot_generation is not None and generation > snapshot_generation:
                    continue
                metadata = event.metadata or {}
                run_id = str(metadata.get("run_id") or "")
                run_key = (session_id, run_id)
                if run_key not in run_keys:
                    continue
                if event.event_type == "run_checkpoint":
                    latest_seq_ids[run_key] = max(
                        latest_seq_ids[run_key], int(event.seq_id or 0)
                    )
                elif event.event_type == "run_resume":
                    key = (
                        session_id,
                        run_id,
                        str(metadata.get("checkpoint_id") or ""),
                    )
                    if key in audits:
                        audit = audits[key]
                        audit["resume_count"] = int(audit["resume_count"]) + 1
                        audit["last_resumed_at"] = max(
                            audit["last_resumed_at"] or event.timestamp,
                            event.timestamp,
                        )
        return {"audits": audits, "latest_seq_ids": latest_seq_ids}

    async def _query_events(self, query: SessionEventQuery, *, count_only: bool) -> list[SessionEvent] | int:
        async with self._lock:
            selected_ids = list(dict.fromkeys(query.session_ids)) if query.session_ids is not None else list(self._sessions)
            allowed_types = set(query.event_types or [])
            allowed_checkpoint_ids = set(query.checkpoint_ids or [])
            events = [
                event for session_id in selected_ids for session in [self._sessions.get(session_id)]
                if session is not None and (query.agent_id is None or session.agent_id == query.agent_id)
                for event in session.events
                if (query.after_seq_id is None or event.seq_id > query.after_seq_id)
                and (query.before_seq_id is None or event.seq_id < query.before_seq_id)
                and (not allowed_types or event.event_type in allowed_types)
                and (
                    query.invocation_id is None
                    or event.invocation_id == query.invocation_id
                )
                and (query.run_id is None or str((event.metadata or {}).get("run_id") or "") == query.run_id)
                and (query.checkpoint_id is None or str((event.metadata or {}).get("checkpoint_id") or "") == query.checkpoint_id)
                and (
                    not allowed_checkpoint_ids
                    or str((event.metadata or {}).get("checkpoint_id") or "")
                    in allowed_checkpoint_ids
                )
            ]
            if count_only:
                return len(events)
            if query.order_by_seq:
                events.sort(key=lambda event: (event.session_id, event.seq_id, event.id))
            else:
                events.sort(
                    key=lambda event: (
                        event.timestamp,
                        event.session_id,
                        event.seq_id,
                        event.id,
                    )
                )
            if query.from_start:
                return copy.deepcopy(events[query.offset : query.offset + query.limit])
            end = max(len(events) - query.offset, 0)
            return copy.deepcopy(events[max(end - query.limit, 0) : end])

    async def get_events_batch(
        self,
        session_ids: list[str] | None = None,
        *,
        agent_id: str | None = None,
        offset: int = 0,
        limit: int = 1000,
        after_seq_id: int | None = None,
        before_seq_id: int | None = None,
        event_types: list[str] | None = None,
        from_start: bool = False,
    ) -> list[SessionEvent]:
        return await self.query_events(SessionEventQuery(
            session_ids=session_ids, agent_id=agent_id, offset=offset, limit=limit,
            after_seq_id=after_seq_id, before_seq_id=before_seq_id,
            event_types=event_types, from_start=from_start,
        ))

    async def count_events_batch(
        self,
        session_ids: list[str] | None = None,
        *,
        agent_id: str | None = None,
        after_seq_id: int | None = None,
        before_seq_id: int | None = None,
        event_types: list[str] | None = None,
    ) -> int:
        return await self.count_event_query(SessionEventQuery(
            session_ids=session_ids, agent_id=agent_id, after_seq_id=after_seq_id,
            before_seq_id=before_seq_id, event_types=event_types,
        ))

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
