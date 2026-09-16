"""PostgreSQL persistence for a server-owned Teams authority.

The same Transaction implements the domain on both stores. Each authority has
an isolated schema and a database transaction lock: objects, event watermarks,
outbox and idempotency receipts commit atomically across server processes.
Scheduling leases are separate from this short-lived transaction lock.
"""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from typing import Iterator

from .errors import TeamsError
from .store import TeamsStore, Transaction


class _Connection:
    def __init__(self, connection):
        self.raw = connection

    def execute(self, query, parameters=()):
        # Domain SQL uses DB-API positional placeholders, never interpolated
        # values or identifiers. No domain query contains a literal '?'.
        return self.raw.execute(query.replace("?", "%s"), parameters)


class PostgresTeamsStore(TeamsStore):
    def __init__(self, dsn: str, *, authority_ref: str) -> None:
        try:
            import psycopg
            from psycopg import sql
        except ImportError as error:
            raise TeamsError(
                "postgres_driver_required", "团队服务端需要安装 PostgreSQL 驱动", status=503
            ) from error
        fingerprint = hashlib.sha256(authority_ref.encode()).hexdigest()
        self.schema = "teams_" + fingerprint[:24]
        self._advisory_key = int(fingerprint[:15], 16)
        self._lock = threading.RLock()
        self._dsn = dsn
        self._closed = False
        self.path = None
        self._raw = psycopg.connect(dsn, autocommit=True, connect_timeout=10)
        self._raw.execute("SET lock_timeout = '5s'")
        self._raw.execute("SET statement_timeout = '15s'")
        self._connection = _Connection(self._raw)
        try:
            self._raw.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema))
            )
            self._raw.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
            with self._raw.transaction():
                self._raw.execute("SELECT pg_advisory_xact_lock(%s)", (self._advisory_key,))
                self._raw.execute(
                    "CREATE TABLE IF NOT EXISTS teams_meta "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                version = self._raw.execute(
                    "SELECT value FROM teams_meta WHERE key='schema_version'"
                ).fetchone()
                if version and version[0] != "1":
                    raise TeamsError(
                        "schema_version_unsupported", "团队数据版本不兼容，需要迁移", status=503
                    )
                for statement in (
                    "CREATE TABLE IF NOT EXISTS team_objects (ordinal BIGSERIAL PRIMARY KEY,"
                    "kind TEXT NOT NULL,object_id TEXT NOT NULL,group_id TEXT,team_run_id TEXT,"
                    "revision BIGINT NOT NULL,body TEXT NOT NULL,UNIQUE(kind,object_id))",
                    "CREATE INDEX IF NOT EXISTS team_objects_scope "
                    "ON team_objects(kind,group_id,team_run_id)",
                    "CREATE TABLE IF NOT EXISTS group_sequences "
                    "(group_id TEXT PRIMARY KEY,watermark BIGINT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS group_events (group_id TEXT NOT NULL,"
                    "seq BIGINT NOT NULL,event_id TEXT NOT NULL UNIQUE,body TEXT NOT NULL,"
                    "PRIMARY KEY(group_id,seq))",
                    "CREATE TABLE IF NOT EXISTS team_idempotency (scope TEXT NOT NULL,"
                    "key TEXT NOT NULL,payload_digest TEXT NOT NULL,response TEXT NOT NULL,"
                    "PRIMARY KEY(scope,key))",
                    "INSERT INTO teams_meta VALUES('schema_version','1') ON CONFLICT DO NOTHING",
                ):
                    self._raw.execute(statement)
        except BaseException:
            self._raw.close()
            raise

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        import psycopg
        from psycopg import sql

        with self._lock:
            if self._closed:
                raise TeamsError("store_closed", "团队存储已关闭", status=503)
            if self._raw.closed:
                self._raw = psycopg.connect(self._dsn, autocommit=True, connect_timeout=10)
                self._connection = _Connection(self._raw)
                try:
                    self._raw.execute("SET lock_timeout = '5s'")
                    self._raw.execute("SET statement_timeout = '15s'")
                    self._raw.execute(
                        sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema))
                    )
                except Exception:
                    self._raw.close()
                    raise
            try:
                with self._raw.transaction():
                    self._raw.execute("SELECT pg_advisory_xact_lock(%s)", (self._advisory_key,))
                    yield Transaction(self._connection)
            except (psycopg.OperationalError, psycopg.InterfaceError):
                # A lost COMMIT acknowledgement is unknown, not a rollback.
                # Never replay this transaction; the next caller reconnects
                # and resolves the durable command/idempotency receipt first.
                self._raw.close()
                raise

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
            self._raw.close()
