"""Durable local outbox for AgentEngine A2A task event batches."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENV_A2A_EVENT_OUTBOX_PATH = "KSADK_A2A_EVENT_OUTBOX_PATH"
DEFAULT_A2A_EVENT_OUTBOX_PATH = ".agentengine/a2a_event_outbox.sqlite3"


@dataclass(frozen=True)
class A2ATaskEventBatch:
    sequence: int
    batch_id: str
    platform_task_id: str
    events: list[dict[str, Any]]
    attempt_count: int = 0


class A2ATaskEventOutbox(ABC):
    """Persists task-sink batches until AgentEngine acknowledges them."""

    @abstractmethod
    async def initialize(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def ensure_writable(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def enqueue(
        self,
        *,
        platform_task_id: str,
        events: list[dict[str, Any]],
    ) -> str:
        raise NotImplementedError

    @abstractmethod
    async def pending(self, *, limit: int = 100) -> list[A2ATaskEventBatch]:
        raise NotImplementedError

    @abstractmethod
    async def acknowledge(self, batch_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def record_failure(self, batch_id: str, error: str) -> None:
        raise NotImplementedError


def _serialize_events(events: list[dict[str, Any]]) -> str:
    return json.dumps(events, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _batch_id(platform_task_id: str, events_json: str) -> str:
    digest = hashlib.sha256(f"{platform_task_id}:{events_json}".encode("utf-8")).hexdigest()
    return f"a2a-event-batch-{digest}"


class InMemoryA2ATaskEventOutbox(A2ATaskEventOutbox):
    """Test/local adapter; production Runtime deployments should use SQLite."""

    def __init__(self) -> None:
        self._batches: dict[str, A2ATaskEventBatch] = {}
        self._lock = asyncio.Lock()
        self._next_sequence = 0

    async def initialize(self) -> None:
        return None

    async def ensure_writable(self) -> None:
        return None

    async def enqueue(
        self,
        *,
        platform_task_id: str,
        events: list[dict[str, Any]],
    ) -> str:
        events_json = _serialize_events(events)
        batch_id = _batch_id(platform_task_id, events_json)
        async with self._lock:
            if batch_id not in self._batches:
                self._next_sequence += 1
                self._batches[batch_id] = A2ATaskEventBatch(
                    sequence=self._next_sequence,
                    batch_id=batch_id,
                    platform_task_id=platform_task_id,
                    events=json.loads(events_json),
                )
        return batch_id

    async def pending(self, *, limit: int = 100) -> list[A2ATaskEventBatch]:
        async with self._lock:
            return sorted(self._batches.values(), key=lambda batch: batch.sequence)[:limit]

    async def acknowledge(self, batch_id: str) -> None:
        async with self._lock:
            self._batches.pop(batch_id, None)

    async def record_failure(self, batch_id: str, error: str) -> None:
        async with self._lock:
            batch = self._batches.get(batch_id)
            if batch is not None:
                self._batches[batch_id] = A2ATaskEventBatch(
                    sequence=batch.sequence,
                    batch_id=batch.batch_id,
                    platform_task_id=batch.platform_task_id,
                    events=batch.events,
                    attempt_count=batch.attempt_count + 1,
                )


class SQLiteA2ATaskEventOutbox(A2ATaskEventOutbox):
    """SQLite adapter with stable batch IDs and crash-safe acknowledgement."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        configured = path or os.getenv(ENV_A2A_EVENT_OUTBOX_PATH) or DEFAULT_A2A_EVENT_OUTBOX_PATH
        self._path = Path(configured).expanduser()
        self._initialized = False
        self._init_lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True

    def _initialize_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path, timeout=5)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS a2a_task_event_outbox (
                    sequence INTEGER NOT NULL,
                    batch_id TEXT PRIMARY KEY,
                    platform_task_id TEXT NOT NULL,
                    events_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                )
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(a2a_task_event_outbox)").fetchall()
            }
            if "sequence" not in columns:
                connection.execute("ALTER TABLE a2a_task_event_outbox ADD COLUMN sequence INTEGER")
                connection.execute(
                    "UPDATE a2a_task_event_outbox SET sequence = rowid WHERE sequence IS NULL"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS a2a_task_event_outbox_sequence (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT
                )
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO a2a_task_event_outbox_sequence(sequence)
                SELECT sequence FROM a2a_task_event_outbox
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_a2a_task_event_outbox_created
                ON a2a_task_event_outbox(sequence)
                """
            )
            connection.commit()
        finally:
            connection.close()
        os.chmod(self._path, 0o600)

    async def enqueue(
        self,
        *,
        platform_task_id: str,
        events: list[dict[str, Any]],
    ) -> str:
        await self.initialize()
        events_json = _serialize_events(events)
        batch_id = _batch_id(platform_task_id, events_json)
        await asyncio.to_thread(
            self._enqueue_sync,
            batch_id,
            platform_task_id,
            events_json,
        )
        return batch_id

    async def ensure_writable(self) -> None:
        await self.initialize()
        await asyncio.to_thread(self._ensure_writable_sync)

    async def pending(self, *, limit: int = 100) -> list[A2ATaskEventBatch]:
        await self.initialize()
        rows = await asyncio.to_thread(self._pending_sync, limit)
        return [
            A2ATaskEventBatch(
                sequence=int(row[0]),
                batch_id=str(row[1]),
                platform_task_id=str(row[2]),
                events=list(json.loads(str(row[3]))),
                attempt_count=int(row[4]),
            )
            for row in rows
        ]

    async def acknowledge(self, batch_id: str) -> None:
        await self.initialize()
        await asyncio.to_thread(
            self._execute,
            "DELETE FROM a2a_task_event_outbox WHERE batch_id = ?",
            (batch_id,),
        )

    async def record_failure(self, batch_id: str, error: str) -> None:
        await self.initialize()
        await asyncio.to_thread(
            self._execute,
            """
            UPDATE a2a_task_event_outbox
            SET attempt_count = attempt_count + 1, last_error = ?
            WHERE batch_id = ?
            """,
            (error[:512], batch_id),
        )

    def _execute(self, statement: str, params: tuple[Any, ...]) -> None:
        connection = sqlite3.connect(self._path, timeout=5)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(statement, params)
            connection.commit()
        finally:
            connection.close()

    def _enqueue_sync(self, batch_id: str, platform_task_id: str, events_json: str) -> None:
        connection = sqlite3.connect(self._path, timeout=5, isolation_level=None)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM a2a_task_event_outbox WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    "INSERT INTO a2a_task_event_outbox_sequence DEFAULT VALUES"
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("SQLite did not return an outbox sequence")
                sequence = int(cursor.lastrowid)
                connection.execute(
                    """
                    INSERT INTO a2a_task_event_outbox(
                        sequence, batch_id, platform_task_id, events_json
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (sequence, batch_id, platform_task_id, events_json),
                )
            connection.execute("COMMIT")
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _ensure_writable_sync(self) -> None:
        connection = sqlite3.connect(self._path, timeout=5, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ROLLBACK")
        finally:
            connection.close()

    def _pending_sync(self, limit: int) -> list[tuple[Any, ...]]:
        connection = sqlite3.connect(self._path, timeout=5)
        try:
            return list(
                connection.execute(
                    """
                    SELECT sequence, batch_id, platform_task_id, events_json, attempt_count
                    FROM a2a_task_event_outbox
                    ORDER BY sequence
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            )
        finally:
            connection.close()


__all__ = [
    "A2ATaskEventBatch",
    "A2ATaskEventOutbox",
    "DEFAULT_A2A_EVENT_OUTBOX_PATH",
    "ENV_A2A_EVENT_OUTBOX_PATH",
    "InMemoryA2ATaskEventOutbox",
    "SQLiteA2ATaskEventOutbox",
]
