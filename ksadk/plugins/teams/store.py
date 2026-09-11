"""Transactional SQLite companion owned and versioned by the Teams plugin.

No Kernel inbox/run/lease records live here. Messages, routing decisions,
outbox and group events commit together. All writes use BEGIN IMMEDIATE;
idempotency receipts are in that same transaction, including their payload
digest. The runtime separately acquires single-authority process ownership.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar
from uuid import uuid4

from .contracts import API_VERSION
from .errors import TeamsError

T = TypeVar("T")
KINDS = frozenset(
    {
        "group",
        "group_revision",
        "member",
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

    def recent(self, kind: str, group_id: str, limit: int = 12) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT body FROM team_objects WHERE kind=? AND group_id=? "
            "ORDER BY ordinal DESC LIMIT ?",
            (kind, group_id, limit),
        ).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

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
        self.connection.execute(
            "INSERT INTO team_objects(kind,object_id,group_id,team_run_id,revision,body) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(kind,object_id) DO UPDATE SET group_id=excluded.group_id,"
            "team_run_id=excluded.team_run_id,revision=excluded.revision,body=excluded.body",
            (
                kind,
                key,
                value.get("groupId"),
                value.get("teamRunId"),
                value.get("revision", 1),
                encode(value),
            ),
        )

    def event(
        self, group_id: str, event_type: str, payload: dict[str, Any], **refs: Any
    ) -> dict[str, Any]:
        self.connection.execute(
            "INSERT INTO group_sequences(group_id,watermark) VALUES(?,1) "
            "ON CONFLICT(group_id) DO UPDATE SET watermark=watermark+1",
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

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield Transaction(self._connection)
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise

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
