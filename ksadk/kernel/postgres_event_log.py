"""PostgreSQL canonical session event log and transactional append."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

from ksadk.events.session_event import (
    _timestamp_to_float,
    envelope_to_session_event,
    session_event_storage_id,
    session_event_to_envelope,
    validate_write_guard,
)
from ksadk.kernel.contracts import SessionEventEnvelope
from ksadk.kernel.errors import InvalidCommandError
from ksadk.sessions._postgres_tables import KSADK_PG_EVENTS_TABLE, KSADK_PG_SESSIONS_TABLE
from ksadk.sessions.db_budget import budgeted_pool_connection, session_db_budget


class PostgresKernelEventLog:
    """把 ``SessionEventEnvelope`` 写进共享 session log（ksadk_events）。

    ``append_on(connection, ...)`` 允许调用方把 event insert 并入一个已经
    打开的 kernel writer 事务；``append`` 则自开事务。
    """

    def __init__(
        self,
        pool: Any,
        *,
        namespace: str = "default",
        tenant_id: str = "default",
        workspace_id: str = "default",
    ) -> None:
        self._pool = pool
        self._namespace = namespace.strip() or "default"
        self._tenant_id = tenant_id.strip() or "default"
        self._workspace_id = workspace_id.strip() or "default"

    @asynccontextmanager
    async def _connection(self, *, write: bool = False):
        async with budgeted_pool_connection(
            self._pool,
            session_db_budget(),
            write=write,
        ) as connection:
            yield connection

    async def append_on(
        self,
        connection: Any,
        envelope: SessionEventEnvelope,
        guard: Any | None = None,
    ) -> SessionEventEnvelope:
        if guard is not None:
            validate_write_guard(envelope, guard)
        if not envelope.session_id.strip():
            raise ValueError("session_id must be nonempty")
        packed = envelope_to_session_event(envelope)
        storage_id = session_event_storage_id(envelope.session_id, str(envelope.event_id))
        # 锁 session 行串行化 seq 分配，与 PostgresSessionService.append_event 相同。
        session_row = await connection.fetchrow(
            f"SELECT id FROM {KSADK_PG_SESSIONS_TABLE} WHERE namespace=$1 AND id=$2 FOR UPDATE",
            self._namespace,
            envelope.session_id,
        )
        if session_row is None:
            raise InvalidCommandError(
                f"session {envelope.session_id!r} does not exist in the shared event log"
            )
        next_seq = await connection.fetchval(
            f"SELECT COALESCE(MAX(seq_id), 0) + 1 FROM {KSADK_PG_EVENTS_TABLE}"
            " WHERE namespace=$1 AND session_id=$2",
            self._namespace,
            envelope.session_id,
        )
        seq = int(next_seq or 1)
        packed.bind_seq_id(seq)  # 把物理 seq 绑定回 runtime/session envelope 内容
        await connection.execute(
            f"""
            INSERT INTO {KSADK_PG_EVENTS_TABLE} (
                namespace, tenant_id, workspace_id, id, session_id, author,
                event_type, content_json, timestamp, state_delta_json,
                seq_id, invocation_id, metadata_json
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9,
                '{{}}'::jsonb, $10, $11, $12::jsonb
            )
            ON CONFLICT (namespace, id) DO NOTHING
            """,
            self._namespace,
            self._tenant_id,
            self._workspace_id,
            storage_id,
            envelope.session_id,
            packed.author,
            packed.event_type,
            json.dumps(packed.content, ensure_ascii=False),
            _timestamp_to_float(envelope.timestamp),
            int(next_seq or 1),
            packed.invocation_id,
            json.dumps(packed.metadata, ensure_ascii=False),
        )
        stored = await self._fetch_event(connection, envelope.session_id, storage_id)
        if stored is not None:
            return stored
        raise RuntimeError("kernel event insert did not persist")  # pragma: no cover

    async def _fetch_event(
        self, connection: Any, session_id: str, storage_id: str
    ) -> SessionEventEnvelope | None:
        row = await connection.fetchrow(
            f"SELECT content_json, metadata_json, seq_id, timestamp FROM {KSADK_PG_EVENTS_TABLE}"
            " WHERE namespace=$1 AND session_id=$2 AND id=$3",
            self._namespace,
            session_id,
            storage_id,
        )
        if row is None:
            return None
        from ksadk.sessions.base import SessionEvent

        event = SessionEvent(
            id=storage_id,
            session_id=session_id,
            author="",
            event_type="",
            content=json.loads(row["content_json"]),
            timestamp=row["timestamp"],
            seq_id=row["seq_id"],
            metadata=json.loads(row["metadata_json"]),
        )
        return session_event_to_envelope(event)

    async def append(
        self, envelope: SessionEventEnvelope, *, guard: Any | None = None
    ) -> SessionEventEnvelope:
        async with self._connection(write=True) as connection:
            async with connection.transaction():
                return await self.append_on(connection, envelope, guard)

    async def get_by_id(self, session_id: str, event_id: Any) -> SessionEventEnvelope | None:
        storage_id = session_event_storage_id(session_id, str(event_id))
        async with self._connection() as connection:
            return await self._fetch_event(connection, session_id, storage_id)

    async def read(self, session_id: str, after_seq: int, limit: int) -> list[SessionEventEnvelope]:
        if limit < 1:
            raise ValueError("limit must be positive")
        async with self._connection() as connection:
            rows = await connection.fetch(
                f"SELECT id, content_json, metadata_json, seq_id, timestamp, author,"
                f" event_type, invocation_id FROM {KSADK_PG_EVENTS_TABLE}"
                " WHERE namespace=$1 AND session_id=$2 AND seq_id > $3"
                " ORDER BY seq_id LIMIT $4",
                self._namespace,
                session_id,
                int(after_seq),
                int(limit),
            )
        from ksadk.sessions.base import SessionEvent

        envelopes: list[SessionEventEnvelope] = []
        for row in rows:
            event = SessionEvent(
                id=row["id"],
                session_id=session_id,
                author=row["author"],
                event_type=row["event_type"],
                content=json.loads(row["content_json"]),
                timestamp=row["timestamp"],
                seq_id=row["seq_id"],
                invocation_id=row["invocation_id"],
                metadata=json.loads(row["metadata_json"]),
            )
            envelope = session_event_to_envelope(event)
            if envelope is not None:
                envelopes.append(envelope)
        return envelopes
