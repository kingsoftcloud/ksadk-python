"""PostgreSQL shared session backend for production multi-pod runtimes."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from contextvars import ContextVar
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from ksadk.ids import new_session_id
from ksadk.sessions._postgres_schema import _PostgresSchemaMixin
from ksadk.sessions._postgres_tables import (
    KSADK_PG_EVENTS_TABLE,
    KSADK_PG_SESSIONS_TABLE,
    KSADK_PG_STATES_TABLE,
)
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
from ksadk.sessions.errors import SessionBackendUnavailable

logger = logging.getLogger(__name__)


class PostgresSessionService(_PostgresSchemaMixin, BaseSessionService):
    storage_capabilities = CANONICAL_EVENT_STORAGE_CAPABILITIES

    def __init__(
        self,
        *,
        dsn: str,
        namespace: str = "default",
        tenant_id: str = "default",
        workspace_id: str = "default",
        min_size: int = 1,
        max_size: int = 10,
        connect_timeout: float = 5.0,
    ):
        if not dsn.strip():
            raise ValueError("KSADK_SESSION_DSN is required when KSADK_SESSION_BACKEND=postgres")
        self.dsn = dsn.strip()
        self.namespace = namespace.strip() or "default"
        self.tenant_id = tenant_id.strip() or "default"
        self.workspace_id = workspace_id.strip() or "default"
        self.min_size = min_size
        self.max_size = max_size
        self.connect_timeout = connect_timeout
        self._pool: Any = None
        self._pool_lock = asyncio.Lock()
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()
        self._checkpoint_snapshot_connection: ContextVar[Any | None] = ContextVar(
            f"checkpoint_snapshot_connection_{id(self)}", default=None
        )

    async def create_session(
        self,
        agent_id: str,
        user_id: str,
        session_id: Optional[str] = None,
    ) -> Session:
        await self._ensure_schema()
        session_key = session_id or new_session_id()
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                existing = await self._get_session_with_connection(connection, session_key)
                if existing is not None:
                    return existing

                now = time.time()
                await connection.execute(
                    f"""
                    INSERT INTO {KSADK_PG_SESSIONS_TABLE} (
                        namespace, tenant_id, workspace_id, id, agent_id, user_id,
                        title, title_source, summary, first_prompt, last_prompt,
                        state_json, created_at, updated_at, version
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, '', '', '', '', '', $7::jsonb, $8, $9, 0)
                    ON CONFLICT (namespace, id) DO NOTHING
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
                persisted = await self._get_session_with_connection(
                    connection,
                    session_key,
                    for_update=True,
                )
                if persisted is None:
                    raise RuntimeError(f"Failed to create Postgres session {session_key}")
                await connection.execute(
                    f"""
                    INSERT INTO {KSADK_PG_STATES_TABLE} (
                        namespace, tenant_id, workspace_id, scope, agent_id,
                        user_id, session_id, state_json, version, updated_at
                    )
                    VALUES ($1, $2, $3, 'session', $4, $5, $6, $7::jsonb, 0, $8)
                    ON CONFLICT (namespace, scope, agent_id, user_id, session_id)
                    DO NOTHING
                    """,
                    self.namespace,
                    self.tenant_id,
                    self.workspace_id,
                    persisted.agent_id,
                    persisted.user_id,
                    session_key,
                    "{}",
                    now,
                )
                return persisted

    async def get_session(self, session_id: str) -> Optional[Session]:
        await self._ensure_schema()
        async with self._pool.acquire() as connection:
            return await self._get_session_with_connection(connection, session_id)

    async def get_session_metadata(self, session_id: str) -> Optional[Session]:
        await self._ensure_schema()
        async with self._pool.acquire() as connection:
            return await self._get_session_with_connection(
                connection,
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
        await self._ensure_schema()
        async with self._pool.acquire() as connection:
            query = f"""
                SELECT id, agent_id, user_id, title, title_source, summary,
                       first_prompt, last_prompt,
                       state_json, created_at, updated_at, version
                FROM {KSADK_PG_SESSIONS_TABLE}
                WHERE namespace = $1 AND agent_id = $2
            """
            params: list[Any] = [self.namespace, agent_id]
            if user_id is not None:
                params.append(user_id)
                query += f" AND user_id = ${len(params)}"
            query += " ORDER BY updated_at DESC, created_at DESC, id DESC"
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
                    SELECT id, agent_id, user_id, title, title_source, summary,
                           first_prompt, last_prompt,
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
                    seq_binding=event.seq_binding,
                )
                stored.bind_seq_id(int(next_seq or 1))
                await connection.execute(
                    f"""
                    INSERT INTO {KSADK_PG_EVENTS_TABLE} (
                        namespace, tenant_id, workspace_id, id, session_id, author,
                        event_type, content_json, timestamp,
                        state_delta_json, seq_id, invocation_id, metadata_json
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9,
                        $10::jsonb, $11, $12, $13::jsonb
                    )
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

    async def get_event_by_id(self, session_id: str, event_id: str) -> Optional[SessionEvent]:
        await self._ensure_schema()
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT id, session_id, author, event_type, content_json, timestamp,
                       state_delta_json, seq_id, invocation_id, metadata_json
                FROM {KSADK_PG_EVENTS_TABLE}
                WHERE namespace = $1 AND session_id = $2 AND id = $3
                """,
                self.namespace,
                session_id,
                event_id,
            )
            return self._event_from_row(row) if row is not None else None

    async def get_events_by_invocation_id(
        self,
        session_id: str,
        invocation_id: str,
        *,
        after_seq_id: Optional[int] = None,
        before_seq_id: Optional[int] = None,
    ) -> list[SessionEvent]:
        await self._ensure_schema()
        conditions = ["namespace = $1", "session_id = $2", "invocation_id = $3"]
        params: list[Any] = [self.namespace, session_id, invocation_id]
        if after_seq_id is not None:
            params.append(after_seq_id)
            conditions.append(f"seq_id > ${len(params)}")
        if before_seq_id is not None:
            params.append(before_seq_id)
            conditions.append(f"seq_id < ${len(params)}")
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT id, session_id, author, event_type, content_json, timestamp,
                       state_delta_json, seq_id, invocation_id, metadata_json
                FROM {KSADK_PG_EVENTS_TABLE}
                WHERE {" AND ".join(conditions)}
                ORDER BY seq_id ASC
                """,
                *params,
            )
            return [self._event_from_row(row) for row in rows]

    async def get_events(
        self,
        session_id: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        after_seq_id: Optional[int] = None,
        before_seq_id: Optional[int] = None,
    ) -> list[SessionEvent]:
        await self._ensure_schema()
        # seq 过滤先应用,再对结果集应用"最新 N 条" offset/limit 语义。
        conditions = ["namespace = $1", "session_id = $2"]
        params: list[Any] = [self.namespace, session_id]
        if after_seq_id is not None:
            params.append(after_seq_id)
            conditions.append(f"seq_id > ${len(params)}")
        if before_seq_id is not None:
            params.append(before_seq_id)
            conditions.append(f"seq_id < ${len(params)}")
        where_clause = " AND ".join(conditions)
        async with self._pool.acquire() as connection:
            if limit is not None:
                params.extend([limit, offset or 0])
                limit_param = len(params) - 1
                offset_param = len(params)
                query = f"""
                    SELECT id, session_id, author, event_type, content_json, timestamp,
                           state_delta_json, seq_id, invocation_id, metadata_json
                    FROM (
                        SELECT id, session_id, author, event_type, content_json, timestamp,
                               state_delta_json, seq_id, invocation_id, metadata_json
                        FROM {KSADK_PG_EVENTS_TABLE}
                        WHERE {where_clause}
                        ORDER BY seq_id DESC
                        LIMIT ${limit_param} OFFSET ${offset_param}
                    ) AS latest_events
                    ORDER BY seq_id ASC
                """
            elif offset is not None:
                params.append(offset)
                offset_param = len(params)
                query = f"""
                    SELECT id, session_id, author, event_type, content_json, timestamp,
                           state_delta_json, seq_id, invocation_id, metadata_json
                    FROM (
                        SELECT id, session_id, author, event_type, content_json, timestamp,
                               state_delta_json, seq_id, invocation_id, metadata_json
                        FROM {KSADK_PG_EVENTS_TABLE}
                        WHERE {where_clause}
                        ORDER BY seq_id DESC
                        OFFSET ${offset_param}
                    ) AS latest_events
                    ORDER BY seq_id ASC
                """
            else:
                query = f"""
                    SELECT id, session_id, author, event_type, content_json, timestamp,
                           state_delta_json, seq_id, invocation_id, metadata_json
                    FROM {KSADK_PG_EVENTS_TABLE}
                    WHERE {where_clause}
                    ORDER BY seq_id ASC
                """
            rows = await connection.fetch(query, *params)
            return [self._event_from_row(row) for row in rows]

    async def count_events(
        self,
        session_id: str,
        after_seq_id: Optional[int] = None,
        before_seq_id: Optional[int] = None,
    ) -> int:
        await self._ensure_schema()
        conditions = ["namespace = $1", "session_id = $2"]
        params: list[Any] = [self.namespace, session_id]
        if after_seq_id is not None:
            params.append(after_seq_id)
            conditions.append(f"seq_id > ${len(params)}")
        if before_seq_id is not None:
            params.append(before_seq_id)
            conditions.append(f"seq_id < ${len(params)}")
        where_clause = " AND ".join(conditions)
        async with self._pool.acquire() as connection:
            query = f"""
                SELECT COUNT(*) AS total
                FROM {KSADK_PG_EVENTS_TABLE}
                WHERE {where_clause}
            """
            return int(await connection.fetchval(query, *params) or 0)

    async def get_sessions_by_ids(self, session_ids: list[str]) -> list[Session]:
        if not session_ids:
            return []
        await self._ensure_schema()
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"SELECT id,agent_id,user_id,title,title_source,summary,first_prompt,"
                f"last_prompt,state_json,created_at,updated_at,version "
                f"FROM {KSADK_PG_SESSIONS_TABLE} WHERE namespace=$1 "
                "AND id=ANY($2::text[])",
                self.namespace,
                session_ids,
            )
        by_id = {row["id"]: self._session_from_row(row, events=[]) for row in rows}
        return [by_id[item] for item in session_ids if item in by_id]

    async def list_session_metadata(
        self, agent_id: Optional[str] = None, user_id: Optional[str] = None
    ) -> list[Session]:
        await self._ensure_schema()
        clauses = ["namespace=$1"]
        params: list[Any] = [self.namespace]
        if agent_id is not None:
            params.append(agent_id)
            clauses.append(f"agent_id=${len(params)}")
        if user_id is not None:
            params.append(user_id)
            clauses.append(f"user_id=${len(params)}")
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"SELECT id,agent_id,user_id,title,title_source,summary,first_prompt,"
                f"last_prompt,state_json,created_at,updated_at,version "
                f"FROM {KSADK_PG_SESSIONS_TABLE} WHERE {' AND '.join(clauses)} "
                "ORDER BY updated_at DESC,created_at DESC,id DESC",
                *params,
            )
        return [self._session_from_row(row, events=[]) for row in rows]

    def _batch_event_where(self, query: SessionEventQuery) -> tuple[list[str], list[Any]]:
        clauses = ["event_row.namespace=$1"]
        params: list[Any] = [self.namespace]
        if query.session_ids is not None:
            if not query.session_ids:
                return ["FALSE"], []
            params.append(query.session_ids)
            clauses.append(f"event_row.session_id=ANY(${len(params)}::text[])")
        for value, clause in (
            (query.agent_id, "session_row.agent_id"),
            (query.after_seq_id, "event_row.seq_id >"),
            (query.before_seq_id, "event_row.seq_id <"),
            (query.invocation_id, "event_row.invocation_id"),
        ):
            if value is not None:
                params.append(value)
                operator = "" if clause.endswith((">", "<")) else "="
                clauses.append(f"{clause}{operator}${len(params)}")
        if query.event_types:
            params.append(query.event_types)
            clauses.append(f"event_row.event_type=ANY(${len(params)}::text[])")
        if query.run_id is not None:
            params.append(query.run_id)
            clauses.append(
                "(event_row.metadata_json->>'run_id'="
                f"${len(params)}"
                " OR (event_row.event_type='continuation.created'"
                f" AND event_row.content_json->'runtime_event'->>'run_id'=${len(params)}))"
            )
        if query.checkpoint_id is not None:
            params.append(query.checkpoint_id)
            clauses.append(
                "(event_row.metadata_json->>'checkpoint_id'="
                f"${len(params)}"
                " OR (event_row.event_type='continuation.created'"
                f" AND event_row.content_json->'runtime_event'->>'continuation_id'=${len(params)}))"
            )
        if query.checkpoint_ids:
            params.append(query.checkpoint_ids)
            clauses.append(
                "(event_row.metadata_json->>'checkpoint_id'=ANY("
                f"${len(params)}::text[])"
                " OR (event_row.event_type='continuation.created'"
                " AND event_row.content_json->'runtime_event'->>'continuation_id'=ANY("
                f"${len(params)}::text[])))"
            )
        return clauses, params

    async def query_events(self, query: SessionEventQuery) -> list[SessionEvent]:
        await self._ensure_schema()
        clauses, params = self._batch_event_where(query)
        direction = "ASC" if query.from_start else "DESC"
        order = (
            f"event_row.session_id {direction},event_row.seq_id {direction},"
            f"event_row.id {direction}"
            if query.order_by_seq
            else f"event_row.timestamp {direction},event_row.session_id {direction},"
            f"event_row.seq_id {direction},event_row.id {direction}"
        )
        params.extend([query.limit, query.offset])
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"SELECT event_row.id,event_row.session_id,event_row.author,event_row.event_type,"
                f"event_row.content_json,event_row.timestamp,event_row.state_delta_json,"
                f"event_row.seq_id,event_row.invocation_id,event_row.metadata_json "
                f"FROM {KSADK_PG_EVENTS_TABLE} event_row "
                f"JOIN {KSADK_PG_SESSIONS_TABLE} session_row "
                "ON session_row.namespace=event_row.namespace "
                "AND session_row.id=event_row.session_id "
                f"WHERE {' AND '.join(clauses)} ORDER BY {order} "
                f"LIMIT ${len(params)-1} OFFSET ${len(params)}",
                *params,
            )
        events = [self._event_from_row(row) for row in rows]
        if not query.from_start:
            events.reverse()
        return events

    async def count_event_query(self, query: SessionEventQuery) -> int:
        await self._ensure_schema()
        clauses, params = self._batch_event_where(query)
        async with self._pool.acquire() as connection:
            return int(
                await connection.fetchval(
                    f"SELECT COUNT(*) FROM {KSADK_PG_EVENTS_TABLE} event_row "
                    f"JOIN {KSADK_PG_SESSIONS_TABLE} session_row "
                    "ON session_row.namespace=event_row.namespace "
                    "AND session_row.id=event_row.session_id "
                    f"WHERE {' AND '.join(clauses)}",
                    *params,
                )
                or 0
            )

    async def scan_checkpoint_events(
        self, query: CheckpointEventQuery
    ) -> list[SessionEvent]:
        if query.limit < 1 or query.limit > 50:
            raise ValueError("checkpoint scan limit must be between 1 and 50")
        return await self._scan_checkpoint_events_with_connection(None, query)

    async def _scan_checkpoint_events_with_connection(
        self,
        connection: Any | None,
        query: CheckpointEventQuery,
        cursor: tuple[float, str, int, str] | None = None,
    ) -> list[SessionEvent]:
        event_query = SessionEventQuery(
            session_ids=query.session_ids,
            agent_id=query.agent_id,
            event_types=["run_checkpoint", "continuation.created"],
            offset=query.offset,
            limit=query.limit,
            from_start=True,
        )
        if query.framework is None and connection is None:
            return await self.query_events(event_query)
        await self._ensure_schema()
        clauses, params = self._batch_event_where(event_query)
        if query.checkpoint_ids:
            params.append(query.checkpoint_ids)
            clauses.append(
                "(event_row.metadata_json->>'checkpoint_id'=ANY("
                f"${len(params)}::text[])"
                " OR (event_row.event_type='continuation.created'"
                " AND event_row.content_json->'runtime_event'->>'continuation_id'=ANY("
                f"${len(params)}::text[])))"
            )
        if query.run_id is not None:
            params.append(query.run_id)
            clauses.append(
                "(event_row.metadata_json->>'run_id'="
                f"${len(params)}"
                " OR (event_row.event_type='continuation.created'"
                f" AND event_row.content_json->'runtime_event'->>'run_id'=${len(params)}))"
            )
        if query.framework is not None:
            params.append(query.framework.lower())
            clauses.append(
                "(lower(event_row.metadata_json->>'framework')="
                f"${len(params)}"
                " OR (event_row.event_type='continuation.created'"
                " AND lower(event_row.content_json->'runtime_event'->'source'->>'framework')="
                f"${len(params)}))"
            )
        if cursor is not None:
            placeholders = []
            for value in cursor:
                params.append(value)
                placeholders.append(f"${len(params)}")
            clauses.append(
                "(event_row.timestamp,event_row.session_id,event_row.seq_id,event_row.id) > "
                f"({','.join(placeholders)})"
            )
        params.extend([query.limit, query.offset])
        owns_connection = connection is None
        if owns_connection:
            connection = await self._pool.acquire()
        try:
            rows = await connection.fetch(
                f"SELECT event_row.id,event_row.session_id,event_row.author,event_row.event_type,"
                f"event_row.content_json,event_row.timestamp,event_row.state_delta_json,"
                f"event_row.seq_id,event_row.invocation_id,event_row.metadata_json "
                f"FROM {KSADK_PG_EVENTS_TABLE} event_row "
                f"JOIN {KSADK_PG_SESSIONS_TABLE} session_row "
                "ON session_row.namespace=event_row.namespace "
                "AND session_row.id=event_row.session_id "
                f"WHERE {' AND '.join(clauses)} ORDER BY event_row.timestamp,event_row.session_id,"
                f"event_row.seq_id,event_row.id LIMIT ${len(params)-1} OFFSET ${len(params)}",
                *params,
            )
            return [self._event_from_row(row) for row in rows]
        finally:
            if owns_connection:
                await self._pool.release(connection)

    async def iter_checkpoint_event_chunks(
        self, query: CheckpointEventQuery
    ) -> AsyncIterator[list[SessionEvent]]:
        if query.limit < 1 or query.limit > 50:
            raise ValueError("checkpoint scan limit must be between 1 and 50")
        await self._ensure_schema()
        async with self._pool.acquire() as connection:
            async with connection.transaction(isolation="repeatable_read", readonly=True):
                token = self._checkpoint_snapshot_connection.set(connection)
                try:
                    cursor: tuple[float, str, int, str] | None = None
                    first_page = True
                    while True:
                        batch = await self._scan_checkpoint_events_with_connection(
                            connection,
                            CheckpointEventQuery(
                                **{
                                    **query.__dict__,
                                    "offset": query.offset if first_page else 0,
                                }
                            ),
                            cursor,
                        )
                        if not batch:
                            break
                        yield batch
                        if len(batch) < query.limit:
                            break
                        last = batch[-1]
                        cursor = (
                            last.timestamp,
                            last.session_id,
                            last.seq_id,
                            last.id,
                        )
                        first_page = False
                finally:
                    self._checkpoint_snapshot_connection.reset(token)

    async def get_checkpoint_lookup_stats(
        self, session_id: str, run_id: str, checkpoint_id: str
    ) -> dict[str, object]:
        events = await self.query_events(
            SessionEventQuery(
                session_ids=[session_id], run_id=run_id, limit=2**31 - 1, from_start=True
            )
        )
        candidate = None
        max_seq_id = 0
        resume_count = 0
        last_resumed_at = None
        for event in events:
            metadata = event.metadata or {}
            if event.event_type == "run_checkpoint":
                max_seq_id = max(max_seq_id, event.seq_id)
                if str(metadata.get("checkpoint_id") or "") == checkpoint_id:
                    candidate = event
            elif event.event_type == "run_resume" and str(
                metadata.get("checkpoint_id") or ""
            ) == checkpoint_id:
                resume_count += 1
                last_resumed_at = max(last_resumed_at or event.timestamp, event.timestamp)
        return {"candidate": candidate, "max_seq_id": max_seq_id, "resume_count": resume_count,
                "last_resumed_at": last_resumed_at}

    async def get_checkpoint_stats(
        self, keys: list[tuple[str, str, str]]
    ) -> dict[str, object]:
        if len(keys) > 50:
            raise ValueError("checkpoint stats batch cannot exceed 50 keys")
        unique_keys = list(dict.fromkeys(keys))
        audits = {
            key: {"resume_count": 0, "last_resumed_at": None} for key in unique_keys
        }
        run_keys = list(
            dict.fromkeys((session_id, run_id) for session_id, run_id, _ in unique_keys)
        )
        latest = {key: 0 for key in run_keys}
        if not unique_keys:
            return {"audits": audits, "latest_seq_ids": latest}
        await self._ensure_schema()
        connection = self._checkpoint_snapshot_connection.get()
        owns_connection = connection is None
        if owns_connection:
            connection = await self._pool.acquire()
        try:
            audit_rows = await connection.fetch(
                f"""WITH requested(session_id,run_id,checkpoint_id) AS (
                    SELECT * FROM unnest($2::text[],$3::text[],$4::text[])
                )
                SELECT requested.session_id,requested.run_id,requested.checkpoint_id,
                       COUNT(event_row.id) AS resume_count,
                       MAX(event_row.timestamp) AS last_resumed_at
                FROM requested
                LEFT JOIN {KSADK_PG_EVENTS_TABLE} event_row
                  ON event_row.namespace=$1
                 AND event_row.session_id=requested.session_id
                 AND event_row.event_type='run_resume'
                 AND event_row.metadata_json->>'run_id'=requested.run_id
                 AND event_row.metadata_json->>'checkpoint_id'=requested.checkpoint_id
                GROUP BY requested.session_id,requested.run_id,requested.checkpoint_id""",
                self.namespace,
                [key[0] for key in unique_keys],
                [key[1] for key in unique_keys],
                [key[2] for key in unique_keys],
            )
            latest_rows = await connection.fetch(
                f"""WITH requested(session_id,run_id) AS (
                    SELECT * FROM unnest($2::text[],$3::text[])
                )
                SELECT requested.session_id,requested.run_id,
                       COALESCE(MAX(event_row.seq_id),0) AS latest_seq_id
                FROM requested
                LEFT JOIN {KSADK_PG_EVENTS_TABLE} event_row
                  ON event_row.namespace=$1
                 AND event_row.session_id=requested.session_id
                 AND event_row.event_type='run_checkpoint'
                 AND event_row.metadata_json->>'run_id'=requested.run_id
                GROUP BY requested.session_id,requested.run_id""",
                self.namespace,
                [key[0] for key in run_keys],
                [key[1] for key in run_keys],
            )
        finally:
            if owns_connection:
                await self._pool.release(connection)
        for row in audit_rows:
            audits[(row["session_id"], row["run_id"], row["checkpoint_id"])] = {
                "resume_count": int(row["resume_count"] or 0),
                "last_resumed_at": row["last_resumed_at"],
            }
        for row in latest_rows:
            latest[(row["session_id"], row["run_id"])] = int(
                row["latest_seq_id"] or 0
            )
        return {"audits": audits, "latest_seq_ids": latest}

    def _agent_events_query_parts(
        self,
        agent_id: str,
        user_id: Optional[str],
    ) -> tuple[str, list[Any]]:
        """跨会话事件查询的公共 JOIN/WHERE。events 表无 agent_id 列，需 join sessions。"""
        conditions = [
            "e.namespace = $1",
            "s.namespace = e.namespace",
            "s.id = e.session_id",
            "s.agent_id = $2",
        ]
        params: list[Any] = [self.namespace, agent_id]
        if user_id is not None:
            params.append(user_id)
            conditions.append(f"s.user_id = ${len(params)}")
        where_clause = " AND ".join(conditions)
        from_clause = (
            f"FROM {KSADK_PG_EVENTS_TABLE} e "
            f"JOIN {KSADK_PG_SESSIONS_TABLE} s "
            "ON s.namespace = e.namespace AND s.id = e.session_id "
            f"WHERE {where_clause}"
        )
        return from_clause, params

    async def get_events_for_agent(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[SessionEvent]:
        await self._ensure_schema()
        from_clause, params = self._agent_events_query_parts(agent_id, user_id)
        columns = (
            "e.id, e.session_id, e.author, e.event_type, e.content_json, e.timestamp, "
            "e.state_delta_json, e.seq_id, e.invocation_id, e.metadata_json"
        )
        async with self._pool.acquire() as connection:
            if limit is not None or offset is not None:
                # 与 get_events 一致的"最新 N 条"尾部语义：先 DESC 取窗口再 ASC 重排
                if limit is not None:
                    params.append(limit)
                    limit_sql = f"LIMIT ${len(params)}"
                else:
                    limit_sql = ""
                params.append(offset or 0)
                offset_sql = f"OFFSET ${len(params)}"
                query = f"""
                    SELECT id, session_id, author, event_type, content_json, timestamp,
                           state_delta_json, seq_id, invocation_id, metadata_json
                    FROM (
                        SELECT {columns}
                        {from_clause}
                        ORDER BY e.timestamp DESC, e.seq_id DESC, e.id DESC
                        {limit_sql} {offset_sql}
                    ) AS latest_events
                    ORDER BY timestamp ASC, seq_id ASC, id ASC
                """
            else:
                query = f"""
                    SELECT {columns}
                    {from_clause}
                    ORDER BY e.timestamp ASC, e.seq_id ASC, e.id ASC
                """
            rows = await connection.fetch(query, *params)
            return [self._event_from_row(row) for row in rows]

    async def count_events_for_agent(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
    ) -> int:
        await self._ensure_schema()
        from_clause, params = self._agent_events_query_parts(agent_id, user_id)
        async with self._pool.acquire() as connection:
            query = f"SELECT COUNT(*) AS total {from_clause}"
            return int(await connection.fetchval(query, *params) or 0)

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
                WHERE namespace = $1 AND scope = $2 AND agent_id = $3
                  AND user_id = $4 AND session_id = $5
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
                    WHERE namespace = $1 AND scope = $2 AND agent_id = $3
                      AND user_id = $4 AND session_id = $5
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
                        namespace, tenant_id, workspace_id, scope, agent_id,
                        user_id, session_id, state_json, version, updated_at
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
        pool = self._pool
        self._pool = None
        self._schema_ready = False
        if pool is None:
            return
        try:
            await asyncio.wait_for(pool.close(), timeout=self.connect_timeout)
        except (TimeoutError, OSError, ConnectionError, asyncio.TimeoutError):
            pool.terminate()

    async def _ensure_pool(self) -> None:
        if self._pool is not None:
            return
        async with self._pool_lock:
            if self._pool is not None:
                return
            try:
                import asyncpg  # type: ignore[import-untyped]
            except ImportError as exc:
                raise SessionBackendUnavailable(
                    "asyncpg is required for KSADK_SESSION_BACKEND=postgres"
                ) from exc
            try:
                self._pool = await asyncpg.create_pool(
                    dsn=self.dsn,
                    min_size=self.min_size,
                    max_size=self.max_size,
                    timeout=self.connect_timeout,
                    command_timeout=self.connect_timeout,
                )
            except (TimeoutError, OSError, ConnectionError, asyncio.TimeoutError) as exc:
                raise SessionBackendUnavailable(
                    "Postgres session backend unavailable: "
                    f"could not connect to {mask_postgres_session_dsn(self.dsn)}"
                ) from exc

    async def _get_session_with_connection(
        self,
        connection: Any,
        session_id: str,
        *,
        for_update: bool = False,
        include_events: bool = True,
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
        events = (
            await self._get_events_with_connection(connection, session_id)
            if include_events and not for_update
            else []
        )
        return self._session_from_row(row, events=events)

    async def _get_events_with_connection(
        self, connection: Any, session_id: str
    ) -> list[SessionEvent]:
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
                namespace, tenant_id, workspace_id, scope, agent_id,
                user_id, session_id, state_json, version, updated_at
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


def mask_postgres_session_dsn(dsn: str) -> str:
    if not dsn:
        return ""
    try:
        parts = urlsplit(dsn)
    except ValueError:
        return "***"
    if not parts.password:
        return dsn
    username = parts.username or ""
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    auth = f"{username}:***@" if username else "***@"
    return urlunsplit(
        (parts.scheme, f"{auth}{host}{port}", parts.path, parts.query, parts.fragment)
    )


__all__ = [
    "KSADK_PG_EVENTS_TABLE",
    "KSADK_PG_SESSIONS_TABLE",
    "KSADK_PG_STATES_TABLE",
    "PostgresSessionService",
    "create_postgres_session_service",
    "mask_postgres_session_dsn",
]
