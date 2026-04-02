"""Local persistent session backend for embedded KSADK runtimes."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

from ksadk.sessions.base import BaseSessionService, Session, SessionEvent, SessionState, generate_id


def resolve_local_session_dir(project_dir: Optional[str] = None) -> Path:
    configured = (os.getenv("AGENTENGINE_UI_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()

    root = Path(project_dir or os.getenv("KSADK_PROJECT_DIR") or os.getcwd()).resolve()
    return root / ".agentengine" / "ui"


class LocalSessionService(BaseSessionService):
    def __init__(self, db_path: Optional[Path] = None, *, project_dir: Optional[str] = None):
        ui_dir = (
            resolve_local_session_dir(project_dir)
            if db_path is None
            else Path(db_path).expanduser().resolve().parent
        )
        self.db_path = Path(db_path).expanduser().resolve() if db_path else ui_dir / "sessions.sqlite"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._ensure_schema()

    async def create_session(
        self,
        agent_id: str,
        user_id: str,
        session_id: Optional[str] = None,
    ) -> Session:
        async with self._lock:
            return await asyncio.to_thread(self._create_session_sync, agent_id, user_id, session_id)

    async def get_session(self, session_id: str) -> Optional[Session]:
        async with self._lock:
            return await asyncio.to_thread(self._get_session_sync, session_id)

    async def list_sessions(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
    ) -> list[Session]:
        async with self._lock:
            return await asyncio.to_thread(self._list_sessions_sync, agent_id, user_id)

    async def delete_session(self, session_id: str) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._delete_session_sync, session_id)

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
            return await asyncio.to_thread(
                self._update_session_metadata_sync,
                session_id,
                title,
                title_source,
                summary,
                first_prompt,
                last_prompt,
            )

    async def append_event(self, session_id: str, event: SessionEvent) -> SessionEvent:
        async with self._lock:
            return await asyncio.to_thread(self._append_event_sync, session_id, event)

    async def get_events(
        self,
        session_id: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[SessionEvent]:
        async with self._lock:
            return await asyncio.to_thread(self._get_events_sync, session_id, offset, limit)

    async def get_state(
        self,
        agent_id: str,
        user_id: Optional[str],
        session_id: Optional[str],
        scope: str = "session",
    ) -> Optional[SessionState]:
        async with self._lock:
            return await asyncio.to_thread(
                self._get_state_sync,
                agent_id,
                user_id,
                session_id,
                scope,
            )

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
            return await asyncio.to_thread(
                self._update_state_sync,
                agent_id,
                user_id,
                session_id,
                scope,
                state_delta,
            )

    async def aclose(self) -> None:
        return None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path))
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    title_source TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    first_prompt TEXT NOT NULL DEFAULT '',
                    last_prompt TEXT NOT NULL DEFAULT '',
                    state_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    author TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    content_json TEXT NOT NULL DEFAULT '{}',
                    timestamp REAL NOT NULL,
                    state_delta_json TEXT NOT NULL DEFAULT '{}',
                    seq_id INTEGER NOT NULL,
                    invocation_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_events_session_seq
                ON events (session_id, seq_id);

                CREATE TABLE IF NOT EXISTS states (
                    scope TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    user_id TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    state_json TEXT NOT NULL DEFAULT '{}',
                    version INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (scope, agent_id, user_id, session_id)
                );
                """
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
            }
            required_columns = {
                "title": "TEXT NOT NULL DEFAULT ''",
                "title_source": "TEXT NOT NULL DEFAULT ''",
                "summary": "TEXT NOT NULL DEFAULT ''",
                "first_prompt": "TEXT NOT NULL DEFAULT ''",
                "last_prompt": "TEXT NOT NULL DEFAULT ''",
            }
            for column_name, ddl in required_columns.items():
                if column_name not in columns:
                    connection.execute(f"ALTER TABLE sessions ADD COLUMN {column_name} {ddl}")
            connection.commit()

    def _create_session_sync(
        self,
        agent_id: str,
        user_id: str,
        session_id: Optional[str],
    ) -> Session:
        with self._connect() as connection:
            if session_id:
                existing = self._get_session_sync(session_id, connection=connection)
                if existing:
                    return existing

            now = time.time()
            session = Session(
                id=session_id or generate_id(),
                agent_id=agent_id,
                user_id=user_id,
                created_at=now,
                updated_at=now,
            )
            connection.execute(
                """
                INSERT INTO sessions (
                    id, agent_id, user_id, title, title_source, summary, first_prompt, last_prompt,
                    state_json, created_at, updated_at, version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    agent_id,
                    user_id,
                    session.title,
                    session.title_source,
                    session.summary,
                    session.first_prompt,
                    session.last_prompt,
                    "{}",
                    session.created_at,
                    session.updated_at,
                    session.version,
                ),
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO states (scope, agent_id, user_id, session_id, state_json, version, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("session", agent_id, user_id, session.id, "{}", 0, now),
            )
            connection.commit()
            return session

    def _get_session_sync(self, session_id: str, *, connection: Optional[sqlite3.Connection] = None) -> Optional[Session]:
        owns_connection = connection is None
        connection = connection or self._connect()
        try:
            row = connection.execute(
                """
                SELECT
                    id, agent_id, user_id, title, title_source, summary, first_prompt, last_prompt,
                    state_json, created_at, updated_at, version
                FROM sessions
                WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            return Session(
                id=row["id"],
                agent_id=row["agent_id"],
                user_id=row["user_id"],
                title=row["title"],
                title_source=row["title_source"],
                summary=row["summary"],
                first_prompt=row["first_prompt"],
                last_prompt=row["last_prompt"],
                state=json.loads(row["state_json"] or "{}"),
                events=self._get_events_sync(session_id, connection=connection),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                version=row["version"],
            )
        finally:
            if owns_connection:
                connection.close()

    def _list_sessions_sync(
        self,
        agent_id: str,
        user_id: Optional[str],
    ) -> list[Session]:
        with self._connect() as connection:
            query = """
                SELECT
                    id, agent_id, user_id, title, title_source, summary, first_prompt, last_prompt,
                    state_json, created_at, updated_at, version
                FROM sessions
                WHERE agent_id = ?
            """
            params: list[object] = [agent_id]
            if user_id is not None:
                query += " AND user_id = ?"
                params.append(user_id)
            query += " ORDER BY updated_at DESC, created_at DESC"
            rows = connection.execute(query, params).fetchall()
            return [
                Session(
                    id=row["id"],
                    agent_id=row["agent_id"],
                    user_id=row["user_id"],
                    title=row["title"],
                    title_source=row["title_source"],
                    summary=row["summary"],
                    first_prompt=row["first_prompt"],
                    last_prompt=row["last_prompt"],
                    state=json.loads(row["state_json"] or "{}"),
                    events=[],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    version=row["version"],
                )
                for row in rows
            ]

    def _delete_session_sync(self, session_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT agent_id, user_id FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return False
            connection.execute("DELETE FROM events WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            connection.execute(
                """
                DELETE FROM states
                WHERE scope = 'session' AND agent_id = ? AND user_id = ? AND session_id = ?
                """,
                (row["agent_id"], row["user_id"], session_id),
            )
            connection.commit()
            return True

    def _append_event_sync(self, session_id: str, event: SessionEvent) -> SessionEvent:
        with self._connect() as connection:
            session_row = connection.execute(
                """
                SELECT agent_id, user_id, state_json, version
                FROM sessions
                WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
            if session_row is None:
                raise ValueError(f"Session {session_id} not found")

            next_seq = int(
                connection.execute(
                    "SELECT COALESCE(MAX(seq_id), 0) + 1 FROM events WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
            )
            stored = SessionEvent(
                id=event.id or generate_id(),
                session_id=session_id,
                author=event.author,
                event_type=event.event_type,
                content=event.content,
                timestamp=event.timestamp,
                state_delta=event.state_delta,
                seq_id=next_seq,
                invocation_id=event.invocation_id,
                metadata=event.metadata,
            )
            connection.execute(
                """
                INSERT INTO events (
                    id, session_id, author, event_type, content_json, timestamp,
                    state_delta_json, seq_id, invocation_id, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stored.id,
                    session_id,
                    stored.author,
                    stored.event_type,
                    json.dumps(stored.content),
                    stored.timestamp,
                    json.dumps(stored.state_delta),
                    stored.seq_id,
                    stored.invocation_id,
                    json.dumps(stored.metadata),
                ),
            )

            updated_at = time.time()
            state = json.loads(session_row["state_json"] or "{}")
            version = int(session_row["version"] or 0)
            if stored.state_delta:
                state.update(stored.state_delta)
                version += 1
            connection.execute(
                """
                UPDATE sessions
                SET state_json = ?, updated_at = ?, version = ?
                WHERE id = ?
                """,
                (json.dumps(state), updated_at, version, session_id),
            )
            if stored.state_delta:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO states (
                        scope, agent_id, user_id, session_id, state_json, version, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "session",
                        session_row["agent_id"],
                        session_row["user_id"],
                        session_id,
                        json.dumps(state),
                        version,
                        updated_at,
                    ),
                )
            connection.commit()
            return stored

    def _update_session_metadata_sync(
        self,
        session_id: str,
        title: Optional[str],
        title_source: Optional[str],
        summary: Optional[str],
        first_prompt: Optional[str],
        last_prompt: Optional[str],
    ) -> Session:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    id, agent_id, user_id, title, title_source, summary, first_prompt, last_prompt,
                    state_json, created_at, updated_at, version
                FROM sessions
                WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Session {session_id} not found")

            updated_at = time.time()
            next_title = row["title"] if title is None else title
            next_title_source = row["title_source"] if title_source is None else title_source
            next_summary = row["summary"] if summary is None else summary
            next_first_prompt = row["first_prompt"] if first_prompt is None else first_prompt
            next_last_prompt = row["last_prompt"] if last_prompt is None else last_prompt
            connection.execute(
                """
                UPDATE sessions
                SET title = ?, title_source = ?, summary = ?, first_prompt = ?, last_prompt = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    next_title,
                    next_title_source,
                    next_summary,
                    next_first_prompt,
                    next_last_prompt,
                    updated_at,
                    session_id,
                ),
            )
            connection.commit()
            return Session(
                id=row["id"],
                agent_id=row["agent_id"],
                user_id=row["user_id"],
                title=next_title,
                title_source=next_title_source,
                summary=next_summary,
                first_prompt=next_first_prompt,
                last_prompt=next_last_prompt,
                state=json.loads(row["state_json"] or "{}"),
                events=[],
                created_at=row["created_at"],
                updated_at=updated_at,
                version=row["version"],
            )

    def _get_events_sync(
        self,
        session_id: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        *,
        connection: Optional[sqlite3.Connection] = None,
    ) -> list[SessionEvent]:
        owns_connection = connection is None
        connection = connection or self._connect()
        try:
            query = """
                SELECT id, session_id, author, event_type, content_json, timestamp,
                       state_delta_json, seq_id, invocation_id, metadata_json
                FROM events
                WHERE session_id = ?
                ORDER BY seq_id ASC
            """
            params: list[object] = [session_id]
            if limit is not None:
                query += " LIMIT ?"
                params.append(limit)
                if offset is not None:
                    query += " OFFSET ?"
                    params.append(offset)
            elif offset is not None:
                query += " LIMIT -1 OFFSET ?"
                params.append(offset)

            rows = connection.execute(query, params).fetchall()
            return [
                SessionEvent(
                    id=row["id"],
                    session_id=row["session_id"],
                    author=row["author"],
                    event_type=row["event_type"],
                    content=json.loads(row["content_json"] or "{}"),
                    timestamp=row["timestamp"],
                    state_delta=json.loads(row["state_delta_json"] or "{}"),
                    seq_id=row["seq_id"],
                    invocation_id=row["invocation_id"],
                    metadata=json.loads(row["metadata_json"] or "{}"),
                )
                for row in rows
            ]
        finally:
            if owns_connection:
                connection.close()

    def _get_state_sync(
        self,
        agent_id: str,
        user_id: Optional[str],
        session_id: Optional[str],
        scope: str,
    ) -> Optional[SessionState]:
        with self._connect() as connection:
            if scope == "session" and session_id:
                session = self._get_session_sync(session_id, connection=connection)
                if session is None:
                    return None
                return SessionState(
                    scope="session",
                    agent_id=session.agent_id,
                    user_id=session.user_id,
                    session_id=session.id,
                    state=session.state,
                    version=session.version,
                    updated_at=session.updated_at,
                )

            row = connection.execute(
                """
                SELECT scope, agent_id, user_id, session_id, state_json, version, updated_at
                FROM states
                WHERE scope = ? AND agent_id = ? AND user_id = ? AND session_id = ?
                """,
                (scope, agent_id, user_id or "", session_id or ""),
            ).fetchone()
            if row is None:
                return None
            return SessionState(
                scope=row["scope"],
                agent_id=row["agent_id"],
                user_id=row["user_id"],
                session_id=row["session_id"],
                state=json.loads(row["state_json"] or "{}"),
                version=row["version"],
                updated_at=row["updated_at"],
            )

    def _update_state_sync(
        self,
        agent_id: str,
        user_id: Optional[str],
        session_id: Optional[str],
        scope: str,
        state_delta: dict,
    ) -> SessionState:
        with self._connect() as connection:
            updated_at = time.time()
            if scope == "session":
                if not session_id:
                    raise ValueError("session_id is required for session scope")
                session = self._get_session_sync(session_id, connection=connection)
                if session is None:
                    raise ValueError(f"Session {session_id} not found")
                next_state = dict(session.state)
                next_state.update(state_delta)
                next_version = session.version + 1
                connection.execute(
                    """
                    UPDATE sessions
                    SET state_json = ?, updated_at = ?, version = ?
                    WHERE id = ?
                    """,
                    (json.dumps(next_state), updated_at, next_version, session_id),
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO states (
                        scope, agent_id, user_id, session_id, state_json, version, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("session", session.agent_id, session.user_id, session.id, json.dumps(next_state), next_version, updated_at),
                )
                connection.commit()
                return SessionState(
                    scope="session",
                    agent_id=session.agent_id,
                    user_id=session.user_id,
                    session_id=session.id,
                    state=next_state,
                    version=next_version,
                    updated_at=updated_at,
                )

            row = connection.execute(
                """
                SELECT state_json, version
                FROM states
                WHERE scope = ? AND agent_id = ? AND user_id = ? AND session_id = ?
                """,
                (scope, agent_id, user_id or "", session_id or ""),
            ).fetchone()
            next_state = json.loads(row["state_json"] or "{}") if row else {}
            next_state.update(state_delta)
            next_version = int(row["version"] or 0) + 1 if row else 1
            connection.execute(
                """
                INSERT OR REPLACE INTO states (
                    scope, agent_id, user_id, session_id, state_json, version, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (scope, agent_id, user_id or "", session_id or "", json.dumps(next_state), next_version, updated_at),
            )
            connection.commit()
            return SessionState(
                scope=scope,
                agent_id=agent_id,
                user_id=user_id or "",
                session_id=session_id or "",
                state=next_state,
                version=next_version,
                updated_at=updated_at,
            )


def create_local_session_service(*, project_dir: Optional[str] = None) -> BaseSessionService:
    return LocalSessionService(project_dir=project_dir)


__all__ = [
    "LocalSessionService",
    "create_local_session_service",
    "resolve_local_session_dir",
]
