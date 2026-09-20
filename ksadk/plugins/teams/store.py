"""Transactional SQLite companion owned and versioned by the Teams plugin.

No Kernel inbox/run/lease records live here. Messages, routing decisions,
outbox and group events commit together. All writes use BEGIN IMMEDIATE;
idempotency receipts are in that same transaction, including their payload
digest. The runtime separately acquires single-authority process ownership.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, TypeVar
from uuid import uuid4

from .contracts import API_VERSION
from .errors import TeamsError

T = TypeVar("T")
KINDS = frozenset(
    {
        "group",
        "group_revision",
        "member",
        "run_member",
        "team_run",
        "message",
        "task",
        "attempt",
        "delivery",
        "trigger",
        "wait",
        "interaction",
        "artifact",
        "cursor",
        "reservation",
        "read_watermark",
        "control",
        "child_invocation",
        "installation",
        "member_control",
        "execution_node",
        "execution_command",
        "execution_lease",
        "leader_checkpoint",
        "leader_takeover",
        "teams_import",
        "coordinator",
        "grant_operation",
        "native_control",
        "interaction_response",
        "effect",
        "effect_evidence",
        "effect_reconciliation",
        "tool_invocation",
    }
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def encode(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(encode(value).encode()).hexdigest()


@dataclass(frozen=True)
class ObjectPage:
    """An ordinal cursor is scoped to the same filters by the caller."""

    items: list[dict[str, Any]]
    next_cursor: int | None


@dataclass(frozen=True, order=True)
class QuotaScope:
    scope_type: str
    scope_id: str
    resource: str
    limit: int

    def __post_init__(self):
        if (
            not all(
                isinstance(value, str) and value
                for value in (self.scope_type, self.scope_id, self.resource)
            )
            or isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or self.limit < 0
        ):
            raise ValueError("invalid quota scope")


def query_projection(value: dict[str, Any]) -> tuple[str | None, float, str | None]:
    """Materialized query columns; JSON remains the authoritative object."""
    due = value.get("_nextRetryAt", value.get("nextAttemptAt", value.get("dueAt", 0)))
    if isinstance(due, str):
        due = datetime.fromisoformat(due.replace("Z", "+00:00")).timestamp()
    due = float(due or 0)
    if not math.isfinite(due):
        raise ValueError("due time must be finite")
    return value.get("status", value.get("state")), due, value.get("_terminalState")


def _page_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise TeamsError("invalid_cursor", "分页大小必须在 1 到 1000 之间", status=422)


class Transaction:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def get(self, kind: str, key: str, *, required: bool = True) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT body FROM team_objects WHERE kind=? AND object_id=?", (kind, key)
        ).fetchone()
        if row is None:
            if required:
                raise TeamsError("not_found", "记录不存在或不可访问", status=404)
            return None
        return json.loads(row[0])

    def list(
        self, kind: str, group_id: str | None = None, *, team_run_id: str | None = None
    ) -> list[dict[str, Any]]:
        where, params = ["kind=?"], [kind]
        if group_id is not None:
            where.append("group_id=?")
            params.append(group_id)
        if team_run_id is not None:
            where.append("team_run_id=?")
            params.append(team_run_id)
        return [
            json.loads(row[0])
            for row in self.connection.execute(
                "SELECT body FROM team_objects WHERE " + " AND ".join(where) + " ORDER BY ordinal",
                params,
            )
        ]

    def recent(
        self, kind: str, group_id: str, limit: int = 12, *, team_run_id: str | None = None
    ) -> list[dict[str, Any]]:
        _page_limit(limit)
        scope = " AND team_run_id=?" if team_run_id is not None else ""
        params = (
            (kind, group_id, team_run_id, limit)
            if team_run_id is not None
            else (kind, group_id, limit)
        )
        rows = self.connection.execute(
            "SELECT body FROM team_objects WHERE kind=? AND group_id=?"
            + scope
            + " ORDER BY ordinal DESC LIMIT ?",
            params,
        ).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    def list_page(
        self,
        kind: str,
        group_id: str | None = None,
        *,
        team_run_id: str | None = None,
        after: int = 0,
        limit: int = 100,
    ) -> ObjectPage:
        _page_limit(limit)
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise TeamsError("invalid_cursor", "对象游标无效", status=422)
        where, params = ["kind=?", "ordinal> ?"], [kind, after]
        if group_id is not None:
            where.append("group_id=?")
            params.append(group_id)
        if team_run_id is not None:
            where.append("team_run_id=?")
            params.append(team_run_id)
        rows = self.connection.execute(
            "SELECT ordinal,body FROM team_objects WHERE "
            + " AND ".join(where)
            + " ORDER BY ordinal LIMIT ?",
            [*params, limit + 1],
        ).fetchall()
        return ObjectPage(
            [json.loads(row[1]) for row in rows[:limit]],
            int(rows[limit - 1][0]) if len(rows) > limit else None,
        )

    def list_due(
        self,
        kind: str,
        *,
        due_before: float,
        statuses: Iterable[str] = ("pending",),
        group_id: str | None = None,
        team_run_id: str | None = None,
        limit: int = 100,
        exclude_terminal: bool = True,
    ) -> list[dict[str, Any]]:
        _page_limit(limit)
        if not math.isfinite(due_before):
            raise ValueError("due_before must be finite")
        states = tuple(sorted(set(statuses)))
        if not states:
            return []
        where = ["kind=?", "status IN (" + ",".join("?" for _ in states) + ")", "due_at<=?"]
        params: list[Any] = [kind, *states, due_before]
        if group_id is not None:
            where.append("group_id=?")
            params.append(group_id)
        if team_run_id is not None:
            where.append("team_run_id=?")
            params.append(team_run_id)
        if exclude_terminal:
            where.append("terminal_state IS NULL")
        return [
            json.loads(row[0])
            for row in self.connection.execute(
                "SELECT body FROM team_objects WHERE "
                + " AND ".join(where)
                + " ORDER BY due_at,ordinal LIMIT ?",
                [*params, limit],
            )
        ]

    def put(
        self, kind: str, key: str, value: dict[str, Any], *, expected_revision: int | None = None
    ) -> None:
        if kind not in KINDS:
            raise ValueError("unknown Teams object kind")
        current = self.get(kind, key, required=False)
        if expected_revision is not None and (
            current is None or current.get("revision") != expected_revision
        ):
            raise TeamsError("revision_conflict", "记录已更新，请刷新后重试")
        status, due_at, terminal_state = query_projection(value)
        self.connection.execute(
            "INSERT INTO team_objects"
            "(kind,object_id,group_id,team_run_id,revision,body,status,due_at,terminal_state) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(kind,object_id) DO UPDATE SET group_id=excluded.group_id,"
            "team_run_id=excluded.team_run_id,revision=excluded.revision,body=excluded.body,"
            "status=excluded.status,due_at=excluded.due_at,terminal_state=excluded.terminal_state",
            (
                kind,
                key,
                value.get("groupId"),
                value.get("teamRunId"),
                value.get("revision", 1),
                encode(value),
                status,
                due_at,
                terminal_state,
            ),
        )

    def event(
        self, group_id: str, event_type: str, payload: dict[str, Any], **refs: Any
    ) -> dict[str, Any]:
        self.connection.execute(
            "INSERT INTO group_sequences(group_id,watermark) VALUES(?,1) "
            "ON CONFLICT(group_id) DO UPDATE SET watermark=group_sequences.watermark+1",
            (group_id,),
        )
        seq = self.watermark(group_id)
        event = {
            "apiVersion": API_VERSION,
            "eventId": new_id("ge"),
            "groupId": group_id,
            "groupSeq": seq,
            "type": event_type,
            "createdAt": now(),
            "payload": payload,
            **{key: value for key, value in refs.items() if value is not None},
        }
        self.connection.execute(
            "INSERT INTO group_events(group_id,seq,event_id,body) VALUES(?,?,?,?)",
            (group_id, seq, event["eventId"], encode(event)),
        )
        return event

    def watermark(self, group_id: str) -> int:
        row = self.connection.execute(
            "SELECT watermark FROM group_sequences WHERE group_id=?", (group_id,)
        ).fetchone()
        return int(row[0]) if row else 0


class TeamsStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._active: ContextVar[Any] = ContextVar("teams_sqlite_transaction", default=None)
        self._connection = sqlite3.connect(
            str(self.path), isolation_level=None, check_same_thread=False
        )
        self._connection.execute("PRAGMA busy_timeout=5000")
        # Refuse unknown schemas before any DDL or journal-mode write. A
        # maintenance rollback must not mutate a newer plugin's database.
        has_meta = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='teams_meta'"
        ).fetchone()
        if has_meta:
            version = self._connection.execute(
                "SELECT value FROM teams_meta WHERE key='schema_version'"
            ).fetchone()
            if version and version[0] != "1":
                self._connection.close()
                raise TeamsError(
                    "schema_version_unsupported", "团队数据版本不兼容，需要迁移", status=503
                )
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS teams_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS team_objects (
                ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL, object_id TEXT NOT NULL, group_id TEXT,
                team_run_id TEXT, revision INTEGER NOT NULL, body TEXT NOT NULL,
                UNIQUE(kind, object_id)
            );
            CREATE INDEX IF NOT EXISTS team_objects_scope
                ON team_objects(kind,group_id,team_run_id);
            CREATE TABLE IF NOT EXISTS group_sequences (
                group_id TEXT PRIMARY KEY, watermark INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS group_events (
                group_id TEXT NOT NULL, seq INTEGER NOT NULL, event_id TEXT NOT NULL UNIQUE,
                body TEXT NOT NULL, PRIMARY KEY(group_id,seq)
            );
            CREATE TABLE IF NOT EXISTS team_idempotency (
                scope TEXT NOT NULL, key TEXT NOT NULL, payload_digest TEXT NOT NULL,
                response TEXT NOT NULL, PRIMARY KEY(scope,key)
            );
        """)
        version = self._connection.execute(
            "SELECT value FROM teams_meta WHERE key='schema_version'"
        ).fetchone()
        if version and version[0] != "1":
            self._connection.close()
            raise TeamsError(
                "schema_version_unsupported", "团队数据版本不兼容，需要迁移", status=503
            )
        self._connection.execute("INSERT OR IGNORE INTO teams_meta VALUES('schema_version','1')")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            columns = {
                row[1] for row in self._connection.execute("PRAGMA table_info(team_objects)")
            }
            for name, definition in (
                ("status", "TEXT"),
                ("due_at", "REAL NOT NULL DEFAULT 0"),
                ("terminal_state", "TEXT"),
            ):
                if name not in columns:
                    self._connection.execute(
                        f"ALTER TABLE team_objects ADD COLUMN {name} {definition}"
                    )
            projected = self._connection.execute(
                "SELECT value FROM teams_meta WHERE key='query_projection_version'"
            ).fetchone()
            if not projected:
                for ordinal, body in self._connection.execute(
                    "SELECT ordinal,body FROM team_objects"
                ).fetchall():
                    self._connection.execute(
                        "UPDATE team_objects SET status=?,due_at=?,terminal_state=? "
                        "WHERE ordinal=?",
                        (*query_projection(json.loads(body)), ordinal),
                    )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS team_objects_due "
                "ON team_objects(kind,status,due_at,ordinal)"
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO teams_meta VALUES('query_projection_version','1')"
            )
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            self._connection.close()
            raise

    @contextmanager
    def transaction(self, *, quota_scopes: Iterable[QuotaScope] = ()) -> Iterator[Transaction]:
        if tuple(quota_scopes):
            raise TeamsError("quota_store_unsupported", "跨团队配额需要 PostgreSQL", status=503)
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        owner = (threading.get_ident(), task)
        active = self._active.get()
        if active is not None:
            if active[0] != owner:
                raise TeamsError(
                    "transaction_context_mismatch", "事务不能跨任务或线程共享", status=503
                )
            try:
                yield active[1]
            except BaseException:
                active[2] = True
                raise
            return
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            state = [owner, Transaction(self._connection), False]
            token = self._active.set(state)
            try:
                yield state[1]
                if state[2]:
                    raise TeamsError("transaction_aborted", "嵌套事务失败，操作未提交", status=503)
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            finally:
                self._active.reset(token)

    def list_page(self, kind: str, group_id: str | None = None, **kwargs: Any) -> ObjectPage:
        with self.transaction() as tx:
            return tx.list_page(kind, group_id, **kwargs)

    def list_due(self, kind: str, **kwargs: Any) -> list[dict[str, Any]]:
        with self.transaction() as tx:
            return tx.list_due(kind, **kwargs)

    def recent(
        self, kind: str, group_id: str, limit: int = 12, **kwargs: Any
    ) -> list[dict[str, Any]]:
        with self.transaction() as tx:
            return tx.recent(kind, group_id, limit, **kwargs)

    def mutate(
        self,
        scope: str,
        key: str,
        payload: Any,
        operation: Callable[[Transaction], T],
        *,
        authorize: Callable[[Transaction], Any] | None = None,
    ) -> T:
        if not isinstance(key, str) or not key.strip() or len(key) > 200:
            raise TeamsError("idempotency_key_required", "操作需要有效的幂等键", status=422)
        fingerprint = digest(payload)
        with self.transaction() as tx:
            if authorize is not None:
                authorize(tx)  # Revalidate even when returning a durable duplicate.
            previous = tx.connection.execute(
                "SELECT payload_digest,response FROM team_idempotency WHERE scope=? AND key=?",
                (scope, key),
            ).fetchone()
            if previous:
                if previous[0] != fingerprint:
                    raise TeamsError("idempotency_conflict", "相同幂等键不能用于不同请求")
                return json.loads(previous[1])
            result = operation(tx)
            tx.connection.execute(
                "INSERT INTO team_idempotency VALUES(?,?,?,?)",
                (scope, key, fingerprint, encode(result)),
            )
            return result

    def backup(self, destination: Path) -> None:
        """Create a consistent SQLite backup without replacing an existing one."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if destination.exists():
                return
            backup = sqlite3.connect(str(destination))
            try:
                self._connection.backup(backup)
            except BaseException:
                backup.close()
                destination.unlink(missing_ok=True)
                raise
            else:
                backup.close()

    def events(self, group_id: str, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        if after < 0 or not 1 <= limit <= 1000:
            raise TeamsError("invalid_cursor", "事件游标或分页大小无效", status=422)
        with self._lock:
            latest = Transaction(self._connection).watermark(group_id)
            if after > latest:
                raise TeamsError("reset_required", "事件游标失效，请重新获取团队快照")
            return [
                json.loads(row[0])
                for row in self._connection.execute(
                    "SELECT body FROM group_events WHERE group_id=? AND seq>? ORDER BY seq LIMIT ?",
                    (group_id, after, limit),
                )
            ]

    def close(self) -> None:
        with self._lock:
            self._connection.close()
