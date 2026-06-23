"""PostgreSQL shared session backend for production multi-pod runtimes."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

from ksadk.sessions.base import (
    BaseSessionService,
    Session,
    SessionEvent,
    SessionState,
    generate_id,
)

KSADK_PG_SESSIONS_TABLE = "ksadk_sessions"
KSADK_PG_EVENTS_TABLE = "ksadk_events"
KSADK_PG_STATES_TABLE = "ksadk_states"


class PostgresSessionService(BaseSessionService):
    def __init__(
        self,
        *,
        dsn: str,
        namespace: str = "default",
        tenant_id: str = "default",
        workspace_id: str = "default",
        min_size: int = 1,
        max_size: int = 10,
    ):
        if not dsn.strip():
            raise ValueError("KSADK_SESSION_DSN is required when KSADK_SESSION_BACKEND=postgres")
        self.dsn = dsn.strip()
        self.namespace = namespace.strip() or "default"
        self.tenant_id = tenant_id.strip() or "default"
        self.workspace_id = workspace_id.strip() or "default"
        self.min_size = min_size
        self.max_size = max_size
        self._pool: Any = None
        self._pool_lock = asyncio.Lock()
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()

    async def create_session(
        self,
        agent_id: str,
        user_id: str,
        session_id: Optional[str] = None,
    ) -> Session:
        await self._ensure_schema()
        session_key = session_id or generate_id()
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                existing = await self._get_session_with_connection(connection, session_key)
                if existing is not None:
                    return existing

                now = time.time()
                await connection.execute(
                    f"""
                    INSERT INTO {KSADK_PG_SESSIONS_TABLE} (
                        namespace, tenant_id, workspace_id, id, agent_id, user_id, title, title_source, summary,
                        first_prompt, last_prompt, state_json, created_at, updated_at, version
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, '', '', '', '', '', $7::jsonb, $8, $9, 0)
                    """,
                    self.namespace,
                    self.tenant_id,
                    self.workspace_id,
                    session_key,
                    agent_id,
                    user_id,
                    "{}",
                    now,
                    now,
                )
                await connection.execute(
                    f"""
                    INSERT INTO {KSADK_PG_STATES_TABLE} (
                        namespace, tenant_id, workspace_id, scope, agent_id, user_id, session_id, state_json, version, updated_at
                    )
                    VALUES ($1, $2, $3, 'session', $4, $5, $6, $7::jsonb, 0, $8)
                    ON CONFLICT (namespace, scope, agent_id, user_id, session_id)
                    DO NOTHING
                    """,
                    self.namespace,
                    self.tenant_id,
                    self.workspace_id,
                    agent_id,
                    user_id,
                    session_key,
                    "{}",
                    now,
                )
                return Session(
                    id=session_key,
                    agent_id=agent_id,
                    user_id=user_id,
                    created_at=now,
                    updated_at=now,
                )

    async def get_session(self, session_id: str) -> Optional[Session]:
        await self._ensure_schema()
        async with self._pool.acquire() as connection:
            return await self._get_session_with_connection(connection, session_id)

    async def list_sessions(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[Session]:
        await self._ensure_schema()
        async with self._pool.acquire() as connection:
            query = f"""
                SELECT id, agent_id, user_id, title, title_source, summary, first_prompt, last_prompt,
                       state_json, created_at, updated_at, version
                FROM {KSADK_PG_SESSIONS_TABLE}
                WHERE namespace = $1 AND agent_id = $2
            """
            params: list[Any] = [self.namespace, agent_id]
            if user_id is not None:
                params.append(user_id)
                query += f" AND user_id = ${len(params)}"
            query += " ORDER BY updated_at DESC, created_at DESC"
            if limit is not None:
                params.append(limit)
                query += f" LIMIT ${len(params)}"
                if offset is not None:
                    params.append(offset)
                    query += f" OFFSET ${len(params)}"
            elif offset is not None:
                params.append(offset)
                query += f" OFFSET ${len(params)}"
            rows = await connection.fetch(query, *params)
            return [self._session_from_row(row, events=[]) for row in rows]

    async def count_sessions(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
    ) -> int:
        await self._ensure_schema()
        async with self._pool.acquire() as connection:
            query = f"""
                SELECT COUNT(*) AS total
                FROM {KSADK_PG_SESSIONS_TABLE}
                WHERE namespace = $1 AND agent_id = $2
            """
            params: list[Any] = [self.namespace, agent_id]
            if user_id is not None:
                params.append(user_id)
                query += f" AND user_id = ${len(params)}"
            return int(await connection.fetchval(query, *params) or 0)

    async def delete_session(self, session_id: str) -> bool:
        await self._ensure_schema()
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    f"""
                    DELETE FROM {KSADK_PG_STATES_TABLE}
                    WHERE namespace = $1 AND session_id = $2
                    """,
                    self.namespace,
                    session_id,
                )
                result = await connection.execute(
                    f"""
                    DELETE FROM {KSADK_PG_SESSIONS_TABLE}
                    WHERE namespace = $1 AND id = $2
                    """,
                    self.namespace,
                    session_id,
                )
                return not result.endswith(" 0")

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
        await self._ensure_schema()
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    f"""
                    SELECT id, agent_id, user_id, title, title_source, summary, first_prompt, last_prompt,
                           state_json, created_at, updated_at, version
                    FROM {KSADK_PG_SESSIONS_TABLE}
                    WHERE namespace = $1 AND id = $2
                    FOR UPDATE
                    """,
                    self.namespace,
                    session_id,
                )
                if row is None:
                    raise ValueError(f"Session {session_id} not found")
                updated_at = time.time()
                next_title = row["title"] if title is None else title
                next_title_source = row["title_source"] if title_source is None else title_source
                next_summary = row["summary"] if summary is None else summary
                next_first_prompt = row["first_prompt"] if first_prompt is None else first_prompt
                next_last_prompt = row["last_prompt"] if last_prompt is None else last_prompt
                await connection.execute(
                    f"""
                    UPDATE {KSADK_PG_SESSIONS_TABLE}
                    SET title = $1, title_source = $2, summary = $3, first_prompt = $4,
                        last_prompt = $5, updated_at = $6
                    WHERE namespace = $7 AND id = $8
                    """,
                    next_title,
                    next_title_source,
                    next_summary,
                    next_first_prompt,
                    next_last_prompt,
                    updated_at,
                    self.namespace,
                    session_id,
                )
                return Session(
                    id=row["id"],
                    agent_id=row["agent_id"],
                    user_id=row["user_id"],
                    title=next_title,
                    title_source=next_title_source,
                    summary=next_summary,
                    first_prompt=next_first_prompt,
                    last_prompt=next_last_prompt,
                    state=self._json_to_dict(row["state_json"]),
                    created_at=row["created_at"],
                    updated_at=updated_at,
                    version=row["version"],
                )

    async def append_event(self, session_id: str, event: SessionEvent) -> SessionEvent:
        await self._ensure_schema()
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                session_row = await connection.fetchrow(
                    f"""
                    SELECT agent_id, user_id, state_json, version
                    FROM {KSADK_PG_SESSIONS_TABLE}
                    WHERE namespace = $1 AND id = $2
                    FOR UPDATE
                    """,
                    self.namespace,
                    session_id,
                )
                if session_row is None:
                    raise ValueError(f"Session {session_id} not found")

                next_seq = await connection.fetchval(
                    f"""
                    SELECT COALESCE(MAX(seq_id), 0) + 1
                    FROM {KSADK_PG_EVENTS_TABLE}
                    WHERE namespace = $1 AND session_id = $2
                    """,
                    self.namespace,
                    session_id,
                )
                stored = SessionEvent(
                    id=event.id or generate_id(),
                    session_id=session_id,
                    author=event.author,
                    event_type=event.event_type,
                    content=dict(event.content),
                    timestamp=event.timestamp,
                    state_delta=dict(event.state_delta),
                    seq_id=int(next_seq or 1),
                    invocation_id=event.invocation_id,
                    metadata=dict(event.metadata),
                )
                await connection.execute(
                    f"""
                    INSERT INTO {KSADK_PG_EVENTS_TABLE} (
                        namespace, tenant_id, workspace_id, id, session_id, author, event_type, content_json, timestamp,
                        state_delta_json, seq_id, invocation_id, metadata_json
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10::jsonb, $11, $12, $13::jsonb)
                    """,
                    self.namespace,
                    self.tenant_id,
                    self.workspace_id,
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
                )

                updated_at = time.time()
                state = self._json_to_dict(session_row["state_json"])
                version = int(session_row["version"] or 0)
                if stored.state_delta:
                    state.update(stored.state_delta)
                    version += 1
                await self._write_session_state(
                    connection,
                    session_id=session_id,
                    agent_id=session_row["agent_id"],
                    user_id=session_row["user_id"],
                    state=state,
                    version=version,
                    updated_at=updated_at,
                )
                return stored

    async def get_events(
        self,
        session_id: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[SessionEvent]:
        await self._ensure_schema()
        async with self._pool.acquire() as connection:
            query = f"""
                SELECT id, session_id, author, event_type, content_json, timestamp,
                       state_delta_json, seq_id, invocation_id, metadata_json
                FROM {KSADK_PG_EVENTS_TABLE}
                WHERE namespace = $1 AND session_id = $2
                ORDER BY seq_id ASC
            """
            params: list[Any] = [self.namespace, session_id]
            if limit is not None:
                params.append(limit)
                query += f" LIMIT ${len(params)}"
                if offset is not None:
                    params.append(offset)
                    query += f" OFFSET ${len(params)}"
            elif offset is not None:
                params.append(offset)
                query += f" OFFSET ${len(params)}"
            rows = await connection.fetch(query, *params)
            return [self._event_from_row(row) for row in rows]

    async def count_events(self, session_id: str) -> int:
        await self._ensure_schema()
        async with self._pool.acquire() as connection:
            query = f"""
                SELECT COUNT(*) AS total
                FROM {KSADK_PG_EVENTS_TABLE}
                WHERE namespace = $1 AND session_id = $2
            """
            return int(await connection.fetchval(query, self.namespace, session_id) or 0)

    async def get_state(
        self,
        agent_id: str,
        user_id: Optional[str],
        session_id: Optional[str],
        scope: str = "session",
    ) -> Optional[SessionState]:
        await self._ensure_schema()
        async with self._pool.acquire() as connection:
            if scope == "session" and session_id:
                session = await self._get_session_with_connection(connection, session_id)
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
            row = await connection.fetchrow(
                f"""
                SELECT scope, agent_id, user_id, session_id, state_json, version, updated_at
                FROM {KSADK_PG_STATES_TABLE}
                WHERE namespace = $1 AND scope = $2 AND agent_id = $3 AND user_id = $4 AND session_id = $5
                """,
                self.namespace,
                scope,
                agent_id,
                user_id or "",
                session_id or "",
            )
            if row is None:
                return None
            return self._state_from_row(row)

    async def update_state(
        self,
        *,
        agent_id: str,
        user_id: Optional[str],
        session_id: Optional[str],
        scope: str,
        state_delta: dict[str, Any],
    ) -> SessionState:
        await self._ensure_schema()
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                updated_at = time.time()
                if scope == "session":
                    if not session_id:
                        raise ValueError("session_id is required for session scope")
                    session = await self._get_session_with_connection(
                        connection,
                        session_id,
                        for_update=True,
                    )
                    if session is None:
                        raise ValueError(f"Session {session_id} not found")
                    next_state = dict(session.state)
                    next_state.update(state_delta)
                    next_version = session.version + 1
                    await self._write_session_state(
                        connection,
                        session_id=session.id,
                        agent_id=session.agent_id,
                        user_id=session.user_id,
                        state=next_state,
                        version=next_version,
                        updated_at=updated_at,
                    )
                    return SessionState(
                        scope="session",
                        agent_id=session.agent_id,
                        user_id=session.user_id,
                        session_id=session.id,
                        state=next_state,
                        version=next_version,
                        updated_at=updated_at,
                    )

                row = await connection.fetchrow(
                    f"""
                    SELECT state_json, version
                    FROM {KSADK_PG_STATES_TABLE}
                    WHERE namespace = $1 AND scope = $2 AND agent_id = $3 AND user_id = $4 AND session_id = $5
                    FOR UPDATE
                    """,
                    self.namespace,
                    scope,
                    agent_id,
                    user_id or "",
                    session_id or "",
                )
                next_state = self._json_to_dict(row["state_json"]) if row else {}
                next_state.update(state_delta)
                next_version = (int(row["version"] or 0) + 1) if row else 1
                await connection.execute(
                    f"""
                    INSERT INTO {KSADK_PG_STATES_TABLE} (
                        namespace, tenant_id, workspace_id, scope, agent_id, user_id, session_id, state_json, version, updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10)
                    ON CONFLICT (namespace, scope, agent_id, user_id, session_id)
                    DO UPDATE SET state_json = EXCLUDED.state_json,
                                  version = EXCLUDED.version,
                                  updated_at = EXCLUDED.updated_at
                    """,
                    self.namespace,
                    self.tenant_id,
                    self.workspace_id,
                    scope,
                    agent_id,
                    user_id or "",
                    session_id or "",
                    json.dumps(next_state),
                    next_version,
                    updated_at,
                )
                return SessionState(
                    scope=scope,
                    agent_id=agent_id,
                    user_id=user_id or "",
                    session_id=session_id or "",
                    state=next_state,
                    version=next_version,
                    updated_at=updated_at,
                )

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._schema_ready = False

    async def _ensure_pool(self) -> None:
        if self._pool is not None:
            return
        async with self._pool_lock:
            if self._pool is not None:
                return
            try:
                import asyncpg
            except ImportError as exc:
                raise RuntimeError(
                    "asyncpg is required for KSADK_SESSION_BACKEND=postgres"
                ) from exc
            self._pool = await asyncpg.create_pool(
                dsn=self.dsn,
                min_size=self.min_size,
                max_size=self.max_size,
            )

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            await self._ensure_pool()
            async with self._pool.acquire() as connection:
                await connection.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {KSADK_PG_SESSIONS_TABLE} (
                        namespace TEXT NOT NULL,
                        tenant_id TEXT NOT NULL DEFAULT 'default',
                        workspace_id TEXT NOT NULL DEFAULT 'default',
                        id TEXT NOT NULL,
                        agent_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        title TEXT NOT NULL DEFAULT '',
                        title_source TEXT NOT NULL DEFAULT '',
                        summary TEXT NOT NULL DEFAULT '',
                        first_prompt TEXT NOT NULL DEFAULT '',
                        last_prompt TEXT NOT NULL DEFAULT '',
                        state_json JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        created_at DOUBLE PRECISION NOT NULL,
                        updated_at DOUBLE PRECISION NOT NULL,
                        version INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (namespace, id)
                    );

                    CREATE TABLE IF NOT EXISTS {KSADK_PG_EVENTS_TABLE} (
                        namespace TEXT NOT NULL,
                        tenant_id TEXT NOT NULL DEFAULT 'default',
                        workspace_id TEXT NOT NULL DEFAULT 'default',
                        id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        author TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        content_json JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        timestamp DOUBLE PRECISION NOT NULL,
                        state_delta_json JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        seq_id INTEGER NOT NULL,
                        invocation_id TEXT,
                        metadata_json JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        PRIMARY KEY (namespace, id),
                        UNIQUE (namespace, session_id, seq_id),
                        FOREIGN KEY (namespace, session_id)
                            REFERENCES {KSADK_PG_SESSIONS_TABLE}(namespace, id)
                            ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_ksadk_pg_events_session_seq
                    ON {KSADK_PG_EVENTS_TABLE} (namespace, session_id, seq_id);

                    CREATE TABLE IF NOT EXISTS {KSADK_PG_STATES_TABLE} (
                        namespace TEXT NOT NULL,
                        tenant_id TEXT NOT NULL DEFAULT 'default',
                        workspace_id TEXT NOT NULL DEFAULT 'default',
                        scope TEXT NOT NULL,
                        agent_id TEXT NOT NULL,
                        user_id TEXT NOT NULL DEFAULT '',
                        session_id TEXT NOT NULL DEFAULT '',
                        state_json JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        version INTEGER NOT NULL DEFAULT 0,
                        updated_at DOUBLE PRECISION NOT NULL,
                        PRIMARY KEY (namespace, scope, agent_id, user_id, session_id)
                    );

                    ALTER TABLE {KSADK_PG_SESSIONS_TABLE}
                    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default';
                    ALTER TABLE {KSADK_PG_SESSIONS_TABLE}
                    ADD COLUMN IF NOT EXISTS workspace_id TEXT NOT NULL DEFAULT 'default';
                    ALTER TABLE {KSADK_PG_EVENTS_TABLE}
                    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default';
                    ALTER TABLE {KSADK_PG_EVENTS_TABLE}
                    ADD COLUMN IF NOT EXISTS workspace_id TEXT NOT NULL DEFAULT 'default';
                    ALTER TABLE {KSADK_PG_STATES_TABLE}
                    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default';
                    ALTER TABLE {KSADK_PG_STATES_TABLE}
                    ADD COLUMN IF NOT EXISTS workspace_id TEXT NOT NULL DEFAULT 'default';
                    """
                )
            self._schema_ready = True

    async def _get_session_with_connection(
        self,
        connection: Any,
        session_id: str,
        *,
        for_update: bool = False,
    ) -> Optional[Session]:
        lock_clause = " FOR UPDATE" if for_update else ""
        row = await connection.fetchrow(
            f"""
            SELECT id, agent_id, user_id, title, title_source, summary, first_prompt, last_prompt,
                   state_json, created_at, updated_at, version
            FROM {KSADK_PG_SESSIONS_TABLE}
            WHERE namespace = $1 AND id = $2
            {lock_clause}
            """,
            self.namespace,
            session_id,
        )
        if row is None:
            return None
        events = [] if for_update else await self._get_events_with_connection(connection, session_id)
        return self._session_from_row(row, events=events)

    async def _get_events_with_connection(self, connection: Any, session_id: str) -> list[SessionEvent]:
        rows = await connection.fetch(
            f"""
            SELECT id, session_id, author, event_type, content_json, timestamp,
                   state_delta_json, seq_id, invocation_id, metadata_json
            FROM {KSADK_PG_EVENTS_TABLE}
            WHERE namespace = $1 AND session_id = $2
            ORDER BY seq_id ASC
            """,
            self.namespace,
            session_id,
        )
        return [self._event_from_row(row) for row in rows]

    async def _write_session_state(
        self,
        connection: Any,
        *,
        session_id: str,
        agent_id: str,
        user_id: str,
        state: dict[str, Any],
        version: int,
        updated_at: float,
    ) -> None:
        await connection.execute(
            f"""
            UPDATE {KSADK_PG_SESSIONS_TABLE}
            SET state_json = $1::jsonb, updated_at = $2, version = $3
            WHERE namespace = $4 AND id = $5
            """,
            json.dumps(state),
            updated_at,
            version,
            self.namespace,
            session_id,
        )
        await connection.execute(
            f"""
            INSERT INTO {KSADK_PG_STATES_TABLE} (
                namespace, tenant_id, workspace_id, scope, agent_id, user_id, session_id, state_json, version, updated_at
            )
            VALUES ($1, $2, $3, 'session', $4, $5, $6, $7::jsonb, $8, $9)
            ON CONFLICT (namespace, scope, agent_id, user_id, session_id)
            DO UPDATE SET state_json = EXCLUDED.state_json,
                          version = EXCLUDED.version,
                          updated_at = EXCLUDED.updated_at
            """,
            self.namespace,
            self.tenant_id,
            self.workspace_id,
            agent_id,
            user_id,
            session_id,
            json.dumps(state),
            version,
            updated_at,
        )

    @classmethod
    def _session_from_row(cls, row: Any, *, events: list[SessionEvent]) -> Session:
        return Session(
            id=row["id"],
            agent_id=row["agent_id"],
            user_id=row["user_id"],
            title=row["title"],
            title_source=row["title_source"],
            summary=row["summary"],
            first_prompt=row["first_prompt"],
            last_prompt=row["last_prompt"],
            state=cls._json_to_dict(row["state_json"]),
            events=events,
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            version=int(row["version"] or 0),
        )

    @classmethod
    def _event_from_row(cls, row: Any) -> SessionEvent:
        return SessionEvent(
            id=row["id"],
            session_id=row["session_id"],
            author=row["author"],
            event_type=row["event_type"],
            content=cls._json_to_dict(row["content_json"]),
            timestamp=float(row["timestamp"]),
            state_delta=cls._json_to_dict(row["state_delta_json"]),
            seq_id=int(row["seq_id"] or 0),
            invocation_id=row["invocation_id"],
            metadata=cls._json_to_dict(row["metadata_json"]),
        )

    @classmethod
    def _state_from_row(cls, row: Any) -> SessionState:
        return SessionState(
            scope=row["scope"],
            agent_id=row["agent_id"],
            user_id=row["user_id"],
            session_id=row["session_id"],
            state=cls._json_to_dict(row["state_json"]),
            version=int(row["version"] or 0),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _json_to_dict(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            return dict(json.loads(value or "{}"))
        return dict(value)


def create_postgres_session_service(
    *,
    dsn: str,
    namespace: str = "default",
    tenant_id: str = "default",
    workspace_id: str = "default",
) -> PostgresSessionService:
    return PostgresSessionService(
        dsn=dsn,
        namespace=namespace,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )


__all__ = [
    "KSADK_PG_EVENTS_TABLE",
    "KSADK_PG_SESSIONS_TABLE",
    "KSADK_PG_STATES_TABLE",
    "PostgresSessionService",
    "create_postgres_session_service",
]
