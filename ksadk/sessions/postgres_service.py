"""PostgreSQL shared session backend for production multi-pod runtimes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from ksadk.ids import new_session_id
from ksadk.sessions._checkpoint_resume_audit import CheckpointResumeAudit
from ksadk.sessions._postgres_schema import _PostgresSchemaMixin
from ksadk.sessions._postgres_tables import (
    KSADK_PG_EVENTS_TABLE,
    KSADK_PG_SESSIONS_TABLE,
    KSADK_PG_STATES_TABLE,
    pg_tool_receipts_table,
)
from ksadk.sessions._postgres_tool_receipts import _PostgresToolReceiptMixin
from ksadk.sessions.base import (
    CANONICAL_EVENT_STORAGE_CAPABILITIES,
    BaseSessionService,
    CheckpointEventQuery,
    Session,
    SessionEvent,
    SessionEventQuery,
    SessionState,
    checkpoint_creation_identity,
    checkpoint_resume_identity,
    generate_id,
)
from ksadk.sessions.db_budget import (
    DatabaseBudget,
    _cleanup_deadline,
    bounded_cleanup,
    bounded_driver_call,
    release_pool_lease,
    session_db_budget,
)
from ksadk.sessions.errors import HistoryProjectionBudgetExceeded, SessionBackendUnavailable
from ksadk.sessions.history_budget import CURRENT_HISTORY_READ_BUDGET

logger = logging.getLogger(__name__)


def _write_deadline(method):
    """Include cold schema/pool initialization in the write operation cap."""
    @wraps(method)
    async def bounded(self, *args, **kwargs):
        async with asyncio.timeout(self.db_budget.write_operation_seconds):
            return await method(self, *args, **kwargs)
    return bounded


class _TimedConnection:
    """Apply the phase's command budget to every asyncpg statement."""

    def __init__(self, connection: Any, sql_seconds: float, cleanup_seconds: float = 1.0, replay_seconds: float = 30.0, replay_sql_seconds: float | None = None) -> None:
        self._connection = connection
        self._sql_seconds = sql_seconds
        self._cleanup_seconds = cleanup_seconds
        self._replay_seconds = replay_seconds
        self._replay_sql_seconds = sql_seconds if replay_sql_seconds is None else replay_sql_seconds

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    async def execute(self, query: str, *args: Any, **kwargs: Any) -> Any:
        seconds = kwargs.pop("timeout", self._sql_seconds)
        return await bounded_driver_call(
            self._connection.execute(query, *args, timeout=seconds, **kwargs),
            self._connection, seconds, self._cleanup_seconds,
        )

    async def fetch(self, query: str, *args: Any, **kwargs: Any) -> Any:
        seconds = kwargs.pop("timeout", self._sql_seconds)
        return await bounded_driver_call(
            self._connection.fetch(query, *args, timeout=seconds, **kwargs),
            self._connection, seconds, self._cleanup_seconds,
        )

    async def fetch_replay(self, query: str, *args: Any, batch_size: int = 32) -> Any:
        """Fetch complete replay through a fixed cursor with bounded commands.

        Full context can exceed a history page's per-command SQL budget in
        aggregate. Small cursor batches have a dedicated command budget; the
        whole replay has a separate deadline and cancellation rolls back.
        """
        if not callable(getattr(self._connection, "cursor", None)):
            return await self.fetch(query, *args)
        rows = []
        async with asyncio.timeout(self._replay_seconds):
            async with self.transaction():
                async def bind_cursor():
                    return await self._connection.cursor(query, *args, timeout=self._replay_sql_seconds)
                cursor = await bounded_driver_call(
                    bind_cursor(),
                    self._connection, self._replay_sql_seconds, self._cleanup_seconds,
                )
                while True:
                    started = asyncio.get_running_loop().time()
                    batch = await bounded_driver_call(
                        cursor.fetch(batch_size, timeout=self._replay_sql_seconds),
                        self._connection, self._replay_sql_seconds, self._cleanup_seconds,
                    )
                    rows.extend(batch)
                    if len(batch) < batch_size:
                        return rows
                    # A run may contain many full graph-state snapshots.
                    # Adapt before the next fetch so a single command does
                    # not transfer dozens of those indivisible JSON bodies.
                    body_bytes = sum(
                        len(value.encode("utf-8")) if isinstance(value, str)
                        else len(value) if isinstance(value, bytes) else 16
                        for row in batch for value in row.values()
                    )
                    byte_rows = max(1, batch_size * (256 * 1024) // max(1, body_bytes))
                    elapsed = asyncio.get_running_loop().time() - started
                    time_rows = max(1, int(
                        batch_size * (self._replay_sql_seconds * 0.5) / max(0.001, elapsed)
                    ))
                    batch_size = min(64, batch_size * 2, byte_rows, time_rows)

    async def fetch_history_page(
        self, query: str, *args: Any, batch_size: int = 8,
        estimated_bytes: int | None = None,
    ) -> Any:
        """Transfer a bounded page inside the caller's read snapshot.

        Start conservatively for large tool outputs, then grow only when the
        observed transfer has command-budget headroom. Permanently fetching
        eight rows makes a modest-RTT database exhaust the *total* deadline.
        Growth is capped by both rows and observed payload bytes; slow/large
        batches shrink again. SQL limit, snapshot, command and HTTP deadlines
        remain unchanged. No failed query is retried.
        """
        # The exact candidate preflight and this read share repeatable-read.
        # A small complete body avoids cursor bind/empty-fetch round trips.
        # Do not take this path when size is unknown (custom callers/tests).
        if estimated_bytes is not None and estimated_bytes <= 256 * 1024:
            return await self.fetch(query, *args)
        if not callable(getattr(self._connection, "cursor", None)):
            return await self.fetch(query, *args)

        async def bind_cursor():
            return await self._connection.cursor(query, *args, timeout=self._sql_seconds)

        cursor = await bounded_driver_call(bind_cursor(), self._connection, self._sql_seconds,
                                          self._cleanup_seconds)
        rows = []
        while True:
            started = time.monotonic()
            batch = await bounded_driver_call(
                cursor.fetch(batch_size, timeout=self._sql_seconds),
                self._connection, self._sql_seconds, self._cleanup_seconds,
            )
            elapsed = time.monotonic() - started
            rows.extend(batch)
            if len(batch) < batch_size:
                return rows
            # asyncpg returns JSON columns as text. Estimate the received body
            # without decoding/copying whole SessionEvents. This is only a
            # transfer heuristic; the exact SQL preflight remains the hard
            # per-request byte guard.
            body_bytes = sum(
                len(value.encode('utf-8')) if isinstance(value, str)
                else len(value) if isinstance(value, bytes) else 16
                for row in batch for value in row.values()
            )
            byte_rows = max(1, batch_size * (256 * 1024) // max(1, body_bytes))
            if elapsed < self._sql_seconds / 4:
                batch_size = min(64, batch_size * 2, byte_rows)
            elif elapsed > self._sql_seconds / 2:
                batch_size = max(1, min(batch_size // 2, byte_rows))
            else:
                batch_size = min(batch_size, byte_rows)

    async def fetchrow(self, query: str, *args: Any, **kwargs: Any) -> Any:
        seconds = kwargs.pop("timeout", self._sql_seconds)
        return await bounded_driver_call(
            self._connection.fetchrow(query, *args, timeout=seconds, **kwargs),
            self._connection, seconds, self._cleanup_seconds,
        )

    async def fetchval(self, query: str, *args: Any, **kwargs: Any) -> Any:
        seconds = kwargs.pop("timeout", self._sql_seconds)
        return await bounded_driver_call(
            self._connection.fetchval(query, *args, timeout=seconds, **kwargs),
            self._connection, seconds, self._cleanup_seconds,
        )

    @asynccontextmanager
    async def transaction(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        transaction = self._connection.transaction(*args, **kwargs)
        await bounded_driver_call(transaction.__aenter__(), self._connection,
                                  self._sql_seconds, self._cleanup_seconds)
        try:
            yield transaction
        except BaseException as exc:
            try:
                await bounded_cleanup(
                    transaction.__aexit__(type(exc), exc, exc.__traceback__),
                    self._connection, self._cleanup_seconds,
                )
            except Exception as cleanup_error:
                # Preserve the operation failure while discarding an unusable
                # connection. A terminated pool proxy may already be detached.
                logger.warning("Transaction rollback failed (%s); discarding connection",
                               type(cleanup_error).__name__)
                try:
                    self._connection.terminate()
                except Exception:
                    logger.debug("Connection already unavailable during rollback", exc_info=True)
            raise
        else:
            try:
                await bounded_driver_call(transaction.__aexit__(None, None, None),
                                          self._connection, self._sql_seconds, self._cleanup_seconds)
            except TimeoutError:
                raise


class _HistoryEventWindow(list[SessionEvent]):
    """Internal short-page proof: a byte-limited page may still have more rows."""
    def __init__(self, events, *, source_exhausted: bool):
        super().__init__(events)
        self.source_exhausted = source_exhausted


def _message_projection_filter(clauses: list[str], prefix: str = "") -> None:
    from ksadk.sessions.history_budget import CURRENT_MESSAGE_PROJECTION

    if not CURRENT_MESSAGE_PROJECTION.get():
        return
    event = f"{prefix}content_json->'runtime_event'"
    # Only the opaque LangGraph state channel is omitted. Protocol-bearing
    # data (including A2UI) and all readable/tool/control facts remain visible.
    clauses.append("NOT ("
        f"COALESCE(({event})->>'item_kind','')='data' AND "
        f"COALESCE(({event})->'source'->>'framework','')='langgraph' AND "
        f"COALESCE(({event})->'source'->'metadata'->>'channel','')='values' AND "
        f"COALESCE(({event})->'source'->>'protocol','')='' AND "
        f"COALESCE(({event})->'source'->'metadata'->>'protocol','')='')")


class PostgresSessionService(_PostgresToolReceiptMixin, _PostgresSchemaMixin, BaseSessionService):
    # Its get/list/query/count operations never execute Agent or tool effects.
    _retry_safe_reads = True
    storage_capabilities = CANONICAL_EVENT_STORAGE_CAPABILITIES

    @staticmethod
    async def _precheck_history_body(
        connection: _TimedConnection, candidate_sql: str, params: list[Any],
    ) -> int | None:
        budget = CURRENT_HISTORY_READ_BUDGET.get()
        if budget is None:
            return
        sizes = await connection.fetchrow(
            "SELECT COALESCE(SUM(COALESCE(octet_length(content_json::text),0) + "
            "COALESCE(octet_length(state_delta_json::text),0) + "
            "COALESCE(octet_length(metadata_json::text),0)), 0) AS body_bytes, "
            "COUNT(*) AS event_count "
            f"FROM ({candidate_sql}) AS history_candidates",
            *params,
        )
        size = int(sizes['body_bytes'])
        # Preserve the conservative per-event allowance for the hard budget;
        # it is not JSON payload and must not inflate the transfer estimate.
        budget.account(size + 1024 * int(sizes['event_count']))
        return size

    def __init__(
        self,
        *,
        dsn: str,
        namespace: str = "default",
        tenant_id: str = "default",
        workspace_id: str = "default",
        min_size: int = 1,
        max_size: int = 10,
        connect_timeout: float | None = None,
        db_budget: DatabaseBudget | None = None,
    ):
        if not dsn.strip():
            raise ValueError("KSADK_SESSION_DSN is required when KSADK_SESSION_BACKEND=postgres")
        self.dsn = dsn.strip()
        self.namespace = namespace.strip() or "default"
        self.tenant_id = tenant_id.strip() or "default"
        self.workspace_id = workspace_id.strip() or "default"
        self.min_size = min_size
        self.max_size = max_size
        self.db_budget = db_budget or session_db_budget()
        self.connect_timeout = connect_timeout or self.db_budget.connect_seconds
        self._pool: Any = None
        self._pool_lock = asyncio.Lock()
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()
        self._checkpoint_snapshot_connection: ContextVar[Any | None] = ContextVar(
            f"checkpoint_snapshot_connection_{id(self)}", default=None
        )
        self._history_read_lease: ContextVar[Any | None] = ContextVar(
            f"history_read_lease_{id(self)}", default=None
        )
        try:
            transfers = max(1, min(32, int(os.getenv("KSADK_HISTORY_READ_CONCURRENCY", "4"))))
        except ValueError:
            transfers = 4
        self._history_transfer_slots = asyncio.Semaphore(transfers)

    @asynccontextmanager
    async def history_read_scope(self) -> AsyncIterator[None]:
        """Reuse one lease for a bounded, serial message projection.

        This does not start a transaction or cache authorization. The caller
        keeps its seq snapshot and operation deadline. Child tasks and writes
        must acquire their own connection; asyncpg cannot multiplex a lease.
        """
        await self._ensure_schema()
        async with self._connection() as connection:
            token = self._history_read_lease.set((asyncio.current_task(), connection))
            try:
                yield
            finally:
                self._history_read_lease.reset(token)

    @asynccontextmanager
    async def _connection(self, *, write: bool = False) -> AsyncIterator[_TimedConnection]:
        shared = self._history_read_lease.get()
        if not write and shared is not None and shared[0] is asyncio.current_task():
            import asyncpg
            connection = shared[1]
            try:
                closed = connection.is_closed()
            except asyncpg.InterfaceError:
                closed = True  # Termination can detach the pool proxy first.
            if not closed:
                yield connection
                return
            # A terminated transport may be retried by the existing read-only
            # recovery path. Never hand that path the same dead connection.
            # Keep the scope marker so replacement reads still use its gate.
        budget = self.db_budget
        acquire = budget.write_acquire_seconds if write else budget.read_acquire_seconds
        sql = budget.write_sql_seconds if write else budget.read_sql_seconds
        lease = None
        connection = None
        token = _cleanup_deadline.set(None)
        try:
            if write:
                async with asyncio.timeout(budget.write_operation_seconds):
                    lease = self._pool.acquire(timeout=acquire)
                    connection = await asyncio.wait_for(lease.__aenter__(), timeout=acquire)
                    yield _TimedConnection(connection, sql, budget.cleanup_seconds, budget.replay_operation_seconds, budget.replay_sql_seconds)
            else:
                lease = self._pool.acquire(timeout=acquire)
                connection = await asyncio.wait_for(lease.__aenter__(), timeout=acquire)
                yield _TimedConnection(connection, sql, budget.cleanup_seconds, budget.replay_operation_seconds, budget.replay_sql_seconds)
        finally:
            if lease is not None and connection is not None:
                try:
                    await release_pool_lease(self._pool, lease, connection,
                                             budget.cleanup_seconds)
                finally:
                    _cleanup_deadline.reset(token)
            else:
                _cleanup_deadline.reset(token)

    async def _set_lock_timeout(self, connection: _TimedConnection) -> None:
        milliseconds = max(1, int(self.db_budget.lock_seconds * 1000))
        await connection.fetchval(
            "SELECT set_config('lock_timeout', $1, true)", f"{milliseconds}ms"
        )

    @_write_deadline
    async def create_session(
        self,
        agent_id: str,
        user_id: str,
        session_id: Optional[str] = None,
    ) -> Session:
        await self._ensure_schema()
        session_key = session_id or new_session_id()
        async with self._connection(write=True) as connection:
            async with connection.transaction():
                await self._set_lock_timeout(connection)
                existing = await self._get_session_with_connection(
                    connection, session_key, include_events=False
                )
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
        async with self._connection() as connection:
            return await self._get_session_with_connection(connection, session_id)

    async def get_adk_session_snapshot(
        self, session_id: str, expected_session: Session | None = None
    ) -> Optional[Session]:
        """Read ownership, state, and the complete transcript from one snapshot."""
        await self._ensure_schema()
        async with self._connection() as connection:
            async with connection.transaction(isolation="repeatable_read", readonly=True):
                session = await self._get_session_with_connection(connection, session_id)
                if expected_session is not None and (
                    session is None or session.agent_id != expected_session.agent_id
                    or session.user_id != expected_session.user_id
                    or session.created_at != expected_session.created_at
                ):
                    from ksadk.sessions.errors import HistoryReloadRequired

                    raise HistoryReloadRequired("Session changed during legacy read")
                return session

    async def get_session_metadata(self, session_id: str) -> Optional[Session]:
        await self._ensure_schema()
        async with self._connection() as connection:
            return await self._get_session_with_connection(
                connection,
                session_id,
                include_events=False,
            )

    async def get_session_header(self, session_id: str) -> Optional[Session]:
        """History authorization/epoch fields without reading the state blob."""
        await self._ensure_schema()
        async with self._connection() as connection:
            row = await connection.fetchrow(
                f"SELECT id,agent_id,user_id,title,title_source,summary,first_prompt,"
                f"last_prompt,created_at,updated_at,version FROM {KSADK_PG_SESSIONS_TABLE} "
                "WHERE namespace=$1 AND id=$2",
                self.namespace, session_id,
            )
        if row is None:
            return None
        return self._header_from_row(row)

    @staticmethod
    def _header_from_row(row: Any) -> Session:
        return Session(
            id=row["id"], agent_id=row["agent_id"], user_id=row["user_id"],
            title=row["title"], title_source=row["title_source"],
            summary=row["summary"], first_prompt=row["first_prompt"],
            last_prompt=row["last_prompt"], created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]), version=int(row["version"] or 0),
        )

    async def get_session_header_with_watermark(self, session_id: str) -> tuple[Session | None, int]:
        """One statement snapshot for history authorization and its high watermark."""
        if type(self).get_session_header is not PostgresSessionService.get_session_header:
            # A custom backend may add ownership policy in its header loader.
            header = await self.get_session_header(session_id)
            return header, await self.get_max_seq_id(session_id) if header is not None else 0
        await self._ensure_schema()
        async with self._connection() as connection:
            row = await connection.fetchrow(
                f"SELECT s.id,s.agent_id,s.user_id,s.title,s.title_source,s.summary,"
                "s.first_prompt,s.last_prompt,s.created_at,s.updated_at,s.version,"
                f"(SELECT MAX(e.seq_id) FROM {KSADK_PG_EVENTS_TABLE} e "
                "WHERE e.namespace=s.namespace AND e.session_id=s.id) AS newest_seq "
                f"FROM {KSADK_PG_SESSIONS_TABLE} s WHERE s.namespace=$1 AND s.id=$2",
                self.namespace, session_id,
            )
        return (self._header_from_row(row), int(row['newest_seq'] or 0)) if row else (None, 0)

    @_write_deadline
    async def ensure_session_header(
        self, agent_id: str, user_id: str, session_id: str
    ) -> Session:
        """Create a canonical session without reading state or event bodies on races."""
        await self._ensure_schema()
        now = time.time()
        async with self._connection(write=True) as connection:
            async with connection.transaction():
                await self._set_lock_timeout(connection)
                inserted = await connection.fetchval(
                    f"INSERT INTO {KSADK_PG_SESSIONS_TABLE} ("
                    "namespace,tenant_id,workspace_id,id,agent_id,user_id,title,"
                    "title_source,summary,first_prompt,last_prompt,state_json,"
                    "created_at,updated_at,version) VALUES "
                    "($1,$2,$3,$4,$5,$6,'','','','','','{}'::jsonb,$7,$7,0) "
                    "ON CONFLICT (namespace,id) DO NOTHING RETURNING id",
                    self.namespace, self.tenant_id, self.workspace_id,
                    session_id, agent_id, user_id, now,
                )
                row = await connection.fetchrow(
                    f"SELECT id,agent_id,user_id,title,title_source,summary,"
                    f"first_prompt,last_prompt,created_at,updated_at,version "
                    f"FROM {KSADK_PG_SESSIONS_TABLE} WHERE namespace=$1 AND id=$2",
                    self.namespace, session_id,
                )
                if row is None:
                    raise RuntimeError("Canonical AG-UI session creation was not visible")
                if inserted is not None:
                    await connection.execute(
                        f"INSERT INTO {KSADK_PG_STATES_TABLE} ("
                        "namespace,tenant_id,workspace_id,scope,agent_id,user_id,"
                        "session_id,state_json,version,updated_at) VALUES "
                        "($1,$2,$3,'session',$4,$5,$6,'{}'::jsonb,0,$7) "
                        "ON CONFLICT (namespace,scope,agent_id,user_id,session_id) DO NOTHING",
                        self.namespace, self.tenant_id, self.workspace_id,
                        row["agent_id"], row["user_id"], session_id, now,
                    )
        return Session(
            id=row["id"], agent_id=row["agent_id"], user_id=row["user_id"],
            title=row["title"], title_source=row["title_source"],
            summary=row["summary"], first_prompt=row["first_prompt"],
            last_prompt=row["last_prompt"], created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]), version=int(row["version"] or 0),
        )

    async def list_sessions(
        self,
        agent_id: str,
        user_id: Optional[str] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[Session]:
        await self._ensure_schema()
        async with self._connection() as connection:
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
        async with self._connection() as connection:
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

    @_write_deadline
    async def delete_session(self, session_id: str) -> bool:
        await self._ensure_schema()
        async with self._connection(write=True) as connection:
            async with connection.transaction():
                await self._set_lock_timeout(connection)
                if not await connection.fetchval(
                    f"SELECT 1 FROM {KSADK_PG_SESSIONS_TABLE} "
                    "WHERE namespace=$1 AND id=$2 FOR UPDATE",
                    self.namespace, session_id,
                ):
                    return False
                if await connection.fetchval(
                    f"SELECT 1 FROM {pg_tool_receipts_table} "
                    "WHERE namespace=$1 AND session_id=$2 AND state='unknown' LIMIT 1",
                    self.namespace,
                    session_id,
                ):
                    raise ValueError("session has durable tool receipt claims")
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

    @_write_deadline
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
        async with self._connection(write=True) as connection:
            async with connection.transaction():
                await self._set_lock_timeout(connection)
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

    @_write_deadline
    async def append_event(self, session_id: str, event: SessionEvent) -> SessionEvent:
        return await self._append_event_with_expected(session_id, event, None)

    @_write_deadline
    async def append_event_if_session_matches(
        self, session_id: str, event: SessionEvent, expected_session: Session
    ) -> SessionEvent:
        return await self._append_event_with_expected(session_id, event, expected_session)

    async def _append_event_with_expected(
        self, session_id: str, event: SessionEvent, expected_session: Session | None
    ) -> SessionEvent:
        await self._ensure_schema()
        # No-delta events still lock the session row to allocate a fenced
        # sequence, but do not fetch/decode a potentially large state value.
        state_column = ", state_json" if event.state_delta else ""
        async with self._connection(write=True) as connection:
            async with connection.transaction():
                await self._set_lock_timeout(connection)
                session_row = await connection.fetchrow(
                    f"""
                    SELECT agent_id, user_id, created_at, version{state_column}
                    FROM {KSADK_PG_SESSIONS_TABLE}
                    WHERE namespace = $1 AND id = $2
                    FOR UPDATE
                    """,
                    self.namespace,
                    session_id,
                )
                if session_row is None:
                    raise ValueError(f"Session {session_id} not found")
                if expected_session is not None and (
                    session_row["agent_id"] != expected_session.agent_id
                    or session_row["user_id"] != expected_session.user_id
                    or float(session_row["created_at"]) != expected_session.created_at
                ):
                    from ksadk.sessions.errors import HistoryReloadRequired

                    raise HistoryReloadRequired("Session changed during legacy import")

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
                if stored.state_delta:
                    state = self._json_to_dict(session_row["state_json"])
                    version = int(session_row["version"] or 0)
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
                else:
                    await connection.execute(
                        f"UPDATE {KSADK_PG_SESSIONS_TABLE} SET updated_at=$1 "
                        "WHERE namespace=$2 AND id=$3",
                        updated_at, self.namespace, session_id,
                    )
                return stored

    async def get_event_by_id(self, session_id: str, event_id: str) -> Optional[SessionEvent]:
        return await self._get_event_by_id(session_id, event_id, write=False)

    async def get_event_by_id_for_write(self, session_id: str, event_id: str) -> Optional[SessionEvent]:
        """Canonical idempotency/commit lookup is part of a storage write."""
        return await self._get_event_by_id(session_id, event_id, write=True)

    async def _get_event_by_id(self, session_id: str, event_id: str, *, write: bool) -> Optional[SessionEvent]:
        await self._ensure_schema()
        async with self._connection(write=write) as connection:
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

    async def get_event_by_id_if_session_matches(
        self, session_id: str, event_id: str, expected_session: Session
    ) -> Optional[SessionEvent]:
        """Confirm one stable event ID under the authorized session incarnation."""
        await self._ensure_schema()
        async with self._connection() as connection:
            async with connection.transaction(isolation="repeatable_read", readonly=True):
                owner = await connection.fetchrow(
                    f"SELECT agent_id,user_id,created_at FROM {KSADK_PG_SESSIONS_TABLE} "
                    "WHERE namespace=$1 AND id=$2",
                    self.namespace, session_id,
                )
                if (owner is None or owner["agent_id"] != expected_session.agent_id
                        or owner["user_id"] != expected_session.user_id
                        or float(owner["created_at"]) != expected_session.created_at):
                    from ksadk.sessions.errors import HistoryReloadRequired

                    raise HistoryReloadRequired("Session changed during legacy import")
                row = await connection.fetchrow(
                    f"SELECT id,session_id,author,event_type,content_json,timestamp,"
                    f"state_delta_json,seq_id,invocation_id,metadata_json "
                    f"FROM {KSADK_PG_EVENTS_TABLE} "
                    "WHERE namespace=$1 AND session_id=$2 AND id=$3",
                    self.namespace, session_id, event_id,
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
        _message_projection_filter(conditions)
        params: list[Any] = [self.namespace, session_id, invocation_id]
        if after_seq_id is not None:
            params.append(after_seq_id)
            conditions.append(f"seq_id > ${len(params)}")
        if before_seq_id is not None:
            params.append(before_seq_id)
            conditions.append(f"seq_id < ${len(params)}")
        async with self._connection() as connection:
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
        _message_projection_filter(conditions)
        params: list[Any] = [self.namespace, session_id]
        if after_seq_id is not None:
            params.append(after_seq_id)
            conditions.append(f"seq_id > ${len(params)}")
        if before_seq_id is not None:
            params.append(before_seq_id)
            conditions.append(f"seq_id < ${len(params)}")
        where_clause = " AND ".join(conditions)
        async with self._connection() as connection:
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
            bounded = CURRENT_HISTORY_READ_BUDGET.get() is not None
            async with (connection.transaction(isolation="repeatable_read", readonly=True)
                        if bounded else nullcontext()):
                body_bytes = None
                if bounded:
                    candidate = (
                        f"SELECT content_json,state_delta_json,metadata_json "
                        f"FROM {KSADK_PG_EVENTS_TABLE} WHERE {where_clause} "
                        "ORDER BY seq_id DESC "
                    )
                    if limit is not None:
                        candidate += f"LIMIT ${limit_param} OFFSET ${offset_param}"
                    elif offset is not None:
                        candidate += f"OFFSET ${offset_param}"
                    body_bytes = await self._precheck_history_body(connection, candidate, params)
                if bounded:
                    rows = await connection.fetch_history_page(query, *params, estimated_bytes=body_bytes)
                else:
                    fetch = connection.fetch_replay if limit is None else connection.fetch
                    rows = await fetch(query, *params)
            return [self._event_from_row(row) for row in rows]

    async def get_message_event_window(
        self, session_id: str, *, limit: int, from_start: bool = False,
        after_seq_id: int | None = None, before_seq_id: int | None = None,
        byte_limit: int = 64 * 1024,
    ) -> list[SessionEvent]:
        """Initial message projection window, bounded by rows AND JSON bytes.

        This is deliberately separate from get_events/query_events: those APIs
        must retain their exact row-limit semantics. The caller completes the
        boundary invocation before returning any messages to the client.
        """
        if not 1 <= limit <= 256:
            raise ValueError('Message projection window must be between 1 and 256 events')
        if not 1 <= byte_limit <= 512 * 1024:
            raise ValueError('Message projection window must be between 1 and 512 KiB')
        if from_start and type(self).query_events is not PostgresSessionService.query_events:
            return await self.query_events(SessionEventQuery(
                session_ids=[session_id], from_start=True, order_by_seq=True,
                limit=limit, after_seq_id=after_seq_id, before_seq_id=before_seq_id,
            ))
        if not from_start and type(self).get_events is not PostgresSessionService.get_events:
            return await self.get_events(
                session_id, limit=limit, after_seq_id=after_seq_id, before_seq_id=before_seq_id,
            )
        await self._ensure_schema()
        conditions = ['namespace=$1', 'session_id=$2']
        _message_projection_filter(conditions)
        params: list[Any] = [self.namespace, session_id]
        for value, operator in ((after_seq_id, '>'), (before_seq_id, '<')):
            if value is not None:
                params.append(value)
                conditions.append(f'seq_id {operator} ${len(params)}')
        params.append(limit)
        direction = 'ASC' if from_start else 'DESC'
        fields = ('id,session_id,author,event_type,content_json,timestamp,'
                  'state_delta_json,seq_id,invocation_id,metadata_json')
        budget = CURRENT_HISTORY_READ_BUDGET.get()
        limit_param = len(params)
        params.append(budget.remaining if budget is not None else 2**63 - 1)
        byte_param = len(params)
        # A single statement gives the byte check and returned rows the same
        # snapshot. PostgreSQL withholds all JSON bodies on overflow; only
        # bounded row metadata and the required byte count cross the socket.
        # This avoids BEGIN/preflight/fetch/COMMIT round trips on every page.
        returned_fields = ','.join(
            f'CASE WHEN required_bytes <= ${byte_param}::bigint THEN {field} ELSE NULL END AS {field}'
            if field in {'content_json', 'state_delta_json', 'metadata_json'} else field
            for field in fields.split(',')
        )
        # Include variable-length identity columns and a conservative allowance
        # for numeric values, DataRow framing and the returned guard columns.
        # Crossing a byte ceiling with one additional large tool event nearly
        # doubles a transfer and synchronizes slow responses under load. Only
        # the first event may exceed the window, so an indivisible event still
        # makes progress (subject to the unchanged total request byte budget).
        wire_overhead = ' + '.join(
            f'COALESCE(octet_length({field}::text),0)'
            for field in ('id', 'session_id', 'author', 'event_type', 'invocation_id')
        ) + ' + 128'
        query = f'''
            WITH bounded AS (
                SELECT {fields},
                    COALESCE(octet_length(content_json::text),0) +
                    COALESCE(octet_length(state_delta_json::text),0) +
                    COALESCE(octet_length(metadata_json::text),0) AS body_bytes,
                    {wire_overhead} AS wire_overhead
                FROM {KSADK_PG_EVENTS_TABLE}
                WHERE {' AND '.join(conditions)}
                ORDER BY seq_id {direction} LIMIT ${limit_param}
            ), measured AS (
                SELECT *, ROW_NUMBER() OVER (ORDER BY seq_id {direction}) AS position,
                    SUM(body_bytes + wire_overhead) OVER (
                    ORDER BY seq_id {direction} ROWS UNBOUNDED PRECEDING
                ) AS cumulative_bytes FROM bounded
            ), candidates AS (
                SELECT * FROM measured
                WHERE cumulative_bytes <= {byte_limit} OR position = 1
            ), checked AS (
                SELECT *, SUM(body_bytes + 1024) OVER () AS required_bytes,
                    ((SELECT COUNT(*) FROM bounded) < {limit}
                     AND COUNT(*) OVER () = (SELECT COUNT(*) FROM bounded)) AS source_exhausted
                FROM candidates
            )
            SELECT {returned_fields}, source_exhausted, required_bytes FROM checked
            ORDER BY seq_id ASC
        '''
        # Small individual windows still create a large burst when many
        # projections fetch them simultaneously. Only the bounded non-Kernel
        # projection scope shares this gate; metadata, writes, replay and
        # ordinary/Kernel reads retain their existing concurrency. Waiting is
        # inside the projection's original deadline and never retries a query.
        shared = self._history_read_lease.get()
        gate = (self._history_transfer_slots
                if shared is not None and shared[0] is asyncio.current_task()
                else nullcontext())
        async with gate:
            async with self._connection() as connection:
                rows = await connection.fetch(query, *params)
        if budget is not None and rows:
            budget.account(int(rows[0]['required_bytes']))
        return _HistoryEventWindow(
            [self._event_from_row(row) for row in rows],
            source_exhausted=bool(rows[0]['source_exhausted']) if rows else True,
        )

    async def get_session_summary_events(self, session_id: str) -> list[SessionEvent]:
        """Fetch prompt endpoints and the exact active/latest run summary.

        PostgreSQL ranks lifecycle facts by invocation without sending their JSON
        bodies to Python. A terminal run cannot hide an older active run after
        more than one page of lifecycle events.
        """
        queries = [
            SessionEventQuery(session_ids=[session_id], event_types=["user_message"],
                              limit=1, from_start=True, order_by_seq=True),
            SessionEventQuery(session_ids=[session_id], event_types=["user_message"],
                              limit=1, order_by_seq=True),
        ]
        selected: dict[str, SessionEvent] = {}
        for query in queries:
            selected.update({event.id: event for event in await self.query_events(query)})
        run_id = "COALESCE(NULLIF(metadata_json->>'run_id',''), NULLIF(metadata_json->>'invocation_id',''), NULLIF(invocation_id,''), '')"
        status = (
            "COALESCE(NULLIF(metadata_json->>'status',''), "
            "NULLIF(metadata_json->>'run_status',''), NULLIF(content_json->>'status',''), "
            "NULLIF(content_json->'payload'->>'status',''), "
            "CASE event_type WHEN 'run.started' THEN 'in_progress' "
            "WHEN 'run.progress' THEN 'in_progress' "
            "WHEN 'run.interrupted' THEN 'interrupted' "
            "WHEN 'run.completed' THEN 'completed' "
            "WHEN 'run.failed' THEN 'failed' "
            "WHEN 'run.canceled' THEN 'cancelled' ELSE '' END)"
        )
        await self._ensure_schema()
        async with self._connection() as connection:
            row = await connection.fetchrow(
                f"""WITH facts AS (
                    SELECT id, seq_id, timestamp, event_type, {run_id} AS run_id,
                           {status} AS status,
                           COALESCE(metadata_json->>'run_mode','unknown') AS run_mode,
                           COALESCE(metadata_json->>'run_trigger','unknown') AS run_trigger
                    FROM {KSADK_PG_EVENTS_TABLE}
                    WHERE namespace=$1 AND session_id=$2 AND event_type=ANY($3::text[])
                ), latest AS (
                    SELECT DISTINCT ON (run_id) * FROM facts
                    ORDER BY run_id, seq_id DESC
                ), chosen AS (
                    SELECT * FROM latest
                    ORDER BY CASE WHEN status=ANY($4::text[]) THEN 0
                                  WHEN status<>ALL($5::text[]) THEN 1 ELSE 2 END,
                             seq_id DESC LIMIT 1
                )
                SELECT chosen.*, classified.run_mode AS classified_mode,
                       classified.run_trigger AS classified_trigger
                FROM chosen LEFT JOIN LATERAL (
                    SELECT facts.run_mode, facts.run_trigger FROM facts
                    WHERE facts.run_id=chosen.run_id AND
                          (facts.run_mode<>'unknown' OR facts.run_trigger<>'unknown')
                    ORDER BY facts.seq_id DESC LIMIT 1
                ) classified ON true""",
                self.namespace, session_id,
                ["run_status", "run.started", "run.progress", "run.interrupted",
                 "run.completed", "run.failed", "run.canceled"],
                ["in_progress", "running", "resuming", "starting"],
                ["completed", "failed", "error", "cancelled", "canceled", "aborted",
                 "interrupted", "resume_failed"],
            )
        if row is not None:
            selected[row["id"]] = SessionEvent(
                id=row["id"], session_id=session_id, event_type=row["event_type"],
                seq_id=int(row["seq_id"]), timestamp=float(row["timestamp"]),
                invocation_id=row["run_id"], content={"status": row["status"]},
                metadata={"run_id": row["run_id"], "status": row["status"],
                          "run_mode": row["classified_mode"] or row["run_mode"],
                          "run_trigger": row["classified_trigger"] or row["run_trigger"]},
            )
        return sorted(selected.values(), key=lambda event: event.seq_id)

    async def get_max_seq_id(self, session_id: str) -> int:
        """Find a history snapshot boundary without fetching event JSON."""
        await self._ensure_schema()
        async with self._connection() as connection:
            return int(await connection.fetchval(
                f"SELECT COALESCE(MAX(seq_id),0) FROM {KSADK_PG_EVENTS_TABLE} "
                "WHERE namespace=$1 AND session_id=$2",
                self.namespace, session_id,
            ) or 0)

    async def has_events(
        self, session_id: str, *, after_seq_id: int | None = None,
        before_seq_id: int | None = None,
    ) -> bool:
        """Probe a page boundary without transferring a potentially huge body."""
        await self._ensure_schema()
        clauses = ["namespace=$1", "session_id=$2"]
        _message_projection_filter(clauses)
        params: list[Any] = [self.namespace, session_id]
        if after_seq_id is not None:
            params.append(after_seq_id)
            clauses.append(f"seq_id>${len(params)}")
        if before_seq_id is not None:
            params.append(before_seq_id)
            clauses.append(f"seq_id<${len(params)}")
        async with self._connection() as connection:
            return bool(await connection.fetchval(
                f"SELECT EXISTS(SELECT 1 FROM {KSADK_PG_EVENTS_TABLE} "
                f"WHERE {' AND '.join(clauses)})", *params,
            ))

    async def count_events(
        self,
        session_id: str,
        after_seq_id: Optional[int] = None,
        before_seq_id: Optional[int] = None,
    ) -> int:
        await self._ensure_schema()
        conditions = ["namespace = $1", "session_id = $2"]
        _message_projection_filter(conditions)
        params: list[Any] = [self.namespace, session_id]
        if after_seq_id is not None:
            params.append(after_seq_id)
            conditions.append(f"seq_id > ${len(params)}")
        if before_seq_id is not None:
            params.append(before_seq_id)
            conditions.append(f"seq_id < ${len(params)}")
        where_clause = " AND ".join(conditions)
        async with self._connection() as connection:
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
        async with self._connection() as connection:
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

    async def get_session_ids_by_ids(
        self, session_ids: list[str], agent_id: str | None = None,
    ) -> list[str]:
        if not session_ids:
            return []
        await self._ensure_schema()
        async with self._connection() as connection:
            rows = await connection.fetch(
                f"SELECT id FROM {KSADK_PG_SESSIONS_TABLE} WHERE namespace=$1 "
                "AND id=ANY($2::text[])"
                + (" AND agent_id=$3" if agent_id is not None else ""),
                self.namespace, session_ids, *([agent_id] if agent_id is not None else []),
            )
        found = {row["id"] for row in rows}
        return [sid for sid in session_ids if sid in found]

    async def list_session_ids(
        self, agent_id: str | None = None, user_id: str | None = None,
    ) -> list[str]:
        await self._ensure_schema()
        clauses = ["namespace=$1"]
        params: list[Any] = [self.namespace]
        for column, value in (("agent_id", agent_id), ("user_id", user_id)):
            if value is not None:
                params.append(value)
                clauses.append(f"{column}=${len(params)}")
        async with self._connection() as connection:
            rows = await connection.fetch(
                f"SELECT id FROM {KSADK_PG_SESSIONS_TABLE} WHERE {' AND '.join(clauses)}",
                *params,
            )
        return [row["id"] for row in rows]

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
        async with self._connection() as connection:
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
        _message_projection_filter(clauses, "event_row.")
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
                f" OR event_row.metadata_json->'tool_receipt'->>'run_id'=${len(params)}"
                " OR (event_row.event_type='continuation.created'"
                f" AND event_row.content_json->'runtime_event'->>'run_id'=${len(params)}))"
            )
        if query.checkpoint_id is not None:
            params.append(query.checkpoint_id)
            clauses.append(
                "(event_row.metadata_json->>'checkpoint_id'="
                f"${len(params)}"
                f" OR event_row.metadata_json->'tool_receipt'->>'checkpoint_id'=${len(params)}"
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
        if query.receipt_only:
            clauses.append(
                "event_row.event_type='tool_result' AND "
                "jsonb_typeof(event_row.metadata_json->'tool_receipt')='object' AND "
                "btrim(coalesce(nullif(event_row.metadata_json->'tool_receipt'->>'tool_name', ''), "
                "event_row.metadata_json->>'tool_name', ''))<>''"
            )
        return clauses, params

    async def query_events(self, query: SessionEventQuery) -> list[SessionEvent]:
        return await self._query_events(query)

    async def get_raw_event_page(
        self, query: SessionEventQuery, *, byte_limit: int = 512 * 1024,
    ) -> list[SessionEvent]:
        """HTTP-only byte paging; ordinary query/replay row contracts stay exact."""
        if type(self).query_events is not PostgresSessionService.query_events:
            return await self.query_events(query)
        return await self._query_events(query, byte_limit=byte_limit)

    async def _query_events(
        self, query: SessionEventQuery, *, byte_limit: int | None = None,
    ) -> list[SessionEvent]:
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
        statement = (
            f"SELECT event_row.id,event_row.session_id,event_row.author,event_row.event_type,"
            f"event_row.content_json,event_row.timestamp,event_row.state_delta_json,"
            f"event_row.seq_id,event_row.invocation_id,event_row.metadata_json "
            f"FROM {KSADK_PG_EVENTS_TABLE} event_row "
            f"JOIN {KSADK_PG_SESSIONS_TABLE} session_row "
            "ON session_row.namespace=event_row.namespace "
            "AND session_row.id=event_row.session_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY {order} "
            f"LIMIT ${len(params)-1} OFFSET ${len(params)}"
        )
        if byte_limit is not None:
            if byte_limit <= 0:
                raise ValueError("Raw event page byte limit must be positive")
            page_order = order.replace("event_row.", "")
            params.append(byte_limit)
            statement = (
                # Plain CTEs also support PostgreSQL 9.6, still used by Kernel
                # deployments. LIMIT plus the window frame preserves the page
                # boundary on newer servers that may inline this CTE.
                f"WITH candidates AS ({statement}), sized AS ("
                f"SELECT *,row_number() OVER (ORDER BY {page_order}) AS page_rank,"
                "SUM(COALESCE(octet_length(content_json::text),0)+"
                "COALESCE(octet_length(state_delta_json::text),0)+"
                "COALESCE(octet_length(metadata_json::text),0)+1024) "
                f"OVER (ORDER BY {page_order} ROWS UNBOUNDED PRECEDING) AS page_bytes "
                "FROM candidates) SELECT id,session_id,author,event_type,content_json,"
                "timestamp,state_delta_json,seq_id,invocation_id,metadata_json FROM sized "
                f"WHERE page_rank=1 OR page_bytes<=${len(params)} ORDER BY {page_order}"
            )
        async with self._connection() as connection:
            bounded = CURRENT_HISTORY_READ_BUDGET.get() is not None
            async with (connection.transaction(isolation="repeatable_read", readonly=True)
                        if bounded else nullcontext()):
                body_bytes = await self._precheck_history_body(
                    connection,
                    statement,
                    params,
                )
                fetch = connection.fetch_history_page if bounded else connection.fetch
                rows = await fetch(
                    statement,
                    *params,
                    **({'estimated_bytes': body_bytes} if bounded else {}),
                )
        events = [self._event_from_row(row) for row in rows]
        if not query.from_start:
            events.reverse()
        return events

    async def count_event_query(self, query: SessionEventQuery) -> int:
        await self._ensure_schema()
        clauses, params = self._batch_event_where(query)
        async with self._connection() as connection:
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

    async def iter_resume_related_event_pages(
        self, session_id: str, *, page_size: int = 256,
        expected_session: Session | None = None,
    ) -> AsyncIterator[list[SessionEvent]]:
        """Scan only resume facts by sequence in one read-only snapshot."""
        await self._ensure_schema()
        kinds = ["run_checkpoint", "run_resume", "runtime_resume", "continuation.created", "continuation.resumed"]
        async with self._connection() as connection:
            async with connection.transaction(isolation="repeatable_read", readonly=True):
                if expected_session is not None:
                    from ksadk.sessions.errors import HistoryReloadRequired

                    owner = await connection.fetchrow(
                        f"SELECT agent_id,user_id,created_at FROM {KSADK_PG_SESSIONS_TABLE} "
                        "WHERE namespace=$1 AND id=$2",
                        self.namespace, session_id,
                    )
                    if (owner is None or owner["agent_id"] != expected_session.agent_id
                            or owner["user_id"] != expected_session.user_id
                            or float(owner["created_at"]) != expected_session.created_at):
                        raise HistoryReloadRequired("Checkpoint session changed during lookup")
                upper = int(await connection.fetchval(
                    f"SELECT COALESCE(MAX(seq_id),0) FROM {KSADK_PG_EVENTS_TABLE} "
                    "WHERE namespace=$1 AND session_id=$2",
                    self.namespace, session_id,
                ) or 0)
                count = int(await connection.fetchval(
                    f"SELECT COUNT(*) FROM {KSADK_PG_EVENTS_TABLE} "
                    "WHERE namespace=$1 AND session_id=$2 AND event_type=ANY($3::text[]) "
                    "AND seq_id<=$4",
                    self.namespace, session_id, kinds, upper,
                ) or 0)
                if count > 8192:
                    raise HistoryProjectionBudgetExceeded(
                        "Checkpoint lookup exceeds 8192 related events"
                    )
                cursor = 0
                used = 0
                while True:
                    candidates = (
                        f"FROM {KSADK_PG_EVENTS_TABLE} WHERE namespace=$1 AND session_id=$2 "
                        "AND event_type=ANY($3::text[]) AND seq_id>$4 AND seq_id<=$5 "
                        "ORDER BY seq_id,id LIMIT $6"
                    )
                    args = (self.namespace, session_id, kinds, cursor, upper, page_size)
                    size = int(await connection.fetchval(
                        "SELECT COALESCE(SUM(octet_length(content_json::text)+"
                        "octet_length(state_delta_json::text)+"
                        "octet_length(metadata_json::text)+"
                        "octet_length(to_jsonb(id)::text)+"
                        "octet_length(to_jsonb(session_id)::text)+"
                        "octet_length(to_jsonb(author)::text)+"
                        "octet_length(to_jsonb(event_type)::text)+"
                        "COALESCE(octet_length(to_jsonb(invocation_id)::text),0)+256),0) "
                        "FROM (SELECT id,session_id,author,event_type,invocation_id,"
                        "content_json,state_delta_json,metadata_json "
                        + candidates + ") AS resume_candidates",
                        *args,
                    ) or 0)
                    if used + size > 16 * 1024 * 1024:
                        raise HistoryProjectionBudgetExceeded(
                            "Checkpoint lookup exceeds 16 MiB of related events"
                        )
                    rows = await connection.fetch(
                        "SELECT id,session_id,author,event_type,content_json,timestamp,"
                        "state_delta_json,seq_id,invocation_id,metadata_json "
                        + candidates, *args,
                    )
                    if not rows:
                        break
                    yield [self._event_from_row(row) for row in rows]
                    used += size
                    cursor = int(rows[-1]["seq_id"])
                    if len(rows) < page_size:
                        break

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
            checkpoint_ids=query.checkpoint_ids,
            run_id=query.run_id,
            offset=query.offset,
            limit=query.limit,
            from_start=True,
        )
        if query.framework is None and connection is None:
            return await self.query_events(event_query)
        if connection is None:
            async with self._connection() as owned:
                bounded = CURRENT_HISTORY_READ_BUDGET.get() is not None
                async with (owned.transaction(isolation="repeatable_read", readonly=True)
                            if bounded else nullcontext()):
                    return await self._scan_checkpoint_events_with_connection(
                        owned, query, cursor,
                    )
        await self._ensure_schema()
        clauses, params = self._batch_event_where(event_query)
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
        candidates = (
            f"FROM {KSADK_PG_EVENTS_TABLE} event_row "
            f"JOIN {KSADK_PG_SESSIONS_TABLE} session_row "
            "ON session_row.namespace=event_row.namespace "
            "AND session_row.id=event_row.session_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY event_row.timestamp,event_row.session_id,"
            f"event_row.seq_id,event_row.id LIMIT ${len(params)-1} OFFSET ${len(params)}"
        )
        await self._precheck_history_body(
            connection,
            "SELECT event_row.content_json,event_row.state_delta_json,"
            "event_row.metadata_json " + candidates,
            params,
        )
        rows = await connection.fetch(
            "SELECT event_row.id,event_row.session_id,event_row.author,event_row.event_type,"
            "event_row.content_json,event_row.timestamp,event_row.state_delta_json,"
            "event_row.seq_id,event_row.invocation_id,event_row.metadata_json "
            + candidates,
            *params,
        )
        return [self._event_from_row(row) for row in rows]

    async def iter_checkpoint_event_chunks(
        self, query: CheckpointEventQuery
    ) -> AsyncIterator[list[SessionEvent]]:
        if query.limit < 1 or query.limit > 50:
            raise ValueError("checkpoint scan limit must be between 1 and 50")
        await self._ensure_schema()
        async with self._connection() as connection:
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
        candidate = None
        max_seq_id = 0
        audit = CheckpointResumeAudit()
        async with asyncio.timeout(self.db_budget.write_operation_seconds):
            async with aclosing(self.iter_resume_related_event_pages(session_id)) as pages:
                async for page in pages:
                    for event in page:
                        creation = checkpoint_creation_identity(event)
                        if event.event_type == "run_checkpoint":
                            metadata = event.metadata or {}
                            if str(metadata.get("run_id") or "") != run_id:
                                continue
                            max_seq_id = max(max_seq_id, event.seq_id)
                            if str(metadata.get("checkpoint_id") or "") == checkpoint_id:
                                candidate = event
                        elif creation is not None:
                            if creation == (run_id, checkpoint_id):
                                max_seq_id = max(max_seq_id, event.seq_id)
                                candidate = event
                        else:
                            identity = checkpoint_resume_identity(event)
                            if identity is not None and identity[:2] == (run_id, checkpoint_id):
                                audit.record(event, identity)
        return {"candidate": candidate, "max_seq_id": max_seq_id,
                "resume_count": audit["resume_count"],
                "last_resumed_at": audit["last_resumed_at"]}

    async def get_checkpoint_stats(
        self, keys: list[tuple[str, str, str]]
    ) -> dict[str, object]:
        from ksadk.sessions._postgres_checkpoint_stats import get_checkpoint_stats

        if len(keys) > 50:
            raise ValueError("checkpoint stats batch cannot exceed 50 keys")
        if not keys:
            return {"audits": {}, "latest_seq_ids": {}}
        await self._ensure_schema()
        active = self._checkpoint_snapshot_connection.get()
        async with asyncio.timeout(self.db_budget.write_operation_seconds):
            async with (self._connection() if active is None else nullcontext(active)) as connection:
                async with (connection.transaction(isolation="repeatable_read", readonly=True)
                            if active is None else nullcontext()):
                    # The master aggregation validates every known carrier before
                    # filtering identity. Bound its duplicated per-key JSON input
                    # in the same snapshot, preserving fail-loud audit semantics.
                    sessions = [key[0] for key in dict.fromkeys(keys)]
                    sizes = await connection.fetchrow(
                        f"SELECT COUNT(*) AS event_count, COALESCE(SUM("
                        "octet_length(event_row.content_json::text)+"
                        "octet_length(event_row.metadata_json::text)+1024),0) AS body_bytes "
                        f"FROM {KSADK_PG_EVENTS_TABLE} event_row "
                        "JOIN unnest($2::text[]) requested(session_id) "
                        "ON event_row.session_id=requested.session_id "
                        "WHERE event_row.namespace=$1 AND event_row.event_type IN "
                        "('run_resume','runtime_resume','continuation.resumed','continuation.created')",
                        self.namespace, sessions,
                    )
                    size = int(sizes["body_bytes"])
                    if int(sizes["event_count"]) > 8192 or size > 16 * 1024 * 1024:
                        raise HistoryProjectionBudgetExceeded("Checkpoint audit exceeds related-event budget")
                    budget = CURRENT_HISTORY_READ_BUDGET.get()
                    if budget is not None:
                        budget.account(size)
                    token = self._checkpoint_snapshot_connection.set(connection)
                    try:
                        return await get_checkpoint_stats(self, keys)
                    finally:
                        self._checkpoint_snapshot_connection.reset(token)

    @_write_deadline
    async def claim_tool_receipt(self, session_id: str, **kwargs):
        return await super().claim_tool_receipt(session_id, **kwargs)

    @_write_deadline
    async def settle_tool_receipt(self, session_id: str, **kwargs):
        return await super().settle_tool_receipt(session_id, **kwargs)

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
        async with self._connection() as connection:
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
        async with self._connection() as connection:
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
        async with self._connection() as connection:
            if scope == "session" and session_id:
                session = await self._get_session_with_connection(
                    connection, session_id, include_events=False
                )
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

    @_write_deadline
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
        async with self._connection(write=True) as connection:
            async with connection.transaction():
                await self._set_lock_timeout(connection)
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
                    command_timeout=self.db_budget.write_sql_seconds,
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
        rows = await getattr(connection, "fetch_replay", connection.fetch)(
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
