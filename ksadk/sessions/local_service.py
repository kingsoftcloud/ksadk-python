"""Local persistent session backend for embedded KSADK runtimes."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Optional

from ksadk.sessions._local_service_sync import _LocalServiceSyncMixin
from ksadk.sessions._local_tables import (
    DEFAULT_SESSION_DB_NAME,
    KSADK_EVENTS_TABLE,
    KSADK_SESSIONS_TABLE,
    KSADK_STATES_TABLE,
    LEGACY_EVENTS_TABLE,
    LEGACY_SESSIONS_TABLE,
    LEGACY_STATES_TABLE,
)
from ksadk.sessions.base import (
    CANONICAL_EVENT_STORAGE_CAPABILITIES,
    BaseSessionService,
    Session,
    SessionEvent,
    SessionState,
)


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


class LocalSessionService(_LocalServiceSyncMixin, BaseSessionService):
    storage_capabilities = CANONICAL_EVENT_STORAGE_CAPABILITIES

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

    async def get_session_metadata(self, session_id: str) -> Optional[Session]:
        async with self._lock:
            return await asyncio.to_thread(
                self._get_session_sync,
                session_id,
                include_events=False,
            )

    async def list_sessions(
        self,
        agent_id: str,
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
        agent_id: str,
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

    async def get_event_by_id(self, session_id: str, event_id: str) -> Optional[SessionEvent]:
        async with self._lock:
            return await asyncio.to_thread(self._get_event_by_id_sync, session_id, event_id)

    async def get_events_by_invocation_id(
        self,
        session_id: str,
        invocation_id: str,
        *,
        after_seq_id: Optional[int] = None,
        before_seq_id: Optional[int] = None,
    ) -> list[SessionEvent]:
        async with self._lock:
            return await asyncio.to_thread(
                self._get_events_by_invocation_id_sync,
                session_id,
                invocation_id,
                after_seq_id,
                before_seq_id,
            )

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

    async def get_events_for_agent(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[SessionEvent]:
        async with self._lock:
            return await asyncio.to_thread(
                self._get_events_for_agent_sync,
                agent_id,
                user_id,
                offset,
                limit,
            )

    async def count_events_for_agent(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
    ) -> int:
        async with self._lock:
            return await asyncio.to_thread(
                self._count_events_for_agent_sync,
                agent_id,
                user_id,
            )

    def _get_events_for_agent_sync(
        self,
        agent_id: str,
        user_id: Optional[str],
        offset: Optional[int],
        limit: Optional[int],
    ) -> list[SessionEvent]:
        columns = (
            "e.id, e.session_id, e.author, e.event_type, e.content_json, e.timestamp, "
            "e.state_delta_json, e.seq_id, e.invocation_id, e.metadata_json"
        )
        user_clause = "AND s.user_id = ?" if user_id is not None else ""
        from_clause = (
            f"FROM {KSADK_EVENTS_TABLE} e "
            f"JOIN {KSADK_SESSIONS_TABLE} s ON s.id = e.session_id "
            f"WHERE s.agent_id = ? {user_clause}"
        )
        base_params: list[object] = [agent_id]
        if user_id is not None:
            base_params.append(user_id)
        with self._connection() as connection:
            if limit is not None or offset is not None:
                # 与 get_events 一致的"最新 N 条"尾部语义：先 DESC 取窗口再 ASC 重排
                query = f"""
                    SELECT id, session_id, author, event_type, content_json, timestamp,
                           state_delta_json, seq_id, invocation_id, metadata_json
                    FROM (
                        SELECT {columns}
                        {from_clause}
                        ORDER BY e.timestamp DESC, e.seq_id DESC, e.id DESC
                        LIMIT ? OFFSET ?
                    )
                    ORDER BY timestamp ASC, seq_id ASC, id ASC
                """
                params = [*base_params, limit if limit is not None else -1, offset or 0]
            else:
                query = f"""
                    SELECT {columns}
                    {from_clause}
                    ORDER BY e.timestamp ASC, e.seq_id ASC, e.id ASC
                """
                params = base_params
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

    def _count_events_for_agent_sync(
        self,
        agent_id: str,
        user_id: Optional[str],
    ) -> int:
        user_clause = "AND s.user_id = ?" if user_id is not None else ""
        params: list[object] = [agent_id]
        if user_id is not None:
            params.append(user_id)
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM {KSADK_EVENTS_TABLE} e
                JOIN {KSADK_SESSIONS_TABLE} s ON s.id = e.session_id
                WHERE s.agent_id = ? {user_clause}
                """,
                params,
            ).fetchone()
            return int(row["total"] if row else 0)

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
            connection.execute(f"ALTER TABLE {LEGACY_EVENTS_TABLE} RENAME TO {KSADK_EVENTS_TABLE}")

        legacy_state_columns = self._table_columns(connection, LEGACY_STATES_TABLE)
        if (
            legacy_state_columns
            and not self._table_exists(connection, KSADK_STATES_TABLE)
            and {"scope", "agent_id", "state_json"}.issubset(legacy_state_columns)
        ):
            connection.execute(f"ALTER TABLE {LEGACY_STATES_TABLE} RENAME TO {KSADK_STATES_TABLE}")

    @staticmethod
    def _ensure_event_seq_unique_index(connection: sqlite3.Connection) -> None:
        index_name = "idx_ksadk_events_session_seq"
        existing = next(
            (
                row
                for row in connection.execute(
                    f"PRAGMA index_list('{KSADK_EVENTS_TABLE}')"
                ).fetchall()
                if str(row[1]) == index_name
            ),
            None,
        )
        if existing is not None and not bool(existing[2]):
            # ``CREATE UNIQUE INDEX IF NOT EXISTS`` does not upgrade the old
            # ordinary index with the same name.  Replace it explicitly so
            # reopened pre-v2 databases gain the durable cursor invariant.
            connection.execute(f"DROP INDEX {index_name}")
        connection.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} "
            f"ON {KSADK_EVENTS_TABLE} (session_id, seq_id)"
        )

    def _ensure_schema(self) -> None:
        with self._connection() as connection:
            self._migrate_legacy_schema(connection)
            connection.executescript(f"""
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

                CREATE UNIQUE INDEX IF NOT EXISTS idx_ksadk_events_session_seq
                ON {KSADK_EVENTS_TABLE} (session_id, seq_id);

                -- 跨会话事件查询（get_events_for_agent）JOIN sessions 按
                -- s.agent_id 过滤 + ORDER BY e.timestamp；覆盖索引服务 JOIN 键
                -- s.id=e.session_id + 排序。
                CREATE INDEX IF NOT EXISTS idx_ksadk_events_session_ts
                ON {KSADK_EVENTS_TABLE} (session_id, timestamp, id);

                CREATE INDEX IF NOT EXISTS idx_ksadk_events_session_invocation_seq
                ON {KSADK_EVENTS_TABLE} (session_id, invocation_id, seq_id);

                -- ListSessions 按 agent_id 过滤 + updated_at DESC 排序。
                CREATE INDEX IF NOT EXISTS idx_ksadk_sessions_agent_updated
                ON {KSADK_SESSIONS_TABLE} (agent_id, updated_at DESC, id);

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
                """)
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
            self._ensure_event_seq_unique_index(connection)
            connection.commit()


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
