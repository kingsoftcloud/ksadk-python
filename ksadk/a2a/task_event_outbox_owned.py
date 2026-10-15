"""Runtime-owned PostgreSQL event delivery, isolated from legacy consumers."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import text

from ksadk.a2a.task_event_outbox import (
    A2ATaskEventBatch,
    A2ATaskEventOutbox,
    _batch_id,
    _serialize_events,
)


class OwnedPostgresA2ATaskEventOutbox(A2ATaskEventOutbox):
    """Replicas share their runtime's queue, never another workload's queue.

    Separate tables are intentional: an older unscoped dispatcher must not
    consume new owned batches during a rolling upgrade. Legacy rows are kept
    untouched; their owner cannot safely be inferred from their event payload.
    """

    def __init__(self, engine, *, account_id: str, tenant_id: str, runtime_id: str):
        identity = (account_id, tenant_id, runtime_id)
        if any(not isinstance(value, str) or not value.strip() for value in identity):
            raise ValueError("A2A_OUTBOX_OWNER_REQUIRED")
        self._engine = engine
        self._owner = hashlib.sha256(
            json.dumps(identity, separators=(",", ":")).encode()
        ).hexdigest()

    async def initialize(self):
        async with self._engine.begin() as connection:
            await connection.execute(text("""
                CREATE TABLE IF NOT EXISTS ksadk_a2a_owned_task_event_outbox (
                    sequence BIGSERIAL PRIMARY KEY,
                    owner_key TEXT NOT NULL,
                    batch_id TEXT NOT NULL UNIQUE,
                    platform_task_id TEXT NOT NULL,
                    events_json TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                )
            """))
            await connection.execute(text("""
                CREATE TABLE IF NOT EXISTS ksadk_a2a_owned_task_event_dead_letters (
                    sequence BIGINT NOT NULL,
                    owner_key TEXT NOT NULL,
                    batch_id TEXT PRIMARY KEY,
                    platform_task_id TEXT NOT NULL,
                    events_json TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    last_error TEXT NOT NULL,
                    dead_lettered_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """))
            await connection.execute(text("""
                CREATE INDEX IF NOT EXISTS ix_ksadk_a2a_owned_outbox_owner_sequence
                ON ksadk_a2a_owned_task_event_outbox(owner_key, sequence)
            """))

    async def ensure_writable(self):
        await self.initialize()
        async with self._engine.begin() as connection:
            await connection.execute(text(
                "LOCK TABLE ksadk_a2a_owned_task_event_outbox IN ROW EXCLUSIVE MODE"
            ))

    async def enqueue(self, *, platform_task_id, events):
        payload = _serialize_events(events)
        batch_id = _batch_id(self._owner + ":" + platform_task_id, payload)
        async with self._engine.begin() as connection:
            await connection.execute(text("""
                INSERT INTO ksadk_a2a_owned_task_event_outbox(
                    owner_key, batch_id, platform_task_id, events_json
                ) VALUES (:owner, :batch, :task, :events)
                ON CONFLICT(batch_id) DO NOTHING
            """), {"owner": self._owner, "batch": batch_id,
                   "task": platform_task_id, "events": payload})
        return batch_id

    async def pending(self, *, limit=100):
        async with self._engine.connect() as connection:
            result = await connection.execute(text("""
                SELECT sequence, batch_id, platform_task_id, events_json, attempt_count
                FROM ksadk_a2a_owned_task_event_outbox
                WHERE owner_key = :owner ORDER BY sequence LIMIT :limit
            """), {"owner": self._owner, "limit": limit})
            return [A2ATaskEventBatch(
                sequence=int(row[0]), batch_id=str(row[1]), platform_task_id=str(row[2]),
                events=list(json.loads(row[3])), attempt_count=int(row[4]),
            ) for row in result]

    async def acknowledge(self, batch_id):
        async with self._engine.begin() as connection:
            await connection.execute(text("""
                DELETE FROM ksadk_a2a_owned_task_event_outbox
                WHERE owner_key = :owner AND batch_id = :batch
            """), {"owner": self._owner, "batch": batch_id})

    async def record_failure(self, batch_id, error):
        async with self._engine.begin() as connection:
            await connection.execute(text("""
                UPDATE ksadk_a2a_owned_task_event_outbox
                SET attempt_count = attempt_count + 1, last_error = :error
                WHERE owner_key = :owner AND batch_id = :batch
            """), {"owner": self._owner, "batch": batch_id, "error": error[:512]})

    async def dead_letter(self, batch_id, error):
        async with self._engine.begin() as connection:
            params = {"owner": self._owner, "batch": batch_id, "error": error[:512]}
            await connection.execute(text("""
                INSERT INTO ksadk_a2a_owned_task_event_dead_letters(
                    sequence, owner_key, batch_id, platform_task_id, events_json,
                    created_at, attempt_count, last_error
                ) SELECT sequence, owner_key, batch_id, platform_task_id, events_json,
                         created_at, attempt_count, :error
                  FROM ksadk_a2a_owned_task_event_outbox
                  WHERE owner_key = :owner AND batch_id = :batch
                ON CONFLICT(batch_id) DO NOTHING
            """), params)
            await connection.execute(text("""
                DELETE FROM ksadk_a2a_owned_task_event_outbox
                WHERE owner_key = :owner AND batch_id = :batch
            """), {"owner": self._owner, "batch": batch_id})
