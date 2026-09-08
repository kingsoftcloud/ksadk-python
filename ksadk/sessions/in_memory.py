from __future__ import annotations

import asyncio
import bisect
import copy
import time
from typing import AsyncIterator, Optional

from ksadk.ids import new_session_id
from ksadk.sessions.base import (
    CANONICAL_EVENT_STORAGE_CAPABILITIES,
    BaseSessionService,
    CheckpointEventQuery,
    Session,
    SessionEvent,
    SessionEventQuery,
    SessionState,
    generate_id,
)


class _CheckpointSnapshotView:
    """Compatibility view over task-scoped checkpoint snapshots."""

    def __init__(self, snapshots: dict[asyncio.Task[object], int]) -> None:
        self._snapshots = snapshots

    def get(self) -> int | None:
        task = asyncio.current_task()
        return self._snapshots.get(task) if task is not None else None


class InMemorySessionService(BaseSessionService):
    storage_capabilities = CANONICAL_EVENT_STORAGE_CAPABILITIES

    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._events_by_id: dict[str, SessionEvent] = {}
        self._events_by_invocation: dict[tuple[str, str], list[SessionEvent]] = {}
        self._states: dict[tuple[str, str, str, str], SessionState] = {}
        self._event_order: list[tuple[float, str, int, str, int, SessionEvent]] = []
        self._event_generation = 0
        self._checkpoint_snapshot_by_task: dict[asyncio.Task[object], int] = {}
        self._checkpoint_snapshot_generation = _CheckpointSnapshotView(
            self._checkpoint_snapshot_by_task
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

    async def delete_session(self, session_id: str) -> bool:
        async with self._checkpoint_scan_lock:
            async with self._lock:
                session = self._sessions.pop(session_id, None)
                if not session:
                    return False
                for event in session.events:
                    self._events_by_id.pop(event.id, None)
                    if event.invocation_id is not None:
                        self._events_by_invocation.pop((session_id, event.invocation_id), None)
                self._event_order = [item for item in self._event_order if item[1] != session_id]
                self._states.pop(
                    self._state_key(
                        "session", session.agent_id, session.user_id, session_id
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

    async def get_sessions_by_ids(self, session_ids: list[str]) -> list[Session]:
        async with self._lock:
            return [
                self._session_metadata(self._sessions[session_id])
                for session_id in session_ids
                if session_id in self._sessions
            ]

    @staticmethod
    def _session_metadata(session: Session) -> Session:
        metadata = copy.deepcopy(session)
        metadata.events = []
        return metadata

    async def query_events(self, query: SessionEventQuery) -> list[SessionEvent]:
        return await self._query_events(query, count_only=False)

    async def count_event_query(self, query: SessionEventQuery) -> int:
        return int(await self._query_events(query, count_only=True))

    async def _query_events(
        self, query: SessionEventQuery, *, count_only: bool
    ) -> list[SessionEvent] | int:
        async with self._lock:
            selected_ids = (
                list(dict.fromkeys(query.session_ids))
                if query.session_ids is not None
                else list(self._sessions)
            )
            allowed_types = set(query.event_types or [])
            allowed_checkpoint_ids = set(query.checkpoint_ids or [])
            events = [
                event
                for session_id in selected_ids
                for session in [self._sessions.get(session_id)]
                if session is not None
                and (query.agent_id is None or session.agent_id == query.agent_id)
                for event in session.events
                if (query.after_seq_id is None or event.seq_id > query.after_seq_id)
                and (query.before_seq_id is None or event.seq_id < query.before_seq_id)
                and (not allowed_types or event.event_type in allowed_types)
                and (query.invocation_id is None or event.invocation_id == query.invocation_id)
                and (
                    query.run_id is None
                    or str((event.metadata or {}).get("run_id") or "") == query.run_id
                    or (
                        event.event_type == "continuation.created"
                        and str(
                            ((event.content or {}).get("runtime_event") or {}).get(
                                "run_id", ""
                            )
                            or ""
                        )
                        == query.run_id
                    )
                )
                and (
                    query.checkpoint_id is None
                    or str((event.metadata or {}).get("checkpoint_id") or "")
                    == query.checkpoint_id
                    or (
                        event.event_type == "continuation.created"
                        and str(
                            ((event.content or {}).get("runtime_event") or {}).get(
                                "continuation_id", ""
                            )
                            or ""
                        )
                        == query.checkpoint_id
                    )
                )
                and (
                    not allowed_checkpoint_ids
                    or str((event.metadata or {}).get("checkpoint_id") or "")
                    in allowed_checkpoint_ids
                    or (
                        event.event_type == "continuation.created"
                        and str(
                            ((event.content or {}).get("runtime_event") or {}).get(
                                "continuation_id", ""
                            )
                            or ""
                        )
                        in allowed_checkpoint_ids
                    )
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

    async def get_checkpoint_lookup_stats(
        self, session_id: str, run_id: str, checkpoint_id: str
    ) -> dict[str, object]:
        async with self._lock:
            session = self._sessions.get(session_id)
            candidate = None
            max_seq_id = 0
            resume_count = 0
            last_resumed_at = None
            for event in session.events if session else []:
                metadata = event.metadata or {}
                if str(metadata.get("run_id") or "") != run_id:
                    continue
                if event.event_type == "run_checkpoint":
                    max_seq_id = max(max_seq_id, int(event.seq_id or 0))
                    if str(metadata.get("checkpoint_id") or "") == checkpoint_id and (
                        candidate is None or event.seq_id > candidate.seq_id
                    ):
                        candidate = copy.deepcopy(event)
                elif event.event_type == "run_resume" and str(
                    metadata.get("checkpoint_id") or ""
                ) == checkpoint_id:
                    resume_count += 1
                    last_resumed_at = max(last_resumed_at or event.timestamp, event.timestamp)
            return {
                "candidate": candidate,
                "max_seq_id": max_seq_id,
                "resume_count": resume_count,
                "last_resumed_at": last_resumed_at,
            }

    async def scan_checkpoint_events(
        self, query: CheckpointEventQuery
    ) -> list[SessionEvent]:
        if query.limit < 1 or query.limit > 50:
            raise ValueError("checkpoint scan limit must be between 1 and 50")
        async with self._lock:
            return self._scan_checkpoint_events_locked(query)

    def _scan_checkpoint_events_locked(
        self, query: CheckpointEventQuery, snapshot_generation: int | None = None
    ) -> list[SessionEvent]:
        selected_ids = None if query.session_ids is None else set(query.session_ids)
        checkpoint_ids = set(query.checkpoint_ids or [])
        framework = str(query.framework or "").lower()
        matches: list[SessionEvent] = []
        for _, session_id, _, _, generation, event in self._event_order:
            session = self._sessions.get(session_id)
            metadata = event.metadata or {}
            if (
                session is None
                or (snapshot_generation is not None and generation > snapshot_generation)
                or event.event_type not in {"run_checkpoint", "continuation.created"}
                or (selected_ids is not None and session_id not in selected_ids)
                or (query.agent_id is not None and session.agent_id != query.agent_id)
                or (
                    event.event_type == "run_checkpoint"
                    and checkpoint_ids
                    and str(metadata.get("checkpoint_id") or "") not in checkpoint_ids
                )
                or (
                    event.event_type == "run_checkpoint"
                    and query.run_id is not None
                    and str(metadata.get("run_id") or "") != query.run_id
                )
                or (
                    event.event_type == "run_checkpoint"
                    and framework
                    and str(metadata.get("framework") or "").lower() != framework
                )
            ):
                continue
            matches.append(event)
        return copy.deepcopy(matches[query.offset : query.offset + query.limit])

    async def iter_checkpoint_event_chunks(
        self, query: CheckpointEventQuery
    ) -> AsyncIterator[list[SessionEvent]]:
        if query.limit < 1 or query.limit > 50:
            raise ValueError("checkpoint scan limit must be between 1 and 50")
        async with self._checkpoint_scan_lock:
            async with self._lock:
                snapshot_generation = self._event_generation
            task = asyncio.current_task()
            if task is not None:
                self._checkpoint_snapshot_by_task[task] = snapshot_generation
            try:
                offset = query.offset
                while True:
                    async with self._lock:
                        batch = self._scan_checkpoint_events_locked(
                            CheckpointEventQuery(**{**query.__dict__, "offset": offset}),
                            snapshot_generation,
                        )
                    if not batch:
                        break
                    yield batch
                    offset += len(batch)
                    if len(batch) < query.limit:
                        break
            finally:
                if task is not None:
                    self._checkpoint_snapshot_by_task.pop(task, None)

    async def get_checkpoint_stats(
        self, keys: list[tuple[str, str, str]]
    ) -> dict[str, object]:
        if len(keys) > 50:
            raise ValueError("checkpoint stats batch cannot exceed 50 keys")
        unique_keys = list(dict.fromkeys(keys))
        audits = {key: {"resume_count": 0, "last_resumed_at": None} for key in unique_keys}
        run_keys = {(session_id, run_id) for session_id, run_id, _ in unique_keys}
        latest_seq_ids = {key: 0 for key in run_keys}
        async with self._lock:
            task = asyncio.current_task()
            snapshot_generation = (
                self._checkpoint_snapshot_by_task.get(task) if task is not None else None
            )
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
                    key = (session_id, run_id, str(metadata.get("checkpoint_id") or ""))
                    if key in audits:
                        audit = audits[key]
                        audit["resume_count"] = int(audit["resume_count"]) + 1
                        audit["last_resumed_at"] = max(
                            audit["last_resumed_at"] or event.timestamp, event.timestamp
                        )
        return {"audits": audits, "latest_seq_ids": latest_seq_ids}

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
