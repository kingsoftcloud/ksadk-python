"""Durable local persistence for Scheduler Lite.

This store is deliberately independent from AgentKernel's Inbox tables.  It
owns scheduling decisions, occurrence identity and its *single process*
lease; AgentControl remains the only execution ingress.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from ksadk.scheduler.contracts import (
    ScheduledTask,
    ScheduleOccurrence,
    ScheduleOccurrenceTransition,
)

_SCHEMA_VERSION = 2
_ACTIVE_STATES = ("claimed", "accepted", "running")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduler_tasks (
  task_id TEXT PRIMARY KEY,
  generation INTEGER NOT NULL,
  body_json TEXT NOT NULL,
  enabled INTEGER NOT NULL,
  next_run_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scheduler_tasks_due
  ON scheduler_tasks (enabled, next_run_at);
CREATE TABLE IF NOT EXISTS scheduler_occurrences (
  occurrence_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  generation INTEGER NOT NULL,
  scheduled_for TEXT NOT NULL,
  trigger_kind TEXT NOT NULL,
  body_json TEXT NOT NULL,
  state TEXT NOT NULL,
  command_id TEXT,
  completed_at TEXT,
  UNIQUE(task_id, generation, scheduled_for, trigger_kind)
);
CREATE INDEX IF NOT EXISTS idx_scheduler_occurrences_task_state
  ON scheduler_occurrences (task_id, state, scheduled_for DESC);
CREATE TABLE IF NOT EXISTS scheduler_leases (
  lease_name TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
"""


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def occurrence_id(
    task_id: str,
    generation: int,
    scheduled_for: datetime,
    trigger: Literal["schedule", "manual"] = "schedule",
) -> str:
    raw = f"{task_id}\x00{generation}\x00{_iso(scheduled_for)}\x00{trigger}".encode()
    return f"occ_{hashlib.sha256(raw).hexdigest()[:32]}"


def _session_id(task: ScheduledTask, *, occurrence_id_value: str) -> str:
    if task.continuity == "continue_session":
        assert task.target.session_id is not None
        return task.target.session_id
    # A new scheduled start has an independent durable Session namespace.  It
    # is derived from the immutable occurrence id so a retry cannot create a
    # second conversation.
    return f"sched-{occurrence_id_value}"


class SchedulerSQLiteStore:
    """Synchronous SQLite store; callers may use it through ``to_thread``.

    Every mutation uses its own connection and ``BEGIN IMMEDIATE``.  That is
    intentional: this component has no undeclared aiosqlite dependency and
    remains correct when multiple local Studio processes contend for its DB.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")

    def put_task(self, task: ScheduledTask, *, generation: int | None = None) -> int:
        now = _iso(datetime.now(timezone.utc))
        encoded = task.model_dump_json(by_alias=True, exclude_none=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT generation, created_at FROM scheduler_tasks WHERE task_id=?",
                (task.task_id,),
            ).fetchone()
            actual_generation = generation
            if actual_generation is None:
                actual_generation = int(existing["generation"]) + 1 if existing else 1
            created_at = existing["created_at"] if existing else _iso(task.created_at)
            connection.execute(
                """INSERT INTO scheduler_tasks(
                    task_id,generation,body_json,enabled,next_run_at,created_at,updated_at
                )
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(task_id) DO UPDATE SET generation=excluded.generation,
                  body_json=excluded.body_json,enabled=excluded.enabled,next_run_at=excluded.next_run_at,
                  updated_at=excluded.updated_at""",
                (
                    task.task_id,
                    actual_generation,
                    encoded,
                    int(task.enabled),
                    _iso(task.next_run_at) if task.next_run_at else None,
                    created_at,
                    now,
                ),
            )
            connection.commit()
        return actual_generation

    def get_task(self, task_id: str) -> tuple[ScheduledTask, int] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT body_json,generation,next_run_at,enabled "
                "FROM scheduler_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        body = json.loads(row["body_json"])
        body["nextRunAt"] = row["next_run_at"]
        body["enabled"] = bool(row["enabled"])
        return ScheduledTask.model_validate(body), int(row["generation"])

    def list_due(self, now: datetime) -> list[tuple[ScheduledTask, int]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT body_json,generation,next_run_at FROM scheduler_tasks
                   WHERE enabled=1 AND next_run_at IS NOT NULL AND next_run_at<=?
                   ORDER BY next_run_at,task_id""",
                (_iso(now),),
            ).fetchall()
        result: list[tuple[ScheduledTask, int]] = []
        for row in rows:
            body = json.loads(row["body_json"])
            body["nextRunAt"] = row["next_run_at"]
            result.append((ScheduledTask.model_validate(body), int(row["generation"])))
        return result

    def list_tasks(self) -> list[tuple[ScheduledTask, int]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT body_json,generation,next_run_at,enabled FROM scheduler_tasks "
                "ORDER BY updated_at DESC,task_id"
            ).fetchall()
        result: list[tuple[ScheduledTask, int]] = []
        for row in rows:
            body = json.loads(row["body_json"])
            body["nextRunAt"] = row["next_run_at"]
            body["enabled"] = bool(row["enabled"])
            result.append((ScheduledTask.model_validate(body), int(row["generation"])))
        return result

    def delete_task(self, task_id: str) -> bool:
        """Remove the scheduling intent but retain immutable occurrence history."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            removed = connection.execute(
                "DELETE FROM scheduler_tasks WHERE task_id=?", (task_id,)
            ).rowcount
            connection.commit()
        return removed == 1

    def list_occurrences(self, task_id: str, *, limit: int = 50) -> list[ScheduleOccurrence]:
        if limit < 1 or limit > 200:
            raise ValueError("occurrence limit must be between 1 and 200")
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT body_json FROM scheduler_occurrences WHERE task_id=?
                   ORDER BY scheduled_for DESC,occurrence_id DESC LIMIT ?""",
                (task_id, limit),
            ).fetchall()
        return [ScheduleOccurrence.model_validate_json(row["body_json"]) for row in rows]

    def list_all_occurrences(self, *, limit: int = 200) -> list[ScheduleOccurrence]:
        """Return cross-task history, including records whose task was deleted."""

        if limit < 1 or limit > 500:
            raise ValueError("occurrence limit must be between 1 and 500")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT body_json FROM scheduler_occurrences "
                "ORDER BY scheduled_for DESC,occurrence_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [ScheduleOccurrence.model_validate_json(row["body_json"]) for row in rows]

    def list_active_occurrences(self, *, limit: int = 200) -> list[ScheduleOccurrence]:
        """Return only non-terminal occurrences for event reconciliation.

        The target snapshot is stored on the occurrence itself, so deleting a
        task deliberately does not orphan a previously accepted execution.
        """

        if limit < 1 or limit > 200:
            raise ValueError("active occurrence limit must be between 1 and 200")
        placeholders = ",".join("?" for _ in _ACTIVE_STATES)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT body_json FROM scheduler_occurrences "
                f"WHERE state IN ({placeholders}) "
                "ORDER BY scheduled_for,occurrence_id LIMIT ?",
                (*_ACTIVE_STATES, limit),
            ).fetchall()
        return [ScheduleOccurrence.model_validate_json(row["body_json"]) for row in rows]

    def has_active_occurrence(self, task_id: str) -> bool:
        placeholders = ",".join("?" for _ in _ACTIVE_STATES)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM scheduler_occurrences "
                f"WHERE task_id=? AND state IN ({placeholders}) LIMIT 1",
                (task_id, *_ACTIVE_STATES),
            ).fetchone()
        return row is not None

    def claim_and_advance(
        self,
        task: ScheduledTask,
        *,
        generation: int,
        next_run_at: datetime | None,
        state: Literal["claimed", "skipped"] = "claimed",
        detail: str | None = None,
        trigger: Literal["schedule", "manual"] = "schedule",
        claimed_at: datetime | None = None,
    ) -> ScheduleOccurrence | None:
        if task.next_run_at is None:
            raise ValueError("task must have next_run_at before it can be claimed")
        scheduled_for = task.next_run_at
        now = claimed_at or datetime.now(timezone.utc)
        completed_at = now if state == "skipped" else None
        transitions = [ScheduleOccurrenceTransition(state="claimed", at=now)]
        if state == "skipped":
            transitions.append(ScheduleOccurrenceTransition(state="skipped", at=now, detail=detail))
        occurrence = ScheduleOccurrence(
            occurrence_id=occurrence_id(task.task_id, generation, scheduled_for, trigger),
            task_id=task.task_id,
            target=task.target,
            scheduled_for=scheduled_for,
            session_id=_session_id(
                task,
                occurrence_id_value=occurrence_id(task.task_id, generation, scheduled_for, trigger),
            ),
            trigger=trigger,
            state=state,
            claimed_at=now,
            detail=detail,
            completed_at=completed_at,
            transitions=tuple(transitions),
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """UPDATE scheduler_tasks SET next_run_at=?,updated_at=?
                   WHERE task_id=? AND generation=? AND next_run_at=? AND enabled=1""",
                (
                    _iso(next_run_at) if next_run_at else None,
                    _iso(now),
                    task.task_id,
                    generation,
                    _iso(scheduled_for),
                ),
            ).rowcount
            if updated != 1:
                connection.rollback()
                return None
            connection.execute(
                """INSERT OR IGNORE INTO scheduler_occurrences(
                  occurrence_id,task_id,generation,scheduled_for,trigger_kind,body_json,state,command_id,completed_at)
                  VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    occurrence.occurrence_id,
                    task.task_id,
                    generation,
                    _iso(scheduled_for),
                    trigger,
                    occurrence.model_dump_json(by_alias=True, exclude_none=True),
                    state,
                    None,
                    _iso(completed_at) if completed_at else None,
                ),
            )
            connection.commit()
        return occurrence

    def mark_accepted(
        self,
        occurrence_id_value: str,
        command_id: str,
        *,
        accepted_seq: int | None = None,
    ) -> ScheduleOccurrence:
        return self._transition(
            occurrence_id_value,
            state="accepted",
            command_id=command_id,
            accepted_seq=accepted_seq,
            last_event_seq=accepted_seq,
            accepted_at=datetime.now(timezone.utc),
        )

    def claim_manual(
        self,
        task: ScheduledTask,
        *,
        generation: int,
        now: datetime,
    ) -> ScheduleOccurrence | None:
        """Create a durable manual occurrence without changing the timetable."""

        occurrence = ScheduleOccurrence(
            occurrence_id=occurrence_id(task.task_id, generation, now, "manual"),
            task_id=task.task_id,
            target=task.target,
            scheduled_for=now,
            session_id=_session_id(
                task,
                occurrence_id_value=occurrence_id(task.task_id, generation, now, "manual"),
            ),
            trigger="manual",
            state="claimed",
            claimed_at=now,
            transitions=(ScheduleOccurrenceTransition(state="claimed", at=now),),
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT generation,enabled FROM scheduler_tasks WHERE task_id=?",
                (task.task_id,),
            ).fetchone()
            if (
                current is None
                or int(current["generation"]) != generation
                or not current["enabled"]
            ):
                connection.rollback()
                return None
            inserted = connection.execute(
                """INSERT OR IGNORE INTO scheduler_occurrences(
                    occurrence_id,task_id,generation,scheduled_for,trigger_kind,
                    body_json,state,command_id,completed_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    occurrence.occurrence_id,
                    task.task_id,
                    generation,
                    _iso(now),
                    "manual",
                    occurrence.model_dump_json(by_alias=True, exclude_none=True),
                    occurrence.state,
                    None,
                    None,
                ),
            ).rowcount
            if inserted != 1:
                connection.rollback()
                return None
            connection.commit()
        return occurrence

    def finish(
        self,
        occurrence_id_value: str,
        *,
        succeeded: bool,
        error_code: str | None = None,
        detail: str | None = None,
    ) -> ScheduleOccurrence:
        if not succeeded and not error_code:
            raise ValueError("failed scheduler occurrence requires error_code")
        return self._transition(
            occurrence_id_value,
            state="succeeded" if succeeded else "failed",
            error_code=error_code,
            detail=detail,
            completed_at=datetime.now(timezone.utc),
        )

    def mark_running(
        self,
        occurrence_id_value: str,
        *,
        run_id: str,
        last_event_seq: int,
    ) -> ScheduleOccurrence:
        return self._transition(
            occurrence_id_value,
            state="running",
            run_id=run_id,
            started_at=datetime.now(timezone.utc),
            last_event_seq=last_event_seq,
        )

    def bind_run(
        self,
        occurrence_id_value: str,
        *,
        run_id: str,
        last_event_seq: int,
    ) -> ScheduleOccurrence:
        """Record a durable run identity before that run becomes runnable."""

        return self._transition(
            occurrence_id_value,
            run_id=run_id,
            last_event_seq=last_event_seq,
        )

    def advance_reconciliation_cursor(
        self, occurrence_id_value: str, *, last_event_seq: int
    ) -> ScheduleOccurrence:
        return self._transition(
            occurrence_id_value,
            last_event_seq=last_event_seq,
        )

    def _transition(self, occurrence_id_value: str, **updates: object) -> ScheduleOccurrence:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT body_json,state FROM scheduler_occurrences WHERE occurrence_id=?",
                (occurrence_id_value,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(occurrence_id_value)
            body = json.loads(row["body_json"])
            current = ScheduleOccurrence.model_validate(body)
            terminal = {"succeeded", "failed", "skipped", "cancelled"}
            # Terminal facts are first-wins.  A replayed SSE frame or a later
            # contradictory event must never rewrite auditable scheduler
            # history after it has reached a conclusion.
            if current.state in terminal:
                connection.rollback()
                return current
            current_seq = current.last_event_seq
            requested_seq = updates.get("last_event_seq")
            if (
                isinstance(requested_seq, int)
                and current_seq is not None
                and requested_seq < current_seq
            ):
                connection.rollback()
                return current
            # Stored JSON uses the public camelCase aliases.  Updating with a
            # Python field name while an older alias is present would create
            # two values for one field and Pydantic correctly rejects it as an
            # ambiguous/extra input.  Normalize every mutation at this one
            # persistence boundary instead.
            for key, value in updates.items():
                if value is None:
                    continue
                field = ScheduleOccurrence.model_fields.get(key)
                encoded_key = field.alias if field and field.alias else key
                body[encoded_key] = _iso(value) if isinstance(value, datetime) else value
            requested_state = updates.get("state")
            if isinstance(requested_state, str) and requested_state != current.state:
                transition_at = next(
                    (
                        value
                        for name in ("completed_at", "started_at", "accepted_at")
                        if isinstance((value := updates.get(name)), datetime)
                    ),
                    datetime.now(timezone.utc),
                )
                transitions = list(current.transitions)
                if transitions and transition_at < transitions[-1].at:
                    # Custom/test clocks and small host clock corrections must
                    # not make a durable audit timeline go backwards.
                    transition_at = transitions[-1].at
                transitions.append(
                    ScheduleOccurrenceTransition(
                        state=requested_state,  # type: ignore[arg-type]
                        at=transition_at,
                        detail=(str(updates["detail"]) if updates.get("detail") else None),
                        error_code=(
                            str(updates["error_code"]) if updates.get("error_code") else None
                        ),
                    )
                )
                body["transitions"] = [
                    item.model_dump(mode="json", by_alias=True, exclude_none=True)
                    for item in transitions
                ]
            occurrence = ScheduleOccurrence.model_validate(body)
            connection.execute(
                """UPDATE scheduler_occurrences SET body_json=?,state=?,command_id=?,completed_at=?
                   WHERE occurrence_id=?""",
                (
                    occurrence.model_dump_json(by_alias=True, exclude_none=True),
                    occurrence.state,
                    occurrence.command_id,
                    _iso(occurrence.completed_at) if occurrence.completed_at else None,
                    occurrence_id_value,
                ),
            )
            connection.commit()
        return occurrence

    def acquire_lease(self, *, owner_id: str, now: datetime, ttl_seconds: int = 30) -> bool:
        expiry = now.astimezone(timezone.utc).timestamp() + ttl_seconds
        expires_at = datetime.fromtimestamp(expiry, tz=timezone.utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner_id,expires_at FROM scheduler_leases "
                "WHERE lease_name='local-scheduler'"
            ).fetchone()
            allowed = row is None or _parse(row["expires_at"]) <= now or row["owner_id"] == owner_id
            if allowed:
                connection.execute(
                    """INSERT INTO scheduler_leases(lease_name,owner_id,expires_at)
                    VALUES('local-scheduler',?,?)
                    ON CONFLICT(lease_name) DO UPDATE SET
                      owner_id=excluded.owner_id,expires_at=excluded.expires_at""",
                    (owner_id, _iso(expires_at)),
                )
                connection.commit()
                return True
            connection.rollback()
        return False


__all__ = ["SchedulerSQLiteStore", "occurrence_id"]
