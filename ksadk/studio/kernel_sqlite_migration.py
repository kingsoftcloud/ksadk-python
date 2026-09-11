"""Move legacy per-Build Kernel facts into the canonical Session SQLite file.

The original file remains a readable backup. Durable write-blocking triggers
fence old processes before the destination commit. A crash between those steps
can be retried from the untouched source facts. The destination receipt prevents
replaying that backup over Runs which have since advanced.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from ksadk.kernel.execution_grants_sqlite import EXECUTION_GRANT_SCHEMA
from ksadk.kernel.sqlite_store import _SCHEMA, SCHEMA_VERSION

_TABLES = frozenset(
    {
        "kernel_inbox",
        "kernel_runs",
        "kernel_activations",
        "kernel_accepted_seq",
        "kernel_interactions",
        "kernel_interaction_submissions",
        "kernel_execution_grants",
        "kernel_execution_grant_operations",
    }
)
_RECEIPTS = """
CREATE TABLE IF NOT EXISTS studio_kernel_migrations (
  source_path TEXT PRIMARY KEY,
  source_digest TEXT NOT NULL,
  agent_instance_id TEXT NOT NULL
);
"""


class KernelMigrationError(RuntimeError):
    """Fail closed: leave both existing stores available for operator recovery."""


def migrate_legacy_kernel(source: Path, destination: Path, agent_instance_id: str) -> None:
    source, destination = source.resolve(), destination.resolve()
    if source == destination or not source.exists():
        return
    # Source first matches the old admission writer's lock ordering. Locks also
    # ensure the source fingerprint cannot race a final old-process write.
    with closing(sqlite3.connect(source, isolation_level=None, timeout=5)) as old:
        old.execute("BEGIN IMMEDIATE")
        try:
            if old.execute("PRAGMA user_version").fetchone()[0] > SCHEMA_VERSION:
                raise KernelMigrationError("Legacy Kernel schema is newer than this Studio")
            tables = {
                row[0]
                for row in old.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'kernel_%'"
                )
            }
            if tables - _TABLES:
                raise KernelMigrationError("Legacy Kernel has unknown tables; migration refused")
            if not tables:
                old.rollback()
                return
            if (
                "kernel_activations" in tables
                and old.execute(
                    "SELECT 1 FROM kernel_activations "
                    "WHERE released=0 AND lease_expires_at>? LIMIT 1",
                    (time.time(),),
                ).fetchone()
            ):
                raise KernelMigrationError(
                    "Legacy Kernel still has an active lease; "
                    "close the old Studio and retry after expiry"
                )
            facts, digest = _facts(old, tables, agent_instance_id)
            with closing(sqlite3.connect(destination, isolation_level=None, timeout=5)) as new:
                new.execute("PRAGMA journal_mode=WAL")
                new.execute("PRAGMA synchronous=FULL")
                new.executescript(_SCHEMA + EXECUTION_GRANT_SCHEMA + _RECEIPTS)
                new.execute("BEGIN IMMEDIATE")
                try:
                    receipt = new.execute(
                        "SELECT source_digest, agent_instance_id FROM studio_kernel_migrations "
                        "WHERE source_path=?",
                        (str(source),),
                    ).fetchone()
                    if receipt is not None and receipt != (digest, agent_instance_id):
                        raise KernelMigrationError(
                            "Legacy Kernel changed after migration; recovery required"
                        )
                    if receipt is None:
                        _copy(new, facts)
                        new.execute(
                            "INSERT INTO studio_kernel_migrations VALUES (?,?,?)",
                            (str(source), digest, agent_instance_id),
                        )
                    _fence_backup(old, tables, destination, digest)
                    # Once the source fence commits, an old binary cannot acquire
                    # a lease or admit new work in the abandoned database.
                    old.commit()
                    new.commit()
                except BaseException:
                    new.rollback()
                    raise
        except sqlite3.Error as error:
            old.rollback()
            raise KernelMigrationError(
                "Kernel migration could not lock or merge existing facts"
            ) from error
        except BaseException:
            old.rollback()
            raise


def _facts(connection, tables, agent_instance_id):
    facts = []
    hasher = hashlib.sha256()
    for table in sorted(tables):
        info = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        columns = [column[1] for column in info]
        if (
            "agent_instance_id" in columns
            and connection.execute(
                f'SELECT 1 FROM "{table}" WHERE agent_instance_id<>? LIMIT 1', (agent_instance_id,)
            ).fetchone()
        ):
            raise KernelMigrationError("Legacy Kernel contains a different Build authority")
        names = ",".join(f'"{name}"' for name in columns)
        rows = connection.execute(f'SELECT {names} FROM "{table}" ORDER BY {names}').fetchall()
        facts.append((table, columns, rows))
        hasher.update(
            json.dumps([table, columns, rows], sort_keys=True, separators=(",", ":")).encode()
        )
    return facts, hasher.hexdigest()


def _copy(connection, facts):
    for table, columns, rows in facts:
        target_columns = {
            column[1] for column in connection.execute(f'PRAGMA table_info("{table}")')
        }
        if set(columns) - target_columns:
            raise KernelMigrationError("Legacy Kernel columns are incompatible with this Studio")
        names = ",".join(f'"{name}"' for name in columns)
        placeholders = ",".join("?" for _ in columns)
        predicate = " AND ".join(f'"{name}" IS ?' for name in columns)
        for row in rows:
            connection.execute(
                f'INSERT OR IGNORE INTO "{table}" ({names}) VALUES ({placeholders})', row
            )
            if not connection.execute(f'SELECT 1 FROM "{table}" WHERE {predicate}', row).fetchone():
                raise KernelMigrationError("Existing Kernel fact conflicts with the legacy backup")


def _fence_backup(connection, tables, destination, digest):
    connection.execute(
        "CREATE TABLE IF NOT EXISTS studio_kernel_migration_lock "
        "(destination TEXT NOT NULL, source_digest TEXT NOT NULL)"
    )
    prior = connection.execute(
        "SELECT destination,source_digest FROM studio_kernel_migration_lock"
    ).fetchall()
    if prior and prior != [(str(destination), digest)]:
        raise KernelMigrationError("Legacy Kernel is already assigned to another migration")
    if not prior:
        connection.execute(
            "INSERT INTO studio_kernel_migration_lock VALUES (?,?)", (str(destination), digest)
        )
    for table in sorted(tables):
        for action in ("INSERT", "UPDATE", "DELETE"):
            connection.execute(
                f'CREATE TRIGGER IF NOT EXISTS "migrated_{table}_{action}" '
                f'BEFORE {action} ON "{table}" '
                "BEGIN SELECT RAISE(ABORT, 'Kernel moved to canonical Session database; "
                "old writer fenced'); END"
            )
