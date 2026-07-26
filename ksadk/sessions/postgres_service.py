"""PostgreSQL shared session backend for production multi-pod runtimes."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextvars import ContextVar
from typing import Any, AsyncIterator, Optional
from urllib.parse import urlsplit, urlunsplit

from ksadk.sessions.base import (
    BaseSessionService,
    CheckpointEventQuery,
    Session,
    SessionEvent,
    SessionEventQuery,
    SessionState,
    generate_id,
)
from ksadk.sessions.errors import SessionBackendUnavailable

KSADK_PG_SESSIONS_TABLE = "ksadk_sessions"
KSADK_PG_EVENTS_TABLE = "ksadk_events"
KSADK_PG_STATES_TABLE = "ksadk_states"
PG_READABLE_EVENTS_VIEW = "ksadk_session_events_readable"

logger = logging.getLogger(__name__)


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
                        namespace, tenant_id, workspace_id, scope, agent_id, user_id, session_id, state_json, version, updated_at
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

    async def list_sessions(
        self,
        agent_id: Optional[str],
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
                WHERE namespace = $1
            """
            params: list[Any] = [self.namespace]
            if agent_id is not None:
                params.append(agent_id)
                query += f" AND agent_id = ${len(params)}"
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
                f"""
                SELECT id, agent_id, user_id, title, title_source, summary, first_prompt, last_prompt,
                       state_json, created_at, updated_at, version
                FROM {KSADK_PG_SESSIONS_TABLE}
                WHERE namespace = $1 AND id = ANY($2::text[])
                """, self.namespace, session_ids
            )
            by_id = {row["id"]: self._session_from_row(row, events=[]) for row in rows}
            return [by_id[session_id] for session_id in session_ids if session_id in by_id]

    async def get_session_metadata(self, session_id: str) -> Optional[Session]:
        sessions = await self.get_sessions_by_ids([session_id])
        return sessions[0] if sessions else None

    async def list_session_metadata(
        self, agent_id: Optional[str] = None, user_id: Optional[str] = None
    ) -> list[Session]:
        return await self.list_sessions(agent_id, user_id)

    async def query_events(self, query: SessionEventQuery) -> list[SessionEvent]:
        await self._ensure_schema()
        clauses, params = self._batch_event_where(
            query.session_ids, query.agent_id, query.after_seq_id, query.before_seq_id,
            query.event_types, query.run_id, query.checkpoint_id,
            invocation_id=query.invocation_id,
        )
        params.extend([query.limit, query.offset])
        direction = "ASC" if query.from_start else "DESC"
        inner_order = (
            f"event_row.seq_id {direction}, event_row.id {direction}"
            if query.order_by_seq
            else (
                f"event_row.timestamp {direction}, event_row.session_id {direction}, "
                f"event_row.seq_id {direction}, event_row.id {direction}"
            )
        )
        outer_order = (
            "seq_id ASC, id ASC"
            if query.order_by_seq
            else "timestamp ASC, session_id ASC, seq_id ASC, id ASC"
        )
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"""SELECT id, session_id, author, event_type, content_json, timestamp, state_delta_json, seq_id, invocation_id, metadata_json
                FROM (SELECT event_row.id, event_row.session_id, event_row.author, event_row.event_type, event_row.content_json, event_row.timestamp, event_row.state_delta_json, event_row.seq_id, event_row.invocation_id, event_row.metadata_json
                      FROM {KSADK_PG_EVENTS_TABLE} AS event_row JOIN {KSADK_PG_SESSIONS_TABLE} AS session_row ON session_row.namespace = event_row.namespace AND session_row.id = event_row.session_id
                      WHERE {' AND '.join(clauses)} ORDER BY {inner_order}
                      LIMIT ${len(params)-1} OFFSET ${len(params)}) AS page_events
                ORDER BY {outer_order}""", *params
            )
            return [self._event_from_row(row) for row in rows]

    async def count_event_query(self, query: SessionEventQuery) -> int:
        await self._ensure_schema()
        clauses, params = self._batch_event_where(
            query.session_ids, query.agent_id, query.after_seq_id, query.before_seq_id,
            query.event_types, query.run_id, query.checkpoint_id,
            invocation_id=query.invocation_id,
        )
        async with self._pool.acquire() as connection:
            return int(await connection.fetchval(
                f"SELECT COUNT(*) FROM {KSADK_PG_EVENTS_TABLE} AS event_row JOIN {KSADK_PG_SESSIONS_TABLE} AS session_row ON session_row.namespace = event_row.namespace AND session_row.id = event_row.session_id WHERE {' AND '.join(clauses)}", *params
            ) or 0)

    async def get_checkpoint_lookup_stats(
        self, session_id: str, run_id: str, checkpoint_id: str
    ) -> dict[str, object]:
        await self._ensure_schema()
        async with self._pool.acquire() as connection:
            candidate_row = await connection.fetchrow(
                f"""SELECT id, session_id, author, event_type, content_json, timestamp, state_delta_json, seq_id, invocation_id, metadata_json
                FROM {KSADK_PG_EVENTS_TABLE} WHERE namespace = $1 AND session_id = $2 AND event_type = 'run_checkpoint'
                AND metadata_json ->> 'run_id' = $3 AND metadata_json ->> 'checkpoint_id' = $4 ORDER BY seq_id DESC LIMIT 1""",
                self.namespace, session_id, run_id, checkpoint_id,
            )
            max_seq_id = await connection.fetchval(
                f"SELECT COALESCE(MAX(seq_id), 0) FROM {KSADK_PG_EVENTS_TABLE} WHERE namespace = $1 AND session_id = $2 AND event_type = 'run_checkpoint' AND metadata_json ->> 'run_id' = $3",
                self.namespace, session_id, run_id,
            )
            audit = await connection.fetchrow(
                f"SELECT COUNT(*) AS count, MAX(timestamp) AS last_at FROM {KSADK_PG_EVENTS_TABLE} WHERE namespace = $1 AND session_id = $2 AND event_type = 'run_resume' AND metadata_json ->> 'run_id' = $3 AND metadata_json ->> 'checkpoint_id' = $4",
                self.namespace, session_id, run_id, checkpoint_id,
            )
            return {"candidate": self._event_from_row(candidate_row) if candidate_row else None,
                    "max_seq_id": int(max_seq_id or 0), "resume_count": int(audit["count"] or 0),
                    "last_resumed_at": audit["last_at"]}

    async def scan_checkpoint_events(
        self, query: CheckpointEventQuery
    ) -> list[SessionEvent]:
        if query.limit < 1 or query.limit > 50:
            raise ValueError("checkpoint scan limit must be between 1 and 50")
        await self._ensure_schema()
        async with self._pool.acquire() as connection:
            return await self._scan_checkpoint_events_with_connection(connection, query)

    async def iter_checkpoint_event_chunks(
        self, query: CheckpointEventQuery
    ) -> AsyncIterator[list[SessionEvent]]:
        if query.limit < 1 or query.limit > 50:
            raise ValueError("checkpoint scan limit must be between 1 and 50")
        await self._ensure_schema()
        async with self._pool.acquire() as connection:
            async with connection.transaction(
                isolation="repeatable_read", readonly=True
            ):
                snapshot_token = self._checkpoint_snapshot_connection.set(connection)
                cursor: tuple[float, str, int, str] | None = None
                first_page = True
                try:
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
                            return
                        yield batch
                        if len(batch) < query.limit:
                            return
                        last = batch[-1]
                        cursor = (
                            last.timestamp,
                            last.session_id,
                            last.seq_id,
                            last.id,
                        )
                        first_page = False
                finally:
                    self._checkpoint_snapshot_connection.reset(snapshot_token)

    async def _scan_checkpoint_events_with_connection(
        self,
        connection: Any,
        query: CheckpointEventQuery,
        cursor: tuple[float, str, int, str] | None = None,
    ) -> list[SessionEvent]:
        clauses, params = self._batch_event_where(
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
        if cursor is not None:
            cursor_params = []
            for value in cursor:
                params.append(value)
                cursor_params.append(f"${len(params)}")
            clauses.append(
                "(event_row.timestamp, event_row.session_id, "
                "event_row.seq_id, event_row.id) > "
                f"({', '.join(cursor_params)})"
            )
        params.extend([query.limit, query.offset])
        rows = await connection.fetch(
            f"""SELECT event_row.id, event_row.session_id, event_row.author,
                event_row.event_type, event_row.content_json, event_row.timestamp,
                event_row.state_delta_json, event_row.seq_id,
                event_row.invocation_id, event_row.metadata_json
            FROM {KSADK_PG_EVENTS_TABLE} AS event_row
            JOIN {KSADK_PG_SESSIONS_TABLE} AS session_row
              ON session_row.namespace = event_row.namespace
             AND session_row.id = event_row.session_id
            WHERE {' AND '.join(clauses)}
            ORDER BY event_row.timestamp ASC, event_row.session_id ASC,
                     event_row.seq_id ASC, event_row.id ASC
            LIMIT ${len(params) - 1} OFFSET ${len(params)}""",
            *params,
        )
        return [self._event_from_row(row) for row in rows]

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
        run_keys = list(dict.fromkeys((session_id, run_id) for session_id, run_id, _ in unique_keys))
        latest_seq_ids = {key: 0 for key in run_keys}
        if not unique_keys:
            return {"audits": audits, "latest_seq_ids": latest_seq_ids}
        await self._ensure_schema()
        session_values = [key[0] for key in unique_keys]
        run_values = [key[1] for key in unique_keys]
        checkpoint_values = [key[2] for key in unique_keys]
        run_session_values = [key[0] for key in run_keys]
        latest_run_values = [key[1] for key in run_keys]
        snapshot_connection = self._checkpoint_snapshot_connection.get()
        if snapshot_connection is not None:
            return await self._get_checkpoint_stats_with_connection(
                snapshot_connection,
                audits,
                latest_seq_ids,
                session_values,
                run_values,
                checkpoint_values,
                run_session_values,
                latest_run_values,
            )
        async with self._pool.acquire() as connection:
            return await self._get_checkpoint_stats_with_connection(
                connection,
                audits,
                latest_seq_ids,
                session_values,
                run_values,
                checkpoint_values,
                run_session_values,
                latest_run_values,
            )

    async def _get_checkpoint_stats_with_connection(
        self,
        connection: Any,
        audits: dict[tuple[str, str, str], dict[str, object]],
        latest_seq_ids: dict[tuple[str, str], int],
        session_values: list[str],
        run_values: list[str],
        checkpoint_values: list[str],
        run_session_values: list[str],
        latest_run_values: list[str],
    ) -> dict[str, object]:
        audit_rows = await connection.fetch(
                f"""WITH requested(session_id, run_id, checkpoint_id) AS (
                    SELECT * FROM unnest($2::text[], $3::text[], $4::text[])
                )
                SELECT event_row.session_id,
                       event_row.metadata_json ->> 'run_id' AS run_id,
                       event_row.metadata_json ->> 'checkpoint_id' AS checkpoint_id,
                       COUNT(*) AS resume_count,
                       MAX(event_row.timestamp) AS last_resumed_at
                FROM {KSADK_PG_EVENTS_TABLE} AS event_row
                JOIN requested ON requested.session_id = event_row.session_id
                  AND requested.run_id = event_row.metadata_json ->> 'run_id'
                  AND requested.checkpoint_id = event_row.metadata_json ->> 'checkpoint_id'
                WHERE event_row.namespace = $1 AND event_row.event_type = 'run_resume'
                GROUP BY event_row.session_id, event_row.metadata_json ->> 'run_id', event_row.metadata_json ->> 'checkpoint_id'""",
            self.namespace, session_values, run_values, checkpoint_values,
        )
        latest_rows = await connection.fetch(
                f"""WITH requested(session_id, run_id) AS (
                    SELECT * FROM unnest($2::text[], $3::text[])
                )
                SELECT event_row.session_id,
                       event_row.metadata_json ->> 'run_id' AS run_id,
                       MAX(event_row.seq_id) AS latest_seq_id
                FROM {KSADK_PG_EVENTS_TABLE} AS event_row
                JOIN requested ON requested.session_id = event_row.session_id
                  AND requested.run_id = event_row.metadata_json ->> 'run_id'
                WHERE event_row.namespace = $1 AND event_row.event_type = 'run_checkpoint'
                GROUP BY event_row.session_id, event_row.metadata_json ->> 'run_id'""",
            self.namespace, run_session_values, latest_run_values,
        )
        for row in audit_rows:
            audits[(row["session_id"], row["run_id"], row["checkpoint_id"])] = {
                "resume_count": int(row["resume_count"] or 0),
                "last_resumed_at": row["last_resumed_at"],
            }
        for row in latest_rows:
            latest_seq_ids[(row["session_id"], row["run_id"])] = int(row["latest_seq_id"] or 0)
        return {"audits": audits, "latest_seq_ids": latest_seq_ids}

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
        invocation_id: str | None = None,
    ) -> tuple[list[str], list[Any]]:
        clauses = ["event_row.namespace = $1"]
        params: list[Any] = [self.namespace]
        if session_ids is not None:
            if not session_ids:
                return ["FALSE"], []
            params.append(session_ids)
            clauses.append(f"event_row.session_id = ANY(${len(params)}::text[])")
        if agent_id is not None:
            params.append(agent_id)
            clauses.append(f"session_row.agent_id = ${len(params)}")
        if after_seq_id is not None:
            params.append(after_seq_id)
            clauses.append(f"event_row.seq_id > ${len(params)}")
        if before_seq_id is not None:
            params.append(before_seq_id)
            clauses.append(f"event_row.seq_id < ${len(params)}")
        if event_types:
            params.append(event_types)
            clauses.append(f"event_row.event_type = ANY(${len(params)}::text[])")
        if invocation_id is not None:
            params.append(invocation_id)
            clauses.append(f"event_row.invocation_id = ${len(params)}")
        if run_id is not None:
            params.append(run_id)
            clauses.append(f"event_row.metadata_json ->> 'run_id' = ${len(params)}")
        if checkpoint_id is not None:
            params.append(checkpoint_id)
            clauses.append(f"event_row.metadata_json ->> 'checkpoint_id' = ${len(params)}")
        if checkpoint_ids is not None:
            if not checkpoint_ids:
                return ["FALSE"], []
            params.append(checkpoint_ids)
            clauses.append(f"event_row.metadata_json ->> 'checkpoint_id' = ANY(${len(params)}::text[])")
        if framework is not None:
            params.append(framework.lower())
            clauses.append(f"lower(event_row.metadata_json ->> 'framework') = ${len(params)}")
        return clauses, params

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
        await self._ensure_schema()
        clauses, params = self._batch_event_where(
            session_ids, agent_id, after_seq_id, before_seq_id, event_types
        )
        params.extend([limit, offset])
        direction = "ASC" if from_start else "DESC"
        limit_param, offset_param = len(params) - 1, len(params)
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT id, session_id, author, event_type, content_json, timestamp,
                       state_delta_json, seq_id, invocation_id, metadata_json
                FROM (
                    SELECT event_row.id, event_row.session_id, event_row.author, event_row.event_type,
                           event_row.content_json, event_row.timestamp, event_row.state_delta_json,
                           event_row.seq_id, event_row.invocation_id, event_row.metadata_json
                    FROM {KSADK_PG_EVENTS_TABLE} AS event_row
                    JOIN {KSADK_PG_SESSIONS_TABLE} AS session_row
                      ON session_row.namespace = event_row.namespace AND session_row.id = event_row.session_id
                    WHERE {' AND '.join(clauses)}
                    ORDER BY event_row.timestamp {direction}, event_row.session_id {direction},
                             event_row.seq_id {direction}, event_row.id {direction}
                    LIMIT ${limit_param} OFFSET ${offset_param}
                ) AS page_events
                ORDER BY timestamp ASC, session_id ASC, seq_id ASC, id ASC
                """, *params
            )
            return [self._event_from_row(row) for row in rows]

    async def count_events_batch(
        self,
        session_ids: list[str] | None = None,
        *,
        agent_id: str | None = None,
        after_seq_id: int | None = None,
        before_seq_id: int | None = None,
        event_types: list[str] | None = None,
    ) -> int:
        await self._ensure_schema()
        clauses, params = self._batch_event_where(
            session_ids, agent_id, after_seq_id, before_seq_id, event_types
        )
        async with self._pool.acquire() as connection:
            return int(await connection.fetchval(
                f"""
                SELECT COUNT(*) FROM {KSADK_PG_EVENTS_TABLE} AS event_row
                JOIN {KSADK_PG_SESSIONS_TABLE} AS session_row
                  ON session_row.namespace = event_row.namespace AND session_row.id = event_row.session_id
                WHERE {' AND '.join(clauses)}
                """, *params
            ) or 0)

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
                import asyncpg
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

                    CREATE INDEX IF NOT EXISTS idx_ksadk_pg_events_timestamp_session_seq
                    ON {KSADK_PG_EVENTS_TABLE} (namespace, timestamp, session_id, seq_id, id);

                    CREATE INDEX IF NOT EXISTS idx_ksadk_pg_events_session_invocation_seq
                    ON {KSADK_PG_EVENTS_TABLE} (namespace, session_id, invocation_id, seq_id);

                    CREATE INDEX IF NOT EXISTS idx_ksadk_pg_events_checkpoint_lookup
                    ON {KSADK_PG_EVENTS_TABLE} (namespace, session_id, event_type,
                        (metadata_json ->> 'run_id'), (metadata_json ->> 'checkpoint_id'), seq_id);

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
                try:
                    await connection.execute(
                        f"""
                    CREATE OR REPLACE VIEW {PG_READABLE_EVENTS_VIEW} AS
                    SELECT
                        event_row.namespace,
                        event_row.tenant_id,
                        event_row.workspace_id,
                        session_row.agent_id,
                        session_row.user_id,
                        session_row.title AS session_title,
                        event_row.session_id,
                        event_row.seq_id,
                        event_row.id AS event_id,
                        event_row.invocation_id,
                        event_row.author,
                        event_row.event_type,
                        CASE
                            WHEN event_row.event_type = 'user_message' THEN 'user'
                            WHEN event_row.event_type IN (
                                'assistant_message', 'reasoning', 'tool_call'
                            ) THEN 'assistant'
                            WHEN event_row.event_type = 'tool_result' THEN 'tool'
                            ELSE NULL
                        END AS message_role,
                        COALESCE(
                            NULLIF(event_row.content_json #>> '{{parts,0,text}}', ''),
                            NULLIF(event_row.content_json ->> 'text', ''),
                            NULLIF(event_row.metadata_json ->> 'reasoning', ''),
                            NULLIF(event_row.metadata_json ->> 'tool_output', '')
                        ) AS message_text,
                        event_row.metadata_json ->> 'tool_name' AS tool_name,
                        CASE
                            WHEN event_row.event_type = 'run_status' THEN COALESCE(
                                event_row.content_json ->> 'status',
                                event_row.metadata_json ->> 'status'
                            )
                            ELSE NULL
                        END AS lifecycle_status,
                        to_timestamp(event_row.timestamp) AS created_at,
                        event_row.content_json,
                        event_row.state_delta_json,
                        event_row.metadata_json
                    FROM {KSADK_PG_EVENTS_TABLE} AS event_row
                    JOIN {KSADK_PG_SESSIONS_TABLE} AS session_row
                      ON session_row.namespace = event_row.namespace
                     AND session_row.id = event_row.session_id;
                        """
                    )
                except Exception as exc:
                    logger.warning("Postgres readable session view unavailable: %s", exc)
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
    return urlunsplit((parts.scheme, f"{auth}{host}{port}", parts.path, parts.query, parts.fragment))


__all__ = [
    "KSADK_PG_EVENTS_TABLE",
    "KSADK_PG_SESSIONS_TABLE",
    "KSADK_PG_STATES_TABLE",
    "PostgresSessionService",
    "create_postgres_session_service",
    "mask_postgres_session_dsn",
]
