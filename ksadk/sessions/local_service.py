"""Local persistent session backend for embedded KSADK runtimes."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from collections.abc import Iterator
from typing import Optional

from ksadk.sessions.base import (
    BaseSessionService,
    CheckpointEventQuery,
    Session,
    SessionEvent,
    SessionEventQuery,
    SessionState,
    generate_id,
)

KSADK_SESSIONS_TABLE = "ksadk_sessions"
KSADK_EVENTS_TABLE = "ksadk_events"
KSADK_STATES_TABLE = "ksadk_states"

LEGACY_SESSIONS_TABLE = "sessions"
LEGACY_EVENTS_TABLE = "events"
LEGACY_STATES_TABLE = "states"

DEFAULT_SESSION_DB_NAME = "sessions.sqlite"


def resolve_local_session_dir(project_dir: Optional[str] = None) -> Path:
    configured = (os.getenv("AGENTENGINE_UI_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()

    root = Path(project_dir or os.getenv("KSADK_PROJECT_DIR") or os.getcwd()).resolve()
    return root / ".agentengine" / "ui"


def resolve_local_session_path(project_dir: Optional[str] = None) -> Path:
    configured_ui_dir = (os.getenv("AGENTENGINE_UI_DIR") or "").strip()
    if configured_ui_dir:
        return Path(configured_ui_dir).expanduser().resolve() / DEFAULT_SESSION_DB_NAME

    explicit_path = (os.getenv("KSADK_STM_PATH") or os.getenv("KSADK_STM_DB_PATH") or "").strip()
    if explicit_path:
        return Path(explicit_path).expanduser().resolve()

    return resolve_local_session_dir(project_dir) / DEFAULT_SESSION_DB_NAME


class LocalSessionService(BaseSessionService):
    def __init__(self, db_path: Optional[Path] = None, *, project_dir: Optional[str] = None):
        self.db_path = (
            Path(db_path).expanduser().resolve()
            if db_path is not None
            else resolve_local_session_path(project_dir)
        )
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
        agent_id: Optional[str],
        user_id: Optional[str] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[Session]:
        async with self._lock:
            return await asyncio.to_thread(
                self._list_sessions_sync,
                agent_id,
                user_id,
                offset,
                limit,
            )

    async def count_sessions(
        self,
        agent_id: Optional[str],
        user_id: Optional[str] = None,
    ) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._count_sessions_sync, agent_id, user_id)

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
        after_seq_id: Optional[int] = None,
        before_seq_id: Optional[int] = None,
    ) -> list[SessionEvent]:
        async with self._lock:
            return await asyncio.to_thread(
                self._get_events_sync,
                session_id,
                offset,
                limit,
                after_seq_id,
                before_seq_id,
            )

    async def count_events(
        self,
        session_id: str,
        after_seq_id: Optional[int] = None,
        before_seq_id: Optional[int] = None,
    ) -> int:
        async with self._lock:
            return await asyncio.to_thread(
                self._count_events_sync,
                session_id,
                after_seq_id,
                before_seq_id,
            )

    async def get_sessions_by_ids(self, session_ids: list[str]) -> list[Session]:
        async with self._lock:
            return await asyncio.to_thread(self._get_sessions_by_ids_sync, session_ids)

    async def get_session_metadata(self, session_id: str) -> Optional[Session]:
        sessions = await self.get_sessions_by_ids([session_id])
        return sessions[0] if sessions else None

    async def list_session_metadata(
        self, agent_id: Optional[str] = None, user_id: Optional[str] = None
    ) -> list[Session]:
        return await self.list_sessions(agent_id, user_id)

    async def query_events(self, query: SessionEventQuery) -> list[SessionEvent]:
        async with self._lock:
            return await asyncio.to_thread(self._query_events_sync, query, False)

    async def count_event_query(self, query: SessionEventQuery) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._query_events_sync, query, True)

    async def get_checkpoint_lookup_stats(
        self, session_id: str, run_id: str, checkpoint_id: str
    ) -> dict[str, object]:
        async with self._lock:
            return await asyncio.to_thread(self._checkpoint_lookup_stats_sync, session_id, run_id, checkpoint_id)

    async def scan_checkpoint_events(
        self, query: CheckpointEventQuery
    ) -> list[SessionEvent]:
        if query.limit < 1 or query.limit > 50:
            raise ValueError("checkpoint scan limit must be between 1 and 50")
        async with self._lock:
            return await asyncio.to_thread(self._scan_checkpoint_events_sync, query)

    async def get_checkpoint_stats(
        self, keys: list[tuple[str, str, str]]
    ) -> dict[str, object]:
        if len(keys) > 50:
            raise ValueError("checkpoint stats batch cannot exceed 50 keys")
        async with self._lock:
            return await asyncio.to_thread(self._get_checkpoint_stats_sync, keys)

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
        async with self._lock:
            return await asyncio.to_thread(
                self._get_events_batch_sync,
                session_ids,
                agent_id,
                offset,
                limit,
                after_seq_id,
                before_seq_id,
                event_types,
                from_start,
            )

    async def count_events_batch(
        self,
        session_ids: list[str] | None = None,
        *,
        agent_id: str | None = None,
        after_seq_id: int | None = None,
        before_seq_id: int | None = None,
        event_types: list[str] | None = None,
    ) -> int:
        async with self._lock:
            return await asyncio.to_thread(
                self._count_events_batch_sync,
                session_ids,
                agent_id,
                after_seq_id,
                before_seq_id,
                event_types,
            )

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
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as connection:
            with connection:
                yield connection

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        return row is not None

    @classmethod
    def _table_columns(cls, connection: sqlite3.Connection, table_name: str) -> set[str]:
        if not cls._table_exists(connection, table_name):
            return set()
        rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {str(row["name"]) for row in rows}

    @staticmethod
    def _ensure_columns(
        connection: sqlite3.Connection,
        table_name: str,
        required_columns: dict[str, str],
    ) -> None:
        existing_columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        for column_name, ddl in required_columns.items():
            if column_name not in existing_columns:
                connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}")

    def _migrate_legacy_schema(self, connection: sqlite3.Connection) -> None:
        legacy_session_columns = self._table_columns(connection, LEGACY_SESSIONS_TABLE)
        if (
            legacy_session_columns
            and not self._table_exists(connection, KSADK_SESSIONS_TABLE)
            and "agent_id" in legacy_session_columns
            and "app_name" not in legacy_session_columns
        ):
            connection.execute(
                f"ALTER TABLE {LEGACY_SESSIONS_TABLE} RENAME TO {KSADK_SESSIONS_TABLE}"
            )

        legacy_event_columns = self._table_columns(connection, LEGACY_EVENTS_TABLE)
        if (
            legacy_event_columns
            and not self._table_exists(connection, KSADK_EVENTS_TABLE)
            and {"session_id", "author", "event_type"}.issubset(legacy_event_columns)
        ):
            connection.execute(
                f"ALTER TABLE {LEGACY_EVENTS_TABLE} RENAME TO {KSADK_EVENTS_TABLE}"
            )

        legacy_state_columns = self._table_columns(connection, LEGACY_STATES_TABLE)
        if (
            legacy_state_columns
            and not self._table_exists(connection, KSADK_STATES_TABLE)
            and {"scope", "agent_id", "state_json"}.issubset(legacy_state_columns)
        ):
            connection.execute(
                f"ALTER TABLE {LEGACY_STATES_TABLE} RENAME TO {KSADK_STATES_TABLE}"
            )

    def _ensure_schema(self) -> None:
        with self._connection() as connection:
            self._migrate_legacy_schema(connection)
            connection.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS {KSADK_SESSIONS_TABLE} (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    title_source TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    first_prompt TEXT NOT NULL DEFAULT '',
                    last_prompt TEXT NOT NULL DEFAULT '',
                    state_json TEXT NOT NULL DEFAULT '{{}}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS {KSADK_EVENTS_TABLE} (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    author TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    content_json TEXT NOT NULL DEFAULT '{{}}',
                    timestamp REAL NOT NULL,
                    state_delta_json TEXT NOT NULL DEFAULT '{{}}',
                    seq_id INTEGER NOT NULL,
                    invocation_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{{}}',
                    FOREIGN KEY(session_id) REFERENCES {KSADK_SESSIONS_TABLE}(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_ksadk_events_session_seq
                ON {KSADK_EVENTS_TABLE} (session_id, seq_id);

                CREATE INDEX IF NOT EXISTS idx_ksadk_events_timestamp_session_seq
                ON {KSADK_EVENTS_TABLE} (timestamp, session_id, seq_id, id);

                CREATE INDEX IF NOT EXISTS idx_ksadk_events_checkpoint_lookup
                ON {KSADK_EVENTS_TABLE} (session_id, event_type,
                    json_extract(metadata_json, '$.run_id'),
                    json_extract(metadata_json, '$.checkpoint_id'), seq_id);

                CREATE TABLE IF NOT EXISTS {KSADK_STATES_TABLE} (
                    scope TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    user_id TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    state_json TEXT NOT NULL DEFAULT '{{}}',
                    version INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (scope, agent_id, user_id, session_id)
                );
                """
            )
            self._ensure_columns(
                connection,
                KSADK_SESSIONS_TABLE,
                {
                    "title": "TEXT NOT NULL DEFAULT ''",
                    "title_source": "TEXT NOT NULL DEFAULT ''",
                    "summary": "TEXT NOT NULL DEFAULT ''",
                    "first_prompt": "TEXT NOT NULL DEFAULT ''",
                    "last_prompt": "TEXT NOT NULL DEFAULT ''",
                    "state_json": "TEXT NOT NULL DEFAULT '{}'",
                    "version": "INTEGER NOT NULL DEFAULT 0",
                },
            )
            self._ensure_columns(
                connection,
                KSADK_EVENTS_TABLE,
                {
                    "state_delta_json": "TEXT NOT NULL DEFAULT '{}'",
                    "invocation_id": "TEXT",
                    "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
                },
            )
            self._ensure_columns(
                connection,
                KSADK_STATES_TABLE,
                {
                    "version": "INTEGER NOT NULL DEFAULT 0",
                    "updated_at": "REAL NOT NULL DEFAULT 0",
                },
            )
            connection.commit()

    def _create_session_sync(
        self,
        agent_id: str,
        user_id: str,
        session_id: Optional[str],
    ) -> Session:
        with self._connection() as connection:
            if session_id:
                existing = self._get_session_sync(session_id, connection=connection)
                if existing is not None:
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
                f"""
                INSERT INTO {KSADK_SESSIONS_TABLE} (
                    id, agent_id, user_id, title, title_source, summary, first_prompt, last_prompt,
                    state_json, created_at, updated_at, version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    session.agent_id,
                    session.user_id,
                    session.title,
                    session.title_source,
                    session.summary,
                    session.first_prompt,
                    session.last_prompt,
                    json.dumps(session.state),
                    session.created_at,
                    session.updated_at,
                    session.version,
                ),
            )
            connection.execute(
                f"""
                INSERT OR REPLACE INTO {KSADK_STATES_TABLE} (
                    scope, agent_id, user_id, session_id, state_json, version, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("session", session.agent_id, session.user_id, session.id, "{}", 0, now),
            )
            connection.commit()
            return session

    def _get_session_sync(
        self,
        session_id: str,
        *,
        connection: Optional[sqlite3.Connection] = None,
    ) -> Optional[Session]:
        owns_connection = connection is None
        connection = connection or self._connect()
        try:
            row = connection.execute(
                f"""
                SELECT
                    id, agent_id, user_id, title, title_source, summary, first_prompt, last_prompt,
                    state_json, created_at, updated_at, version
                FROM {KSADK_SESSIONS_TABLE}
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
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[Session]:
        with self._connection() as connection:
            query = f"""
                SELECT
                    id, agent_id, user_id, title, title_source, summary, first_prompt, last_prompt,
                    state_json, created_at, updated_at, version
                FROM {KSADK_SESSIONS_TABLE}
                WHERE 1
            """
            params: list[object] = []
            if agent_id is not None:
                query += " AND agent_id = ?"
                params.append(agent_id)
            if user_id is not None:
                query += " AND user_id = ?"
                params.append(user_id)
            query += " ORDER BY updated_at DESC, created_at DESC"
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

    def _count_sessions_sync(self, agent_id: str, user_id: Optional[str]) -> int:
        with self._connection() as connection:
            query = f"""
                SELECT COUNT(*) AS total
                FROM {KSADK_SESSIONS_TABLE}
                WHERE agent_id = ?
            """
            params: list[object] = [agent_id]
            if user_id is not None:
                query += " AND user_id = ?"
                params.append(user_id)
            row = connection.execute(query, params).fetchone()
            return int(row["total"] if row else 0)

    def _delete_session_sync(self, session_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT 1 FROM {KSADK_SESSIONS_TABLE} WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return False

            connection.execute(f"DELETE FROM {KSADK_EVENTS_TABLE} WHERE session_id = ?", (session_id,))
            connection.execute(f"DELETE FROM {KSADK_STATES_TABLE} WHERE session_id = ?", (session_id,))
            connection.execute(f"DELETE FROM {KSADK_SESSIONS_TABLE} WHERE id = ?", (session_id,))
            connection.commit()
            return True

    def _append_event_sync(self, session_id: str, event: SessionEvent) -> SessionEvent:
        with self._connection() as connection:
            session_row = connection.execute(
                f"""
                SELECT agent_id, user_id, state_json, version
                FROM {KSADK_SESSIONS_TABLE}
                WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
            if session_row is None:
                raise ValueError(f"Session {session_id} not found")

            next_seq = int(
                connection.execute(
                    f"SELECT COALESCE(MAX(seq_id), 0) + 1 FROM {KSADK_EVENTS_TABLE} WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
            )
            stored = SessionEvent(
                id=event.id or generate_id(),
                session_id=session_id,
                author=event.author,
                event_type=event.event_type,
                content=dict(event.content),
                timestamp=event.timestamp,
                state_delta=dict(event.state_delta),
                seq_id=next_seq,
                invocation_id=event.invocation_id,
                metadata=dict(event.metadata),
            )
            connection.execute(
                f"""
                INSERT INTO {KSADK_EVENTS_TABLE} (
                    id, session_id, author, event_type, content_json, timestamp,
                    state_delta_json, seq_id, invocation_id, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stored.id,
                    stored.session_id,
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
                f"""
                UPDATE {KSADK_SESSIONS_TABLE}
                SET state_json = ?, updated_at = ?, version = ?
                WHERE id = ?
                """,
                (json.dumps(state), updated_at, version, session_id),
            )

            if stored.state_delta:
                connection.execute(
                    f"""
                    INSERT OR REPLACE INTO {KSADK_STATES_TABLE} (
                        scope, agent_id, user_id, session_id, state_json, version, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
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
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT
                    id, agent_id, user_id, title, title_source, summary, first_prompt, last_prompt,
                    state_json, created_at, updated_at, version
                FROM {KSADK_SESSIONS_TABLE}
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
                f"""
                UPDATE {KSADK_SESSIONS_TABLE}
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
        after_seq_id: Optional[int] = None,
        before_seq_id: Optional[int] = None,
        *,
        connection: Optional[sqlite3.Connection] = None,
    ) -> list[SessionEvent]:
        owns_connection = connection is None
        connection = connection or self._connect()
        try:
            # seq 过滤先应用,再对结果集应用"最新 N 条" offset/limit 语义。
            seq_clauses: list[str] = []
            seq_params: list[object] = []
            if after_seq_id is not None:
                seq_clauses.append("AND seq_id > ?")
                seq_params.append(after_seq_id)
            if before_seq_id is not None:
                seq_clauses.append("AND seq_id < ?")
                seq_params.append(before_seq_id)
            seq_clause = " ".join(seq_clauses)
            if limit is not None:
                query = f"""
                    SELECT id, session_id, author, event_type, content_json, timestamp,
                           state_delta_json, seq_id, invocation_id, metadata_json
                    FROM (
                        SELECT id, session_id, author, event_type, content_json, timestamp,
                               state_delta_json, seq_id, invocation_id, metadata_json
                        FROM {KSADK_EVENTS_TABLE}
                        WHERE session_id = ? {seq_clause}
                        ORDER BY seq_id DESC
                        LIMIT ? OFFSET ?
                    )
                    ORDER BY seq_id ASC
                """
                params: list[object] = [session_id, *seq_params]
                params.extend([limit, offset or 0])
            elif offset is not None:
                query = f"""
                    SELECT id, session_id, author, event_type, content_json, timestamp,
                           state_delta_json, seq_id, invocation_id, metadata_json
                    FROM (
                        SELECT id, session_id, author, event_type, content_json, timestamp,
                               state_delta_json, seq_id, invocation_id, metadata_json
                        FROM {KSADK_EVENTS_TABLE}
                        WHERE session_id = ? {seq_clause}
                        ORDER BY seq_id DESC
                        LIMIT -1 OFFSET ?
                    )
                    ORDER BY seq_id ASC
                """
                params = [session_id, *seq_params]
                params.append(offset)
            else:
                query = f"""
                    SELECT id, session_id, author, event_type, content_json, timestamp,
                           state_delta_json, seq_id, invocation_id, metadata_json
                    FROM {KSADK_EVENTS_TABLE}
                    WHERE session_id = ? {seq_clause}
                    ORDER BY seq_id ASC
                """
                params = [session_id, *seq_params]

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

    def _count_events_sync(
        self,
        session_id: str,
        after_seq_id: Optional[int] = None,
        before_seq_id: Optional[int] = None,
    ) -> int:
        with self._connection() as connection:
            seq_clauses: list[str] = []
            params: list[object] = [session_id]
            if after_seq_id is not None:
                seq_clauses.append("AND seq_id > ?")
                params.append(after_seq_id)
            if before_seq_id is not None:
                seq_clauses.append("AND seq_id < ?")
                params.append(before_seq_id)
            seq_clause = " ".join(seq_clauses)
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM {KSADK_EVENTS_TABLE}
                WHERE session_id = ? {seq_clause}
                """,
                params,
            ).fetchone()
            return int(row["total"] if row else 0)

    def _get_sessions_by_ids_sync(self, session_ids: list[str]) -> list[Session]:
        if not session_ids:
            return []
        with self._connection() as connection:
            placeholders = ", ".join("?" for _ in session_ids)
            rows = connection.execute(
                f"""
                SELECT id, agent_id, user_id, title, title_source, summary, first_prompt, last_prompt,
                       state_json, created_at, updated_at, version
                FROM {KSADK_SESSIONS_TABLE} WHERE id IN ({placeholders})
                """,
                session_ids,
            ).fetchall()
            by_id = {
                row["id"]: Session(
                    id=row["id"], agent_id=row["agent_id"], user_id=row["user_id"],
                    title=row["title"], title_source=row["title_source"], summary=row["summary"],
                    first_prompt=row["first_prompt"], last_prompt=row["last_prompt"],
                    state=json.loads(row["state_json"] or "{}"), events=[],
                    created_at=row["created_at"], updated_at=row["updated_at"], version=row["version"],
                ) for row in rows
            }
            return [by_id[session_id] for session_id in session_ids if session_id in by_id]

    def _batch_event_where(
        self,
        session_ids: list[str] | None,
        agent_id: str | None,
        after_seq_id: int | None,
        before_seq_id: int | None,
        event_types: list[str] | None,
        run_id: str | None = None,
        checkpoint_id: str | None = None,
        checkpoint_ids: list[str] | None = None,
        framework: str | None = None,
    ) -> tuple[str, list[object]]:
        clauses: list[str] = []
        params: list[object] = []
        if session_ids is not None:
            if not session_ids:
                return "0", []
            clauses.append("event_row.session_id IN (SELECT value FROM json_each(?))")
            params.append(json.dumps(session_ids))
        if agent_id is not None:
            clauses.append("session_row.agent_id = ?")
            params.append(agent_id)
        if after_seq_id is not None:
            clauses.append("event_row.seq_id > ?")
            params.append(after_seq_id)
        if before_seq_id is not None:
            clauses.append("event_row.seq_id < ?")
            params.append(before_seq_id)
        if event_types:
            clauses.append("event_row.event_type IN (" + ", ".join("?" for _ in event_types) + ")")
            params.extend(event_types)
        if run_id is not None:
            clauses.append("json_extract(event_row.metadata_json, '$.run_id') = ?")
            params.append(run_id)
        if checkpoint_id is not None:
            clauses.append("json_extract(event_row.metadata_json, '$.checkpoint_id') = ?")
            params.append(checkpoint_id)
        if checkpoint_ids is not None:
            if not checkpoint_ids:
                return "0", []
            clauses.append(
                "json_extract(event_row.metadata_json, '$.checkpoint_id') "
                "IN (SELECT value FROM json_each(?))"
            )
            params.append(json.dumps(checkpoint_ids))
        if framework is not None:
            clauses.append("lower(json_extract(event_row.metadata_json, '$.framework')) = ?")
            params.append(framework.lower())
        return " AND ".join(clauses) or "1", params

    def _scan_checkpoint_events_sync(self, query: CheckpointEventQuery) -> list[SessionEvent]:
        where, params = self._batch_event_where(
            query.session_ids,
            query.agent_id,
            None,
            None,
            ["run_checkpoint"],
            query.run_id,
            None,
            query.checkpoint_ids,
            query.framework,
        )
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT event_row.id, event_row.session_id, event_row.author,
                       event_row.event_type, event_row.content_json, event_row.timestamp,
                       event_row.state_delta_json, event_row.seq_id,
                       event_row.invocation_id, event_row.metadata_json
                FROM {KSADK_EVENTS_TABLE} AS event_row
                JOIN {KSADK_SESSIONS_TABLE} AS session_row ON session_row.id = event_row.session_id
                WHERE {where}
                ORDER BY event_row.timestamp ASC, event_row.session_id ASC,
                         event_row.seq_id ASC, event_row.id ASC
                LIMIT ? OFFSET ?
                """,
                [*params, query.limit, query.offset],
            ).fetchall()
            return [
                SessionEvent(
                    id=row["id"], session_id=row["session_id"], author=row["author"],
                    event_type=row["event_type"], content=json.loads(row["content_json"] or "{}"),
                    timestamp=row["timestamp"], state_delta=json.loads(row["state_delta_json"] or "{}"),
                    seq_id=row["seq_id"], invocation_id=row["invocation_id"],
                    metadata=json.loads(row["metadata_json"] or "{}"),
                ) for row in rows
            ]

    def _get_checkpoint_stats_sync(
        self, keys: list[tuple[str, str, str]]
    ) -> dict[str, object]:
        unique_keys = list(dict.fromkeys(keys))
        audits = {
            key: {"resume_count": 0, "last_resumed_at": None}
            for key in unique_keys
        }
        run_keys = list(dict.fromkeys((session_id, run_id) for session_id, run_id, _ in unique_keys))
        latest_seq_ids = {key: 0 for key in run_keys}
        if not unique_keys:
            return {"audits": audits, "latest_seq_ids": latest_seq_ids}
        audit_terms = " OR ".join(
            "(session_id = ? AND json_extract(metadata_json, '$.run_id') = ? "
            "AND json_extract(metadata_json, '$.checkpoint_id') = ?)"
            for _ in unique_keys
        )
        audit_params = [value for key in unique_keys for value in key]
        run_terms = " OR ".join(
            "(session_id = ? AND json_extract(metadata_json, '$.run_id') = ?)"
            for _ in run_keys
        )
        run_params = [value for key in run_keys for value in key]
        with self._connection() as connection:
            audit_rows = connection.execute(
                f"""SELECT session_id,
                    json_extract(metadata_json, '$.run_id') AS run_id,
                    json_extract(metadata_json, '$.checkpoint_id') AS checkpoint_id,
                    COUNT(*) AS resume_count, MAX(timestamp) AS last_resumed_at
                FROM {KSADK_EVENTS_TABLE}
                WHERE event_type = 'run_resume' AND ({audit_terms})
                GROUP BY session_id, run_id, checkpoint_id""",
                audit_params,
            ).fetchall()
            latest_rows = connection.execute(
                f"""SELECT session_id,
                    json_extract(metadata_json, '$.run_id') AS run_id,
                    MAX(seq_id) AS latest_seq_id
                FROM {KSADK_EVENTS_TABLE}
                WHERE event_type = 'run_checkpoint' AND ({run_terms})
                GROUP BY session_id, run_id""",
                run_params,
            ).fetchall()
        for row in audit_rows:
            audits[(row["session_id"], row["run_id"], row["checkpoint_id"])] = {
                "resume_count": int(row["resume_count"] or 0),
                "last_resumed_at": row["last_resumed_at"],
            }
        for row in latest_rows:
            latest_seq_ids[(row["session_id"], row["run_id"])] = int(row["latest_seq_id"] or 0)
        return {"audits": audits, "latest_seq_ids": latest_seq_ids}

    def _query_events_sync(self, query: SessionEventQuery, count_only: bool) -> list[SessionEvent] | int:
        where, params = self._batch_event_where(
            query.session_ids, query.agent_id, query.after_seq_id, query.before_seq_id,
            query.event_types, query.run_id, query.checkpoint_id,
        )
        with self._connection() as connection:
            if count_only:
                row = connection.execute(
                    f"SELECT COUNT(*) AS total FROM {KSADK_EVENTS_TABLE} AS event_row JOIN {KSADK_SESSIONS_TABLE} AS session_row ON session_row.id = event_row.session_id WHERE {where}", params
                ).fetchone()
                return int(row["total"] if row else 0)
            direction = "ASC" if query.from_start else "DESC"
            rows = connection.execute(
                f"""SELECT id, session_id, author, event_type, content_json, timestamp, state_delta_json, seq_id, invocation_id, metadata_json
                FROM (SELECT event_row.id, event_row.session_id, event_row.author, event_row.event_type, event_row.content_json, event_row.timestamp, event_row.state_delta_json, event_row.seq_id, event_row.invocation_id, event_row.metadata_json
                      FROM {KSADK_EVENTS_TABLE} AS event_row JOIN {KSADK_SESSIONS_TABLE} AS session_row ON session_row.id = event_row.session_id WHERE {where}
                      ORDER BY event_row.timestamp {direction}, event_row.session_id {direction}, event_row.seq_id {direction}, event_row.id {direction} LIMIT ? OFFSET ?)
                ORDER BY timestamp ASC, session_id ASC, seq_id ASC, id ASC""", [*params, query.limit, query.offset]
            ).fetchall()
            return [SessionEvent(id=row["id"], session_id=row["session_id"], author=row["author"], event_type=row["event_type"], content=json.loads(row["content_json"] or "{}"), timestamp=row["timestamp"], state_delta=json.loads(row["state_delta_json"] or "{}"), seq_id=row["seq_id"], invocation_id=row["invocation_id"], metadata=json.loads(row["metadata_json"] or "{}")) for row in rows]

    def _checkpoint_lookup_stats_sync(self, session_id: str, run_id: str, checkpoint_id: str) -> dict[str, object]:
        with self._connection() as connection:
            candidate_row = connection.execute(
                f"""SELECT id, session_id, author, event_type, content_json, timestamp, state_delta_json, seq_id, invocation_id, metadata_json
                FROM {KSADK_EVENTS_TABLE} WHERE session_id = ? AND event_type = 'run_checkpoint'
                AND json_extract(metadata_json, '$.run_id') = ? AND json_extract(metadata_json, '$.checkpoint_id') = ?
                ORDER BY seq_id DESC LIMIT 1""", (session_id, run_id, checkpoint_id)
            ).fetchone()
            max_seq_id = connection.execute(
                f"SELECT COALESCE(MAX(seq_id), 0) FROM {KSADK_EVENTS_TABLE} WHERE session_id = ? AND event_type = 'run_checkpoint' AND json_extract(metadata_json, '$.run_id') = ?", (session_id, run_id)
            ).fetchone()[0]
            audit = connection.execute(
                f"SELECT COUNT(*) AS count, MAX(timestamp) AS last_at FROM {KSADK_EVENTS_TABLE} WHERE session_id = ? AND event_type = 'run_resume' AND json_extract(metadata_json, '$.run_id') = ? AND json_extract(metadata_json, '$.checkpoint_id') = ?", (session_id, run_id, checkpoint_id)
            ).fetchone()
            candidate = None if candidate_row is None else SessionEvent(id=candidate_row["id"], session_id=candidate_row["session_id"], author=candidate_row["author"], event_type=candidate_row["event_type"], content=json.loads(candidate_row["content_json"] or "{}"), timestamp=candidate_row["timestamp"], state_delta=json.loads(candidate_row["state_delta_json"] or "{}"), seq_id=candidate_row["seq_id"], invocation_id=candidate_row["invocation_id"], metadata=json.loads(candidate_row["metadata_json"] or "{}"))
            return {"candidate": candidate, "max_seq_id": int(max_seq_id or 0), "resume_count": int(audit["count"] or 0), "last_resumed_at": audit["last_at"]}

    def _get_events_batch_sync(
        self, session_ids: list[str] | None, agent_id: str | None, offset: int, limit: int,
        after_seq_id: int | None, before_seq_id: int | None, event_types: list[str] | None,
        from_start: bool,
    ) -> list[SessionEvent]:
        where, params = self._batch_event_where(
            session_ids, agent_id, after_seq_id, before_seq_id, event_types
        )
        direction = "ASC" if from_start else "DESC"
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT id, session_id, author, event_type, content_json, timestamp,
                       state_delta_json, seq_id, invocation_id, metadata_json
                FROM (
                    SELECT event_row.id, event_row.session_id, event_row.author, event_row.event_type,
                           event_row.content_json, event_row.timestamp, event_row.state_delta_json,
                           event_row.seq_id, event_row.invocation_id, event_row.metadata_json
                    FROM {KSADK_EVENTS_TABLE} AS event_row
                    JOIN {KSADK_SESSIONS_TABLE} AS session_row ON session_row.id = event_row.session_id
                    WHERE {where}
                    ORDER BY event_row.timestamp {direction}, event_row.session_id {direction},
                             event_row.seq_id {direction}, event_row.id {direction}
                    LIMIT ? OFFSET ?
                )
                ORDER BY timestamp ASC, session_id ASC, seq_id ASC, id ASC
                """,
                [*params, limit, offset],
            ).fetchall()
            return [
                SessionEvent(
                    id=row["id"], session_id=row["session_id"], author=row["author"],
                    event_type=row["event_type"], content=json.loads(row["content_json"] or "{}"),
                    timestamp=row["timestamp"], state_delta=json.loads(row["state_delta_json"] or "{}"),
                    seq_id=row["seq_id"], invocation_id=row["invocation_id"],
                    metadata=json.loads(row["metadata_json"] or "{}"),
                ) for row in rows
            ]

    def _count_events_batch_sync(
        self, session_ids: list[str] | None, agent_id: str | None, after_seq_id: int | None,
        before_seq_id: int | None, event_types: list[str] | None,
    ) -> int:
        where, params = self._batch_event_where(
            session_ids, agent_id, after_seq_id, before_seq_id, event_types
        )
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS total FROM {KSADK_EVENTS_TABLE} AS event_row
                JOIN {KSADK_SESSIONS_TABLE} AS session_row ON session_row.id = event_row.session_id
                WHERE {where}
                """, params
            ).fetchone()
            return int(row["total"] if row else 0)

    def _get_state_sync(
        self,
        agent_id: str,
        user_id: Optional[str],
        session_id: Optional[str],
        scope: str,
    ) -> Optional[SessionState]:
        with self._connection() as connection:
            if scope == "session" and session_id:
                session = self._get_session_sync(session_id, connection=connection)
                if session is None:
                    return None
                return SessionState(
                    scope="session",
                    agent_id=session.agent_id,
                    user_id=session.user_id,
                    session_id=session.id,
                    state=dict(session.state),
                    version=session.version,
                    updated_at=session.updated_at,
                )

            row = connection.execute(
                f"""
                SELECT scope, agent_id, user_id, session_id, state_json, version, updated_at
                FROM {KSADK_STATES_TABLE}
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
        with self._connection() as connection:
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
                    f"""
                    UPDATE {KSADK_SESSIONS_TABLE}
                    SET state_json = ?, updated_at = ?, version = ?
                    WHERE id = ?
                    """,
                    (json.dumps(next_state), updated_at, next_version, session_id),
                )
                connection.execute(
                    f"""
                    INSERT OR REPLACE INTO {KSADK_STATES_TABLE} (
                        scope, agent_id, user_id, session_id, state_json, version, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "session",
                        session.agent_id,
                        session.user_id,
                        session.id,
                        json.dumps(next_state),
                        next_version,
                        updated_at,
                    ),
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
                f"""
                SELECT state_json, version
                FROM {KSADK_STATES_TABLE}
                WHERE scope = ? AND agent_id = ? AND user_id = ? AND session_id = ?
                """,
                (scope, agent_id, user_id or "", session_id or ""),
            ).fetchone()
            next_state = json.loads(row["state_json"] or "{}") if row else {}
            next_state.update(state_delta)
            next_version = (int(row["version"] or 0) + 1) if row else 1

            connection.execute(
                f"""
                INSERT OR REPLACE INTO {KSADK_STATES_TABLE} (
                    scope, agent_id, user_id, session_id, state_json, version, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scope,
                    agent_id,
                    user_id or "",
                    session_id or "",
                    json.dumps(next_state),
                    next_version,
                    updated_at,
                ),
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
    "DEFAULT_SESSION_DB_NAME",
    "KSADK_EVENTS_TABLE",
    "KSADK_SESSIONS_TABLE",
    "KSADK_STATES_TABLE",
    "LocalSessionService",
    "create_local_session_service",
    "resolve_local_session_dir",
    "resolve_local_session_path",
]
