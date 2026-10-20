"""PostgreSQL canonical session event log and transactional append."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ksadk.kernel.contracts import ActivationWriteGuard
from ksadk.kernel.errors import StaleFenceError

if TYPE_CHECKING:
    from ksadk.kernel.postgres_store import PostgresAgentKernelStore

import json
from contextlib import asynccontextmanager
from typing import Any, Callable

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
from ksadk.sessions.db_budget import DatabaseBudget, budgeted_pool_connection, session_db_budget


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
        budget_provider: Callable[[], DatabaseBudget] | None = None,
    ) -> None:
        self._budget_provider = budget_provider or (lambda: session_db_budget())
        self._pool = pool
        self._namespace = namespace.strip() or "default"
        self._tenant_id = tenant_id.strip() or "default"
        self._workspace_id = workspace_id.strip() or "default"

    @asynccontextmanager
    async def _connection(self, *, write: bool = False):
        async with budgeted_pool_connection(
            self._pool, self._budget_provider(), write=write,
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
        inserted = await connection.fetchrow(
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
            RETURNING seq_id, timestamp
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
        if inserted is not None:
            # The INSERT has the exact payload already. Returning its small
            # receipt avoids downloading a full graph snapshot again while
            # holding both the session row and activation fence locks.
            packed.bind_seq_id(int(inserted["seq_id"]))
            packed.timestamp = inserted["timestamp"]
            return session_event_to_envelope(packed)
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

    async def get_max_seq_id(self, session_id: str) -> int:
        """Indexed metadata read; never decode historical event bodies."""
        async with self._connection() as connection:
            return int(await connection.fetchval(
                f"SELECT COALESCE(MAX(seq_id),0) FROM {KSADK_PG_EVENTS_TABLE} "
                "WHERE namespace=$1 AND session_id=$2", self._namespace, session_id,
            ) or 0)

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
        return self._decode_rows(session_id, rows)

    async def read_runtime(
        self, session_id: str, *, after_seq: int = 0, before_seq: int | None = None,
        run_id: str | None = None, event_types: list[str] | None = None,
        item_kinds: list[str] | None = None, scope_id: str | None = None,
        limit: int = 256, newest_first: bool = False,
    ) -> list[SessionEventEnvelope]:
        if limit < 1:
            raise ValueError("limit must be positive")
        args: list[Any] = [self._namespace, session_id, after_seq]
        where = ["namespace=$1", "session_id=$2", "seq_id > $3",
                 "metadata_json->>'family' = 'runtime'"]
        for clause, value in (("seq_id <", before_seq), ("invocation_id =", run_id),
                              ("event_type = ANY", event_types),
                              ("content_json->'runtime_event'->>'item_kind' = ANY", item_kinds),
                              ("content_json->'runtime_event'->>'scope_id' =", scope_id)):
            if value is not None:
                args.append(value)
                placeholder = f"${len(args)}"
                where.append(f"{clause}({placeholder}::text[])" if clause.endswith("ANY")
                             else f"{clause} {placeholder}")
        args.append(limit)
        order = "DESC" if newest_first else "ASC"
        async with self._connection() as connection:
            query = (f"SELECT id, content_json, metadata_json, seq_id, timestamp, author,"
                f" event_type, invocation_id FROM {KSADK_PG_EVENTS_TABLE} WHERE "
                + " AND ".join(where) + f" ORDER BY seq_id {order} LIMIT ${len(args)}")
            if limit > 32 and event_types is None:
                # Restoring a run still needs every fact. Stream its opaque
                # state through the existing replay budget instead of one
                # potentially multi-MiB ordinary read command.
                from ksadk.sessions.postgres_service import _TimedConnection

                budget = self._budget_provider()
                replay = _TimedConnection(
                    connection, budget.read_sql_seconds, budget.cleanup_seconds,
                    budget.replay_operation_seconds, budget.replay_sql_seconds,
                )
                rows = await replay.fetch_replay(query, *args, batch_size=1)
            else:
                rows = await connection.fetch(query, *args)
        return self._decode_rows(session_id, rows)

    async def has_run_transition(self, session_id, run_id, state):
        async with self._connection() as connection:
            return bool(await connection.fetchval(
                f"SELECT EXISTS(SELECT 1 FROM {KSADK_PG_EVENTS_TABLE} "
                "WHERE namespace=$1 AND session_id=$2 AND invocation_id=$3 "
                "AND event_type='control.run_transition' AND metadata_json->>'family'='control' "
                "AND content_json->'session_event'->'payload'->>'state'=$4)",
                self._namespace, session_id, run_id, state))

    async def find_interaction_resolution(self, record):
        keys = ("interaction_id", "tenant_id", "agent_instance_id", "session_id", "run_id", "kind", "revision")
        args = [self._namespace, record.session_id, record.run_id]
        clauses = ["namespace=$1", "session_id=$2", "invocation_id=$3",
                   "event_type='interaction.resolved'", "metadata_json->>'family'='interaction'",
                   "metadata_json->>'family_version'='1'"]
        for key in keys:
            args.append(str(getattr(record, key)))
            clauses.append(f"content_json->'session_event'->'payload'->>'{key}'=${len(args)}")
        async with self._connection() as connection:
            rows = await connection.fetch(
                "SELECT id,content_json,metadata_json,seq_id,timestamp,author,event_type,invocation_id "
                f"FROM {KSADK_PG_EVENTS_TABLE} WHERE " + " AND ".join(clauses)
                + " ORDER BY seq_id LIMIT 1", *args)
        decoded = self._decode_rows(record.session_id, rows)
        return decoded[0] if decoded else None

    async def read_subscription_page(
        self, session_id: str, after_seq: int, *, limit: int = 64,
        byte_limit: int = 512 * 1024,
    ) -> list[SessionEventEnvelope]:
        """Drain every fact using small byte-bounded subscription batches.

        Unlike `read`, this may return a short page with more facts remaining.
        One indivisible oversized fact always advances the sequence cursor.
        """
        if limit < 1 or byte_limit < 1:
            raise ValueError("subscription limits must be positive")
        async with self._connection() as connection:
            rows = await connection.fetch(
                f"""WITH candidates AS (
                    SELECT id,content_json,metadata_json,seq_id,timestamp,author,
                           event_type,invocation_id,
                           COALESCE(octet_length(content_json::text),0) +
                           COALESCE(octet_length(metadata_json::text),0) + 1024 AS bytes
                    FROM {KSADK_PG_EVENTS_TABLE}
                    WHERE namespace=$1 AND session_id=$2 AND seq_id>$3
                    ORDER BY seq_id LIMIT $4
                ), measured AS (
                    SELECT *,ROW_NUMBER() OVER (ORDER BY seq_id) AS position,
                           SUM(bytes) OVER (ORDER BY seq_id) AS cumulative_bytes
                    FROM candidates
                )
                SELECT id,content_json,metadata_json,seq_id,timestamp,author,
                       event_type,invocation_id FROM measured
                WHERE cumulative_bytes <= $5 OR position=1 ORDER BY seq_id""",
                self._namespace, session_id, int(after_seq), limit, byte_limit,
            )
        return self._decode_rows(session_id, rows)

    @staticmethod
    def _decode_rows(session_id, rows) -> list[SessionEventEnvelope]:
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


class PostgresFencedSessionEventStore:
    """事务级 fenced ``SessionEventStore``（Task 4 Step 5）。

    typed RuntimeEvent 写路径（``RuntimeEventStore.append -> append(envelope,
    guard=ActivationWriteGuard)``）的缺口修复：每个 ActivationWriteGuard
    append 都在**同一个 PostgreSQL 事务**里先对 activation 行做
    ``FOR SHARE`` compare-fence（activation_id / fencing_token / 未过期 /
    未 released），再执行 event insert——被 takeover 的旧 owner 在写出任何
    runtime/progress/terminal 事实之前就被 :class:`StaleFenceError` 回滚。

    AdmissionWriteGuard（accepted/rejected admission 事实）继续由
    :class:`PostgresAgentKernelStore` 的 writer 事务内联处理；独立调用时
    走 event log 自开事务。
    """

    def __init__(
        self, store: "PostgresAgentKernelStore",
        *, budget_provider: Callable[[], DatabaseBudget] | None = None,
    ) -> None:
        self._budget_provider = budget_provider or (lambda: session_db_budget())
        self._store = store
        self._log = store._events

    @asynccontextmanager
    async def _connection(self):
        async with self._store._connection(write=True) as connection:
            yield connection

    async def append(
        self, envelope: SessionEventEnvelope, *, guard: Any
    ) -> SessionEventEnvelope:
        from ksadk.events.session_event import validate_write_guard

        validate_write_guard(envelope, guard)
        if isinstance(guard, ActivationWriteGuard):
            async with asyncio.timeout(self._budget_provider().write_operation_seconds):
                async with self._connection() as connection:
                    async with connection.transaction():
                        await self._assert_activation_fence(
                            connection, guard, envelope.session_id
                        )
                        return await self._log.append_on(connection, envelope, guard)
        return await self._log.append(envelope, guard=guard)

    async def _assert_activation_fence(
        self, connection: Any, guard: ActivationWriteGuard, session_id: str
    ) -> None:
        row = await connection.fetchrow(
            "SELECT activation_id, fencing_token, lease_expires_at, released"
            " FROM kernel_activations WHERE activation_id = $1 AND session_id = $2"
            " FOR SHARE",
            guard.activation_id,
            session_id,
        )
        if (
            row is None
            or row["released"]
            or row["lease_expires_at"] <= await connection.fetchval("SELECT clock_timestamp()")
            or int(row["fencing_token"]) != int(guard.fencing_token)
        ):
            raise StaleFenceError(
                "activation lease does not match runtime event write guard",
                details={
                    "activation_id": guard.activation_id,
                    "expected_fence": int(guard.fencing_token),
                    "observed_activation_id": (
                        str(row["activation_id"]) if row is not None else None
                    ),
                    "observed_fence": (
                        int(row["fencing_token"]) if row is not None else None
                    ),
                    "released": bool(row["released"]) if row is not None else None,
                    "lease_expires_at": (
                        row["lease_expires_at"].isoformat() if row is not None else None
                    ),
                },
            )

    async def read(
        self, session_id: str, after_seq: int, limit: int
    ) -> list[SessionEventEnvelope]:
        return await self._log.read(session_id, after_seq, limit)

    async def get_by_id(self, session_id: str, event_id: Any) -> SessionEventEnvelope | None:
        return await self._log.get_by_id(session_id, event_id)

    async def get_max_seq_id(self, session_id: str) -> int:
        return await self._log.get_max_seq_id(session_id)

    async def has_run_transition(self, session_id, run_id, state):
        return await self._log.has_run_transition(session_id, run_id, state)

    async def find_interaction_resolution(self, record):
        return await self._log.find_interaction_resolution(record)

    async def read_runtime(self, session_id: str, **query) -> list[SessionEventEnvelope]:
        return await self._log.read_runtime(session_id, **query)

    async def subscribe(
        self, session_id: str, after_seq: int, *, poll_interval: float = 0.25
    ):
        import asyncio

        cursor = int(after_seq or 0)
        while True:
            envelopes = await self._log.read_subscription_page(session_id, cursor)
            for envelope in envelopes:
                cursor = max(cursor, int(envelope.seq))
                yield envelope
            if not envelopes:
                await asyncio.sleep(poll_interval)

    async def subscribe_recovering(
        self, session_id: str, after_seq: int, *, poll_interval: float = 0.25,
    ):
        """Internal A2A observation: resume reads, never submit another command.

        Public SSE keeps its existing transport recovery/permit boundary. This
        capability is for the direct, already-admitted A2A observer which has
        no HTTP reconnect layer. Each successful SQL page, including an empty
        page, establishes backend health before another outage budget starts.
        """
        from ksadk.sessions.resilience import is_transient_session_backend_failure

        cursor = int(after_seq or 0)
        loop = asyncio.get_running_loop()
        while True:
            deadline = loop.time() + self._budget_provider().event_observe_recovery_seconds
            delay = 0.2
            while True:
                try:
                    async with asyncio.timeout(max(0.001, deadline - loop.time())):
                        envelopes = await self._log.read_subscription_page(session_id, cursor)
                    break
                except Exception as exc:
                    remaining = deadline - loop.time()
                    if remaining <= 0 or not is_transient_session_backend_failure(exc):
                        raise
                    logging.getLogger(__name__).warning(
                        "Retrying admitted A2A event read after %s at cursor=%d",
                        type(exc).__name__, cursor,
                    )
                    await asyncio.sleep(min(delay, remaining))
                    delay = min(5.0, delay * 2)
            for envelope in envelopes:
                if envelope.session_id != session_id:
                    raise PermissionError("Kernel event store returned another session")
                if envelope.seq <= cursor:
                    continue
                cursor = envelope.seq
                yield envelope
            if not envelopes:
                await asyncio.sleep(poll_interval)
