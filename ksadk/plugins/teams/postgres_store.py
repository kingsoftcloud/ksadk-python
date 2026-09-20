"""Pooled PostgreSQL persistence for a server-owned Teams authority.

A store owns authority configuration, not a permanent connection. Domain state,
watermarks, outbox, quota reservations and idempotency receipts can commit on
one checked-out connection. Scheduler fencing remains the host's responsibility.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

from .errors import TeamsError
from .store import QuotaScope, TeamsStore, Transaction, query_projection


class _Connection:
    def __init__(
        self, connection, *, authority_ref: str, quota_keys: frozenset[tuple[str, str, str]]
    ):
        self.raw = connection
        self._authority_ref = authority_ref
        self._quota_keys = quota_keys

    @property
    def authority_ref(self) -> str:
        return self._authority_ref

    @property
    def quota_keys(self) -> frozenset[tuple[str, str, str]]:
        """Scopes locked before the authority; server quota writes must verify them."""
        return self._quota_keys

    def execute(self, query, parameters=()):
        return self.raw.execute(query.replace("?", "%s"), parameters)


def authority_schema(authority_ref: str) -> tuple[str, int]:
    if not isinstance(authority_ref, str) or not authority_ref:
        raise ValueError("authority_ref is required")
    fingerprint = (
        authority_ref[3:]
        if re.fullmatch(r"ta_[0-9a-f]{64}", authority_ref)
        else hashlib.sha256(authority_ref.encode()).hexdigest()
    )
    return "teams_" + fingerprint[:24], int(fingerprint[:15], 16)


AUTHORITY_SCHEMA_STATEMENTS = (
    "CREATE TABLE IF NOT EXISTS team_objects (ordinal BIGSERIAL PRIMARY KEY,"
    "kind TEXT NOT NULL,object_id TEXT NOT NULL,group_id TEXT,team_run_id TEXT,"
    "revision BIGINT NOT NULL,body TEXT NOT NULL,UNIQUE(kind,object_id))",
    "CREATE INDEX IF NOT EXISTS team_objects_scope ON team_objects(kind,group_id,team_run_id)",
    "CREATE TABLE IF NOT EXISTS group_sequences "
    "(group_id TEXT PRIMARY KEY,watermark BIGINT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS group_events (group_id TEXT NOT NULL,seq BIGINT NOT NULL,"
    "event_id TEXT NOT NULL UNIQUE,body TEXT NOT NULL,PRIMARY KEY(group_id,seq))",
    "CREATE TABLE IF NOT EXISTS team_idempotency (scope TEXT NOT NULL,key TEXT NOT NULL,"
    "payload_digest TEXT NOT NULL,response TEXT NOT NULL,PRIMARY KEY(scope,key))",
    "ALTER TABLE team_objects ADD COLUMN IF NOT EXISTS status TEXT",
    "ALTER TABLE team_objects ADD COLUMN IF NOT EXISTS due_at DOUBLE PRECISION NOT NULL DEFAULT 0",
    "ALTER TABLE team_objects ADD COLUMN IF NOT EXISTS terminal_state TEXT",
)

QUOTA_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS public.teams_quotas ("
    "scope_type TEXT NOT NULL,scope_id TEXT NOT NULL,resource TEXT NOT NULL,"
    "reserved BIGINT NOT NULL DEFAULT 0 CHECK(reserved >= 0),"
    '"limit" BIGINT NOT NULL CHECK("limit" >= 0),revision BIGINT NOT NULL DEFAULT 1,'
    "PRIMARY KEY(scope_type,scope_id,resource))"
)


def schema_migration_checksum() -> str:
    """Reviewable checksum for the additive domain schema migration."""
    from .store import digest

    return digest(
        {
            "domainSchemaVersion": 1,
            "queryProjectionVersion": 1,
            "statements": AUTHORITY_SCHEMA_STATEMENTS,
            "dueIndex": "team_objects(kind,status,due_at,ordinal)",
        }
    )


def initialize_schema(pool: Any, *, authority_ref: str) -> None:
    """Explicit migration/provisioning entrypoint; use a DDL-capable pool.

    Runtime roles using ``initialize=False`` never reach this function.
    The additive query projection preserves the existing domain schema v1.
    """
    from psycopg import sql

    schema, advisory_key = authority_schema(authority_ref)
    with pool.connection() as connection, connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (advisory_key,))
        connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        connection.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))
        connection.execute(
            "CREATE TABLE IF NOT EXISTS teams_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)"
        )
        version = connection.execute(
            "SELECT value FROM teams_meta WHERE key='schema_version'"
        ).fetchone()
        if version and version[0] != "1":
            raise TeamsError(
                "schema_version_unsupported", "团队数据版本不兼容，需要迁移", status=503
            )
        owner = connection.execute(
            "SELECT value FROM teams_meta WHERE key='authority_ref'"
        ).fetchone()
        if owner and owner[0] != authority_ref:
            raise TeamsError("authority_schema_conflict", "团队存储作用域冲突", status=503)
        for statement in AUTHORITY_SCHEMA_STATEMENTS:
            connection.execute(statement)
        projected = connection.execute(
            "SELECT value FROM teams_meta WHERE key='query_projection_version'"
        ).fetchone()
        if projected and projected[0] != "1":
            raise TeamsError("schema_version_unsupported", "团队查询版本不兼容", status=503)
        if not projected:
            after = 0
            while True:
                rows = connection.execute(
                    "SELECT ordinal,body FROM team_objects WHERE ordinal>%s "
                    "ORDER BY ordinal LIMIT 500",
                    (after,),
                ).fetchall()
                if not rows:
                    break
                for ordinal, body in rows:
                    connection.execute(
                        "UPDATE team_objects SET status=%s,due_at=%s,terminal_state=%s "
                        "WHERE ordinal=%s",
                        (*query_projection(json.loads(body)), ordinal),
                    )
                after = rows[-1][0]
        connection.execute(
            "CREATE INDEX IF NOT EXISTS team_objects_due "
            "ON team_objects(kind,status,due_at,ordinal)"
        )
        for key, value in (
            ("schema_version", "1"),
            ("query_projection_version", "1"),
            ("authority_ref", authority_ref),
        ):
            connection.execute(
                "INSERT INTO teams_meta VALUES(%s,%s) ON CONFLICT DO NOTHING", (key, value)
            )


@dataclass
class _ActiveTransaction:
    owner: tuple[int, object]
    tx: Transaction
    quota_keys: frozenset[tuple[str, str, str]]
    failed: bool = False


def _owner() -> tuple[int, object]:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), task


def _quota_scopes(scopes: Iterable[QuotaScope]) -> tuple[QuotaScope, ...]:
    by_key: dict[tuple[str, str, str], QuotaScope] = {}
    for scope in scopes:
        if not isinstance(scope, QuotaScope):
            raise TypeError("quota_scopes must contain QuotaScope values")
        key = (scope.scope_type, scope.scope_id, scope.resource)
        previous = by_key.get(key)
        if previous and previous.limit != scope.limit:
            raise ValueError("conflicting quota defaults")
        by_key[key] = scope
    return tuple(by_key[key] for key in sorted(by_key))


class PostgresTeamsStore(TeamsStore):
    def __init__(
        self,
        dsn: str | None = None,
        *,
        authority_ref: str,
        pool: Any = None,
        initialize: bool | None = None,
        lock_timeout_ms: int = 1000,
        statement_timeout_ms: int = 2000,
    ) -> None:
        try:
            import psycopg  # noqa: F401
            from psycopg_pool import ConnectionPool
        except ImportError as error:
            raise TeamsError(
                "postgres_driver_required", "团队服务端需要 PostgreSQL 连接池驱动", status=503
            ) from error
        if lock_timeout_ms < 1 or statement_timeout_ms < 1:
            raise ValueError("timeouts must be positive")
        if pool is not None and dsn is not None:
            raise ValueError("provide either dsn or pool")
        if pool is None and not dsn:
            raise ValueError("dsn or pool is required")
        self.schema, self._advisory_key = authority_schema(authority_ref)
        self.authority_ref = authority_ref
        self.path = None
        self._closed = False
        self._lock = threading.RLock()
        self._owns_pool = pool is None
        self._pool = (
            pool
            if pool is not None
            else ConnectionPool(
                dsn,
                min_size=0,
                max_size=1,
                timeout=5,
                open=True,
                kwargs={"autocommit": True, "connect_timeout": 5},
            )
        )
        self._lock_timeout_ms = lock_timeout_ms
        self._statement_timeout_ms = statement_timeout_ms
        self._active: ContextVar[_ActiveTransaction | None] = ContextVar(
            "teams_pg_transaction", default=None
        )
        try:
            # Preserve the original DSN-only local/test construction behavior.
            if initialize if initialize is not None else self._owns_pool:
                initialize_schema(self._pool, authority_ref=authority_ref)
            self._validate_schema()
        except BaseException:
            if self._owns_pool:
                self._pool.close()
            raise

    def _validate_schema(self) -> None:
        import psycopg
        from psycopg import sql

        try:
            with self._pool.connection() as connection, connection.transaction():
                connection.execute(
                    sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(self.schema))
                )
                metadata = dict(connection.execute("SELECT key,value FROM teams_meta").fetchall())
                if (
                    metadata.get("schema_version") != "1"
                    or metadata.get("query_projection_version") != "1"
                ):
                    raise TeamsError("schema_migration_required", "请先迁移团队存储", status=503)
                if metadata.get("authority_ref") != self.authority_ref:
                    raise TeamsError("authority_schema_conflict", "团队存储作用域冲突", status=503)
                connection.execute("SELECT status,due_at,terminal_state FROM team_objects LIMIT 0")
        except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn) as error:
            raise TeamsError(
                "schema_migration_required", "请先初始化团队存储", status=503
            ) from error

    @contextmanager
    def transaction(self, *, quota_scopes: Iterable[QuotaScope] = ()) -> Iterator[Transaction]:
        from psycopg import sql

        if self._closed:
            raise TeamsError("store_closed", "团队存储已关闭", status=503)
        scopes = _quota_scopes(quota_scopes)
        keys = frozenset((scope.scope_type, scope.scope_id, scope.resource) for scope in scopes)
        active = self._active.get()
        if active is not None:
            if active.owner != _owner():
                raise TeamsError(
                    "transaction_context_mismatch", "事务不能跨任务或线程共享", status=503
                )
            if not keys.issubset(active.quota_keys):
                raise TeamsError("quota_lock_order", "配额必须在最外层事务预先锁定", status=503)
            try:
                yield active.tx
            except BaseException:
                active.failed = True
                raise
            return
        with self._pool.connection() as connection, connection.transaction():
            connection.execute(
                "SELECT set_config('lock_timeout',%s,true)", (f"{self._lock_timeout_ms}ms",)
            )
            connection.execute(
                "SELECT set_config('statement_timeout',%s,true)",
                (f"{self._statement_timeout_ms}ms",),
            )
            connection.execute(
                sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(self.schema))
            )
            for scope in scopes:
                key = (scope.scope_type, scope.scope_id, scope.resource)
                connection.execute(
                    "INSERT INTO public.teams_quotas"
                    '(scope_type,scope_id,resource,reserved,"limit",revision) '
                    "VALUES(%s,%s,%s,0,%s,1) ON CONFLICT(scope_type,scope_id,resource) DO NOTHING",
                    (*key, scope.limit),
                )
                connection.execute(
                    "SELECT revision FROM public.teams_quotas WHERE scope_type=%s AND scope_id=%s "
                    "AND resource=%s FOR UPDATE",
                    key,
                ).fetchone()
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (self._advisory_key,))
            state = _ActiveTransaction(
                _owner(),
                Transaction(
                    _Connection(connection, authority_ref=self.authority_ref, quota_keys=keys)
                ),
                keys,
            )
            token = self._active.set(state)
            try:
                yield state.tx
                if state.failed:
                    raise TeamsError("transaction_aborted", "嵌套事务失败，操作未提交", status=503)
            finally:
                self._active.reset(token)
        # No retry here, including an uncertain COMMIT response. The caller
        # resolves the persisted idempotency receipt before attempting again.

    def events(self, group_id: str, after: int = 0, limit: int = 200):
        if after < 0 or not 1 <= limit <= 1000:
            raise TeamsError("invalid_cursor", "事件游标或分页大小无效", status=422)
        with self.transaction() as tx:
            if after > tx.watermark(group_id):
                raise TeamsError("reset_required", "事件游标失效，请重新获取团队快照")
            return [
                json.loads(row[0])
                for row in tx.connection.execute(
                    "SELECT body FROM group_events WHERE group_id=? AND seq>? ORDER BY seq LIMIT ?",
                    (group_id, after, limit),
                )
            ]

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._owns_pool:
                self._pool.close()
