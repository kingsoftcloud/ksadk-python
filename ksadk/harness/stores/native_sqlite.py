"""SQLite RunStore with transactional CAS, fencing and append-only event rows.

No external side effect executes inside these transactions. A prepared intent
is committed first, then claimed once. A running intent after restart needs
reconciliation; claiming it again is explicitly forbidden.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from time import time
from typing import Iterator

from ksadk.harness.native.fork import ForkCreation
from ksadk.harness.native.restore_contract import SNAPSHOT_READER_VERSION, StoreRestoreAudit
from ksadk.harness.native.state import (
    CommandReceipt,
    CommitBatch,
    EffectIntent,
    EffectReceipt,
    NativeEvent,
    RunCommand,
    RunScope,
    RunSnapshot,
)

from .native_command_settlement import settle_terminal_commands
from .native_contract import (
    EffectAlreadyClaimed,
    LeaseBusy,
    PendingCommandConflict,
    ScopeMismatch,
    StoreConflict,
    check_commit,
    check_fence,
    check_fork,
    check_initial,
    check_intent,
    check_receipt,
    check_scope,
    check_turn,
    detached,
    duplicate_fork,
    encode,
)
from .native_sqlite_audit import (
    CONSERVATIVE_MIGRATION_ID,
    historical_migrations,
    initialize_audit_tables,
)
from .native_sqlite_migration import _fingerprint, restore_legacy_session_heads

_SCHEMA = """
CREATE TABLE IF NOT EXISTS native_runs (
    scope TEXT NOT NULL, run_id TEXT NOT NULL, snapshot TEXT NOT NULL,
    fence INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (scope, run_id)
);
CREATE INDEX IF NOT EXISTS native_runs_id ON native_runs(run_id);
CREATE TABLE IF NOT EXISTS native_session_heads (
    scope TEXT NOT NULL, session_id TEXT NOT NULL, run_id TEXT NOT NULL,
    PRIMARY KEY (scope, session_id),
    FOREIGN KEY (scope, run_id) REFERENCES native_runs(scope, run_id)
);
CREATE TABLE IF NOT EXISTS native_events (
    scope TEXT NOT NULL, run_id TEXT NOT NULL, seq INTEGER NOT NULL,
    event_id TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (scope, run_id, seq), UNIQUE (scope, run_id, event_id),
    FOREIGN KEY (scope, run_id) REFERENCES native_runs(scope, run_id)
);
CREATE TABLE IF NOT EXISTS native_outbox_cursors (
    scope TEXT NOT NULL, run_id TEXT NOT NULL, consumer TEXT NOT NULL,
    cursor INTEGER NOT NULL DEFAULT 0 CHECK (cursor >= 0),
    PRIMARY KEY (scope, run_id, consumer),
    FOREIGN KEY (scope, run_id) REFERENCES native_runs(scope, run_id)
);
CREATE TABLE IF NOT EXISTS native_run_leases (
    scope TEXT NOT NULL, run_id TEXT NOT NULL, owner_id TEXT NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (scope, run_id),
    FOREIGN KEY (scope, run_id) REFERENCES native_runs(scope, run_id)
);
CREATE TABLE IF NOT EXISTS native_intents (
    scope TEXT NOT NULL, run_id TEXT NOT NULL, operation_key TEXT NOT NULL,
    data TEXT NOT NULL, PRIMARY KEY (scope, run_id, operation_key),
    FOREIGN KEY (scope, run_id) REFERENCES native_runs(scope, run_id)
);
CREATE TABLE IF NOT EXISTS native_receipts (
    scope TEXT NOT NULL, run_id TEXT NOT NULL, operation_key TEXT NOT NULL,
    data TEXT NOT NULL, PRIMARY KEY (scope, run_id, operation_key),
    FOREIGN KEY (scope, run_id, operation_key)
        REFERENCES native_intents(scope, run_id, operation_key)
);
CREATE TABLE IF NOT EXISTS native_commands (
    enqueue_order INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL, run_id TEXT NOT NULL, command_id TEXT NOT NULL,
    priority INTEGER NOT NULL, data TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0,
    UNIQUE (scope, run_id, command_id),
    FOREIGN KEY (scope, run_id) REFERENCES native_runs(scope, run_id)
);
CREATE INDEX IF NOT EXISTS native_commands_pending
    ON native_commands(scope, run_id, consumed, priority DESC, enqueue_order);
CREATE TABLE IF NOT EXISTS native_commits (
    scope TEXT NOT NULL, run_id TEXT NOT NULL, commit_id TEXT NOT NULL,
    payload TEXT NOT NULL, snapshot TEXT NOT NULL,
    PRIMARY KEY (scope, run_id, commit_id),
    FOREIGN KEY (scope, run_id) REFERENCES native_runs(scope, run_id)
);
"""


class SQLiteRunStore:
    def __init__(self, path: str | Path) -> None:
        self._lock = RLock()
        self._closed = False
        self._executed_migration_ids: tuple[str, ...] = ()
        self._connection = sqlite3.connect(
            str(path), timeout=5, isolation_level=None, check_same_thread=False
        )
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
            # Inspect and migrate under the same SQLite writer lock. Another
            # process's completed upgrade must not be recorded as our work.
            self._connection.execute("BEGIN IMMEDIATE")
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1, 2}:
                raise StoreConflict("unsupported native SQLite schema version")
            if version < 2:
                # Version 0 is the original unmarked schema. DDL and the
                # marker commit together; an interrupted upgrade rolls back.
                prior_runs = (
                    self._connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_runs'"
                    ).fetchone()
                    is not None
                )
                source_digest = _fingerprint(self._connection) if prior_runs else None
                # This is our fixed DDL, with no literal semicolons. Unlike
                # executescript, execute does not commit the inspection lock.
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        self._connection.execute(statement)
                restore_legacy_session_heads(self._connection)
                self._connection.execute("PRAGMA user_version = 2")
                initialize_audit_tables(self._connection)
                if prior_runs:
                    self._connection.execute(
                        "INSERT INTO native_store_migrations VALUES (?,?,?,?)",
                        (CONSERVATIVE_MIGRATION_ID, SNAPSHOT_READER_VERSION, source_digest, "[]"),
                    )
                    self._executed_migration_ids = (CONSERVATIVE_MIGRATION_ID,)
            else:
                initialize_audit_tables(self._connection)
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            self._connection.close()
            self._closed = True
            raise

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise StoreConflict("store is closed")
            connection = self._connection
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield connection
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise StoreConflict("persistent uniqueness or integrity conflict") from exc
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _row(connection: sqlite3.Connection, scope: str, run_id: str) -> tuple[str, int]:
        row = connection.execute(
            "SELECT snapshot, fence FROM native_runs WHERE scope = ? AND run_id = ?",
            (scope, run_id),
        ).fetchone()
        if row is None:
            if connection.execute(
                "SELECT 1 FROM native_runs WHERE run_id = ? LIMIT 1", (run_id,)
            ).fetchone():
                raise ScopeMismatch("run authorization scope mismatch")
            raise KeyError("run not found")
        return row

    def create(self, snapshot: RunSnapshot) -> None:
        snapshot = detached(snapshot)
        check_initial(snapshot)
        if snapshot.fork_source is not None or snapshot.fork_command is not None:
            raise StoreConflict("fork requires atomic branch creation")
        with self._transaction(write=True) as connection:
            connection.execute(
                "INSERT INTO native_runs(scope, run_id, snapshot) VALUES (?, ?, ?)",
                (encode(snapshot.scope), snapshot.run_id, encode(snapshot)),
            )

    def load(self, scope: RunScope, run_id: str) -> RunSnapshot:
        with self._transaction() as connection:
            row = self._row(connection, encode(scope), run_id)
            return RunSnapshot.model_validate_json(row[0])

    def record_restore(
        self,
        scope: RunScope,
        run_id: str,
        *,
        reader_version: str,
        configuration_digest: str,
        fencing_token: int | None,
    ) -> None:
        with self._transaction(write=True) as connection:
            raw, fence = self._row(connection, encode(scope), run_id)
            snapshot = RunSnapshot.model_validate_json(raw)
            check_scope(scope, snapshot)
            if snapshot.configuration_digest != configuration_digest:
                raise StoreConflict("restore configuration binding mismatch")
            if fencing_token is not None:
                check_fence(fence, fencing_token)
            audit = StoreRestoreAudit(
                reader_version=reader_version,
                snapshot_schema_version=snapshot.schema_version,
                configuration_digest=configuration_digest,
                fencing_token=fencing_token,
                executed_migration_ids=self._executed_migration_ids,
                historical_migration_ids=historical_migrations(connection),
            )
            connection.execute(
                "INSERT INTO native_restore_audits(scope,run_id,data) VALUES (?,?,?)",
                (encode(scope), run_id, encode(audit)),
            )

    def restore_audits(self, scope: RunScope, run_id: str) -> tuple[StoreRestoreAudit, ...]:
        with self._transaction() as connection:
            self._row(connection, encode(scope), run_id)
            return tuple(
                StoreRestoreAudit.model_validate_json(row[0])
                for row in connection.execute(
                    "SELECT data FROM native_restore_audits "
                    "WHERE scope=? AND run_id=? ORDER BY audit_id",
                    (encode(scope), run_id),
                )
            )

    def session_head(self, scope: RunScope, session_id: str) -> RunSnapshot | None:
        with self._transaction() as connection:
            return self._session_head(connection, encode(scope), session_id)

    def _session_head(
        self, connection: sqlite3.Connection, scope: str, session_id: str
    ) -> RunSnapshot | None:
        row = connection.execute(
            "SELECT run_id FROM native_session_heads WHERE scope = ? AND session_id = ?",
            (scope, session_id),
        ).fetchone()
        return (
            RunSnapshot.model_validate_json(self._row(connection, scope, row[0])[0])
            if row
            else None
        )

    def create_turn(self, snapshot: RunSnapshot, *, expected_parent_run_id: str | None) -> None:
        snapshot = detached(snapshot)
        key = encode(snapshot.scope)
        with self._transaction(write=True) as connection:
            head = self._session_head(connection, key, snapshot.session_id)
            check_turn(snapshot, head, expected_parent_run_id)
            connection.execute(
                "INSERT INTO native_runs(scope, run_id, snapshot) VALUES (?, ?, ?)",
                (key, snapshot.run_id, encode(snapshot)),
            )
            connection.execute(
                "INSERT INTO native_session_heads(scope, session_id, run_id) VALUES (?, ?, ?) "
                "ON CONFLICT(scope, session_id) DO UPDATE SET run_id = excluded.run_id",
                (key, snapshot.session_id, snapshot.run_id),
            )

    def create_fork(self, snapshot: RunSnapshot) -> ForkCreation:
        snapshot = detached(snapshot)
        key = encode(snapshot.scope)
        with self._transaction(write=True) as connection:
            ref = snapshot.fork_source
            if ref is None:
                raise StoreConflict("fork source boundary required")
            source = RunSnapshot.model_validate_json(
                self._row(connection, key, ref.source_run_id)[0]
            )
            check_fork(snapshot, source)
            if snapshot.fork_command is not None:
                prior = connection.execute(
                    "SELECT snapshot FROM native_runs WHERE scope=? AND run_id=?",
                    (key, snapshot.run_id),
                ).fetchone()
                if prior is not None:
                    return duplicate_fork(snapshot, RunSnapshot.model_validate_json(prior[0]))
            if self._session_head(connection, key, snapshot.session_id) is not None:
                raise StoreConflict("fork destination already exists")
            connection.execute(
                "INSERT INTO native_runs(scope, run_id, snapshot) VALUES (?, ?, ?)",
                (key, snapshot.run_id, encode(snapshot)),
            )
            connection.execute(
                "INSERT INTO native_session_heads(scope, session_id, run_id) VALUES (?, ?, ?)",
                (key, snapshot.session_id, snapshot.run_id),
            )
            return ForkCreation(snapshot=snapshot, created=True)

    def acquire(self, scope: RunScope, run_id: str) -> int:
        key = encode(scope)
        with self._transaction(write=True) as connection:
            _, fence = self._row(connection, key, run_id)
            lease = connection.execute(
                "SELECT expires_at FROM native_run_leases WHERE scope = ? AND run_id = ?",
                (key, run_id),
            ).fetchone()
            if lease is not None:
                if lease[0] > time():
                    raise LeaseBusy("run has an active owner lease")
                connection.execute(
                    "DELETE FROM native_run_leases WHERE scope = ? AND run_id = ?",
                    (key, run_id),
                )
            connection.execute(
                "UPDATE native_runs SET fence = ? WHERE scope = ? AND run_id = ?",
                (fence + 1, key, run_id),
            )
            return fence + 1

    @staticmethod
    def _lease_args(owner_id: str, ttl_seconds: float) -> None:
        if not isinstance(owner_id, str) or not owner_id.strip() or len(owner_id) > 160:
            raise ValueError("lease owner must be a nonempty bounded ID")
        if type(ttl_seconds) not in {int, float} or not 0 < ttl_seconds <= 3600:
            raise ValueError("lease TTL must be positive and at most one hour")

    def acquire_lease(
        self, scope: RunScope, run_id: str, *, owner_id: str, ttl_seconds: float
    ) -> int:
        """Claim an unowned/expired run; an active owner keeps its epoch."""
        self._lease_args(owner_id, ttl_seconds)
        key = encode(scope)
        with self._transaction(write=True) as connection:
            _, fence = self._row(connection, key, run_id)
            now = time()
            lease = connection.execute(
                "SELECT owner_id, expires_at FROM native_run_leases WHERE scope = ? AND run_id = ?",
                (key, run_id),
            ).fetchone()
            if lease is not None and lease[1] > now:
                if lease[0] != owner_id:
                    raise LeaseBusy("run has an active owner lease")
                connection.execute(
                    "UPDATE native_run_leases SET expires_at = ? WHERE scope = ? AND run_id = ?",
                    (now + ttl_seconds, key, run_id),
                )
                return fence
            next_fence = fence + 1
            connection.execute(
                "UPDATE native_runs SET fence = ? WHERE scope = ? AND run_id = ?",
                (next_fence, key, run_id),
            )
            connection.execute(
                "INSERT INTO native_run_leases(scope, run_id, owner_id, expires_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(scope, run_id) DO UPDATE SET "
                "owner_id = excluded.owner_id, expires_at = excluded.expires_at",
                (key, run_id, owner_id, now + ttl_seconds),
            )
            return next_fence

    def renew_lease(
        self,
        scope: RunScope,
        run_id: str,
        *,
        owner_id: str,
        fencing_token: int,
        ttl_seconds: float,
    ) -> None:
        self._lease_args(owner_id, ttl_seconds)
        key = encode(scope)
        with self._transaction(write=True) as connection:
            _, fence = self._row(connection, key, run_id)
            check_fence(fence, fencing_token)
            now = time()
            changed = connection.execute(
                "UPDATE native_run_leases SET expires_at = ? "
                "WHERE scope = ? AND run_id = ? AND owner_id = ? AND expires_at > ?",
                (now + ttl_seconds, key, run_id, owner_id, now),
            ).rowcount
            if changed != 1:
                raise StoreConflict("run lease expired or changed owner")

    def release_lease(
        self, scope: RunScope, run_id: str, *, owner_id: str, fencing_token: int
    ) -> None:
        key = encode(scope)
        with self._transaction(write=True) as connection:
            _, fence = self._row(connection, key, run_id)
            check_fence(fence, fencing_token)
            changed = connection.execute(
                "DELETE FROM native_run_leases WHERE scope = ? AND run_id = ? AND owner_id = ?",
                (key, run_id, owner_id),
            ).rowcount
            if changed != 1:
                raise StoreConflict("run lease is not owned by caller")
            # Revocation must fence even a previously successful commit replay.
            connection.execute(
                "UPDATE native_runs SET fence = ? WHERE scope = ? AND run_id = ?",
                (fence + 1, key, run_id),
            )

    @staticmethod
    def _check_live_lease(connection: sqlite3.Connection, key: str, run_id: str) -> None:
        lease = connection.execute(
            "SELECT expires_at FROM native_run_leases WHERE scope = ? AND run_id = ?",
            (key, run_id),
        ).fetchone()
        if lease is not None and lease[0] <= time():
            raise StoreConflict("run lease expired")

    def enqueue(self, scope: RunScope, run_id: str, command: RunCommand) -> CommandReceipt:
        command = detached(command)
        key, raw = encode(scope), encode(command)
        with self._transaction(write=True) as connection:
            saved, _ = self._row(connection, key, run_id)
            existing = connection.execute(
                "SELECT data FROM native_commands "
                "WHERE scope = ? AND run_id = ? AND command_id = ?",
                (key, run_id, command.command_id),
            ).fetchone()
            if existing is not None:
                if existing[0] != raw:
                    raise StoreConflict("command ID payload mismatch")
                return CommandReceipt(command_id=command.command_id, status="duplicate")
            if RunSnapshot.model_validate_json(saved).phase.terminal:
                return CommandReceipt(
                    command_id=command.command_id, status="rejected", reason="run is terminal"
                )
            connection.execute(
                "INSERT INTO native_commands(scope, run_id, command_id, priority, data) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, run_id, command.command_id, command.priority, raw),
            )
            return CommandReceipt(command_id=command.command_id, status="accepted")

    def commands(self, scope: RunScope, run_id: str) -> tuple[RunCommand, ...]:
        key = encode(scope)
        with self._transaction() as connection:
            self._row(connection, key, run_id)
            rows = connection.execute(
                "SELECT data FROM native_commands WHERE scope = ? AND run_id = ? AND consumed = 0 "
                "ORDER BY priority DESC, enqueue_order ASC",
                (key, run_id),
            ).fetchall()
            return tuple(RunCommand.model_validate_json(row[0]) for row in rows)

    def commit(
        self, scope: RunScope, batch: CommitBatch, *, expected_revision: int, fencing_token: int
    ) -> RunSnapshot:
        return self._commit(scope, batch, expected_revision=expected_revision,
                            fencing_token=fencing_token)

    def commit_child_input(self, scope, batch, *, expected_revision, fencing_token, delivery):
        return self._commit(scope, batch, expected_revision=expected_revision,
                            fencing_token=fencing_token, delivery=detached(delivery))

    def _commit(self, scope, batch, *, expected_revision, fencing_token, delivery=None):
        batch = detached(batch)
        check_scope(scope, batch.snapshot)
        key, run_id, raw = encode(scope), batch.snapshot.run_id, encode(batch)
        if delivery is not None:
            raw += "\n" + encode(delivery)
        with self._transaction(write=True) as connection:
            current_raw, fence = self._row(connection, key, run_id)
            check_fence(fence, fencing_token)
            self._check_live_lease(connection, key, run_id)
            prior = connection.execute(
                "SELECT payload, snapshot FROM native_commits "
                "WHERE scope = ? AND run_id = ? AND commit_id = ?",
                (key, run_id, batch.commit_id),
            ).fetchone()
            if prior is not None:
                if prior[0] != raw:
                    raise StoreConflict("commit ID payload mismatch")
                return RunSnapshot.model_validate_json(prior[1])
            current = RunSnapshot.model_validate_json(current_raw)
            check_commit(current, batch, expected_revision)
            if delivery is not None:
                from ksadk.harness.native.child_input import ChildInputRejected, check_delivery

                child_key = encode(delivery.target.scope)
                try:
                    child_raw, _ = self._row(connection, child_key, delivery.target.run_id)
                except (KeyError, ScopeMismatch) as exc:
                    raise ChildInputRejected("child_input_origin_changed") from exc
                def pending_commands(command_scope, command_run_id):
                    rows = connection.execute(
                        "SELECT data FROM native_commands WHERE scope = ? AND run_id = ? "
                        "AND consumed = 0 ORDER BY priority DESC, enqueue_order ASC",
                        (command_scope, command_run_id),
                    ).fetchall()
                    return tuple(RunCommand.model_validate_json(row[0]) for row in rows)

                parent_commands = pending_commands(key, run_id)
                command = next((item for item in parent_commands
                                if item.command_id == batch.consumed_command_id), None)
                if command is None:
                    raise StoreConflict("command is missing or already consumed")
                check_delivery(
                    current, RunSnapshot.model_validate_json(child_raw), command, delivery,
                    parent_commands=parent_commands,
                    child_commands=pending_commands(child_key, delivery.target.run_id),
                )
                child_command_raw = encode(delivery.command)
                existing = connection.execute(
                    "SELECT data FROM native_commands WHERE scope = ? AND run_id = ? "
                    "AND command_id = ?", (child_key, delivery.target.run_id,
                                         delivery.command.command_id),
                ).fetchone()
                if existing is not None and existing[0] != child_command_raw:
                    raise StoreConflict("child command ID payload mismatch")
                if existing is None:
                    connection.execute(
                        "INSERT INTO native_commands(scope, run_id, command_id, priority, data) "
                        "VALUES (?, ?, ?, ?, ?)", (child_key, delivery.target.run_id,
                            delivery.command.command_id, delivery.command.priority,
                            child_command_raw),
                    )
            if (batch.snapshot.phase.terminal or batch.require_no_pending_commands) and (
                batch.consumed_command_id is None
            ):
                pending = connection.execute(
                    "SELECT 1 FROM native_commands WHERE scope = ? AND run_id = ? "
                    "AND consumed = 0 LIMIT 1",
                    (key, run_id),
                ).fetchone()
                if pending is not None:
                    raise PendingCommandConflict(
                        "pending command prevents guarded commit"
                        if batch.require_no_pending_commands
                        else "pending command prevents terminal commit"
                    )
            discarded_ids = ()
            if batch.snapshot.phase.terminal and batch.consumed_command_id is not None:
                rows = connection.execute(
                    "SELECT data FROM native_commands WHERE scope = ? AND run_id = ? "
                    "AND consumed = 0 ORDER BY priority DESC, enqueue_order ASC",
                    (key, run_id),
                ).fetchall()
                pending_commands = tuple(RunCommand.model_validate_json(row[0]) for row in rows)
                if not any(
                    command.command_id == batch.consumed_command_id
                    for command in pending_commands
                ):
                    raise StoreConflict("command is missing or already consumed")
                batch, discarded_ids = settle_terminal_commands(current, batch, pending_commands)
                check_commit(current, batch, expected_revision)
            # Event inserts are append-only; uniqueness constraints also detect
            # a reused event ID from an earlier revision, rolling everything back.
            for event in batch.events:
                connection.execute(
                    "INSERT INTO native_events(scope, run_id, seq, event_id, data) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (key, run_id, event.seq, event.event_id, encode(event)),
                )
            for intent in batch.intents:
                existing = connection.execute(
                    "SELECT data FROM native_intents "
                    "WHERE scope = ? AND run_id = ? AND operation_key = ?",
                    (key, run_id, intent.operation_key),
                ).fetchone()
                check_intent(
                    EffectIntent.model_validate_json(existing[0]) if existing else None, intent
                )
                if existing is None:
                    connection.execute(
                        "INSERT INTO native_intents(scope, run_id, operation_key, data) "
                        "VALUES (?, ?, ?, ?)",
                        (key, run_id, intent.operation_key, encode(intent)),
                    )
            for receipt in batch.receipts:
                intent = connection.execute(
                    "SELECT 1 FROM native_intents "
                    "WHERE scope = ? AND run_id = ? AND operation_key = ?",
                    (key, run_id, receipt.operation_key),
                ).fetchone()
                if intent is None:
                    raise StoreConflict("receipt has no prepared intent")
                existing = connection.execute(
                    "SELECT data FROM native_receipts "
                    "WHERE scope = ? AND run_id = ? AND operation_key = ?",
                    (key, run_id, receipt.operation_key),
                ).fetchone()
                check_receipt(
                    EffectReceipt.model_validate_json(existing[0]) if existing else None, receipt
                )
                if existing is None:
                    connection.execute(
                        "INSERT INTO native_receipts(scope, run_id, operation_key, data) "
                        "VALUES (?, ?, ?, ?)",
                        (key, run_id, receipt.operation_key, encode(receipt)),
                    )
            if batch.consumed_command_id is not None:
                changed = connection.execute(
                    "UPDATE native_commands SET consumed = 1 WHERE scope = ? AND run_id = ? "
                    "AND command_id = ? AND consumed = 0",
                    (key, run_id, batch.consumed_command_id),
                ).rowcount
                if changed != 1:
                    raise StoreConflict("command is missing or already consumed")
            for command_id in discarded_ids:
                connection.execute(
                    "UPDATE native_commands SET consumed = 1 WHERE scope = ? AND run_id = ? "
                    "AND command_id = ? AND consumed = 0",
                    (key, run_id, command_id),
                )
            saved = encode(batch.snapshot)
            connection.execute(
                "UPDATE native_runs SET snapshot = ? WHERE scope = ? AND run_id = ?",
                (saved, key, run_id),
            )
            connection.execute(
                "INSERT INTO native_commits(scope, run_id, commit_id, payload, snapshot) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, run_id, batch.commit_id, raw, saved),
            )
            return RunSnapshot.model_validate_json(saved)

    def events(self, scope: RunScope, run_id: str, after_seq: int = 0) -> tuple[NativeEvent, ...]:
        key = encode(scope)
        with self._transaction() as connection:
            self._row(connection, key, run_id)
            rows = connection.execute(
                "SELECT data FROM native_events "
                "WHERE scope = ? AND run_id = ? AND seq > ? ORDER BY seq",
                (key, run_id, after_seq),
            ).fetchall()
            return tuple(NativeEvent.model_validate_json(row[0]) for row in rows)

    def pending_outbox(
        self, scope: RunScope, run_id: str, consumer: str, *, limit: int = 100
    ) -> tuple[NativeEvent, ...]:
        """Read committed events after this consumer's durable cursor.

        ``native_events`` is the transactional outbox payload: the event row
        and snapshot commit are written together. Delivery remains at least
        once until the consumer acknowledges the event identity.
        """
        if not consumer.strip() or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("outbox consumer and limit must be valid")
        key = encode(scope)
        with self._transaction() as connection:
            self._row(connection, key, run_id)
            row = connection.execute(
                "SELECT cursor FROM native_outbox_cursors "
                "WHERE scope = ? AND run_id = ? AND consumer = ?",
                (key, run_id, consumer),
            ).fetchone()
            cursor = row[0] if row else 0
            rows = connection.execute(
                "SELECT data FROM native_events WHERE scope = ? AND run_id = ? AND seq > ? "
                "ORDER BY seq LIMIT ?",
                (key, run_id, cursor, limit),
            ).fetchall()
            return tuple(NativeEvent.model_validate_json(item[0]) for item in rows)

    def ack_outbox(
        self, scope: RunScope, run_id: str, consumer: str, *, seq: int, event_id: str
    ) -> int:
        """Advance one contiguous event; reject gaps and mismatched identity."""
        if not consumer.strip() or type(seq) is not int or seq < 1 or not event_id:
            raise ValueError("outbox acknowledgment must identify an event")
        key = encode(scope)
        with self._transaction(write=True) as connection:
            self._row(connection, key, run_id)
            event = connection.execute(
                "SELECT event_id FROM native_events WHERE scope = ? AND run_id = ? AND seq = ?",
                (key, run_id, seq),
            ).fetchone()
            if event is None or event[0] != event_id:
                raise StoreConflict("outbox event identity mismatch")
            row = connection.execute(
                "SELECT cursor FROM native_outbox_cursors "
                "WHERE scope = ? AND run_id = ? AND consumer = ?",
                (key, run_id, consumer),
            ).fetchone()
            cursor = row[0] if row else 0
            if seq > cursor + 1:
                raise StoreConflict("outbox acknowledgment would skip events")
            if seq <= cursor:
                return cursor
            connection.execute(
                "INSERT INTO native_outbox_cursors(scope, run_id, consumer, cursor) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(scope, run_id, consumer) "
                "DO UPDATE SET cursor = excluded.cursor",
                (key, run_id, consumer, seq),
            )
            return seq

    def get_intent(self, scope: RunScope, run_id: str, operation_key: str) -> EffectIntent | None:
        key = encode(scope)
        with self._transaction() as connection:
            self._row(connection, key, run_id)
            row = connection.execute(
                "SELECT data FROM native_intents "
                "WHERE scope = ? AND run_id = ? AND operation_key = ?",
                (key, run_id, operation_key),
            ).fetchone()
            return EffectIntent.model_validate_json(row[0]) if row else None

    def claim_intent(
        self, scope: RunScope, run_id: str, operation_key: str, *, fencing_token: int,
        require_no_pending_commands: bool = False,
    ) -> EffectIntent:
        key = encode(scope)
        with self._transaction(write=True) as connection:
            snapshot, fence = self._row(connection, key, run_id)
            check_fence(fence, fencing_token)
            self._check_live_lease(connection, key, run_id)
            if RunSnapshot.model_validate_json(snapshot).phase.terminal:
                raise StoreConflict("terminal run is immutable")
            if require_no_pending_commands and connection.execute(
                "SELECT 1 FROM native_commands WHERE scope=? AND run_id=? AND consumed=0 LIMIT 1",
                (key, run_id),
            ).fetchone() is not None:
                raise PendingCommandConflict("pending command prevents effect claim")
            row = connection.execute(
                "SELECT data FROM native_intents "
                "WHERE scope = ? AND run_id = ? AND operation_key = ?",
                (key, run_id, operation_key),
            ).fetchone()
            if row is None:
                raise KeyError("intent not found")
            intent = EffectIntent.model_validate_json(row[0])
            receipt = connection.execute(
                "SELECT 1 FROM native_receipts "
                "WHERE scope = ? AND run_id = ? AND operation_key = ?",
                (key, run_id, operation_key),
            ).fetchone()
            if intent.status != "prepared" or receipt is not None:
                raise EffectAlreadyClaimed("effect is running or already has a receipt")
            claimed = intent.model_copy(update={"status": "running"})
            connection.execute(
                "UPDATE native_intents SET data = ? "
                "WHERE scope = ? AND run_id = ? AND operation_key = ?",
                (encode(claimed), key, run_id, operation_key),
            )
            return detached(claimed)

    def get_receipt(self, scope: RunScope, run_id: str, operation_key: str) -> EffectReceipt | None:
        key = encode(scope)
        with self._transaction() as connection:
            self._row(connection, key, run_id)
            row = connection.execute(
                "SELECT data FROM native_receipts "
                "WHERE scope = ? AND run_id = ? AND operation_key = ?",
                (key, run_id, operation_key),
            ).fetchone()
            return EffectReceipt.model_validate_json(row[0]) if row else None

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def __enter__(self) -> SQLiteRunStore:
        if self._closed:
            raise StoreConflict("store is closed")
        return self

    def __exit__(self, *_args) -> None:
        self.close()
