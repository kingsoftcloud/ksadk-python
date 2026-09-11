from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from ksadk.kernel.execution_grants_sqlite import EXECUTION_GRANT_SCHEMA
from ksadk.kernel.sqlite_store import _SCHEMA, SCHEMA_VERSION
from ksadk.studio.kernel_sqlite_migration import KernelMigrationError, migrate_legacy_kernel


def legacy(tmp_path: Path):
    source, destination = tmp_path / "old.sqlite", tmp_path / "sessions.sqlite"
    with sqlite3.connect(source) as connection:
        connection.executescript(_SCHEMA + EXECUTION_GRANT_SCHEMA)
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.execute(
            "INSERT INTO kernel_runs VALUES (?,?,?,?,?,?,?,?)",
            (
                "original-run",
                "build-a",
                "session",
                "paused",
                17,
                "created",
                "updated",
                '{"native_handle":"original-checkpoint"}',
            ),
        )
        connection.execute(
            "INSERT INTO kernel_inbox VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "original-command",
                "build-a",
                "session",
                "original-key",
                "digest",
                3,
                "claimed",
                17,
                "{}",
            ),
        )
        connection.execute("INSERT INTO kernel_accepted_seq VALUES (?,?)", ("session", 3))
        connection.execute(
            "INSERT INTO kernel_execution_grants VALUES (?,?,?,?,?,?,?,?,?)",
            ("grant", "tenant", "build-a", "session", "host", "suspended", 2, "created", "updated"),
        )
        connection.execute(
            "INSERT INTO kernel_execution_grant_operations VALUES (?,?,?,?)",
            ("grant", "original-barrier", "digest", '{"state":"suspended"}'),
        )
    return source, destination


def query(path, sql):
    with sqlite3.connect(path) as connection:
        return connection.execute(sql).fetchall()


def test_migrate_retains_run_inbox_fence_and_grant_receipts_and_fences_old_writer(tmp_path):
    source, destination = legacy(tmp_path)
    migrate_legacy_kernel(source, destination, "build-a")
    assert source.exists()
    for table in (
        "kernel_runs",
        "kernel_inbox",
        "kernel_accepted_seq",
        "kernel_execution_grants",
        "kernel_execution_grant_operations",
    ):
        assert query(destination, f"SELECT * FROM {table}") == query(
            source, f"SELECT * FROM {table}"
        )
    with (
        sqlite3.connect(source) as connection,
        pytest.raises(sqlite3.IntegrityError, match="old writer fenced"),
    ):
        connection.execute("UPDATE kernel_runs SET state='running'")
    # Legitimate later changes in the destination are never overwritten by the backup.
    with sqlite3.connect(destination) as connection:
        connection.execute("UPDATE kernel_runs SET state='completed'")
    migrate_legacy_kernel(source, destination, "build-a")
    assert query(destination, "SELECT state FROM kernel_runs") == [("completed",)]


def test_active_lease_refuses_migration_without_mutating_source(tmp_path):
    source, destination = legacy(tmp_path)
    with sqlite3.connect(source) as connection:
        connection.execute(
            "INSERT INTO kernel_activations VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "build-a",
                "session",
                "old-process",
                17,
                time.time() + 60,
                "future",
                0,
                "harness",
                "bundle",
                "capabilities",
            ),
        )
    with pytest.raises(KernelMigrationError, match="active lease"):
        migrate_legacy_kernel(source, destination, "build-a")
    assert not destination.exists()
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE kernel_activations SET released=1")
    migrate_legacy_kernel(source, destination, "build-a")
    assert query(destination, "SELECT fencing_token FROM kernel_activations") == [(17,)]


def test_conflict_and_changed_backup_fail_closed(tmp_path):
    source, destination = legacy(tmp_path)
    with sqlite3.connect(destination) as connection:
        connection.executescript(_SCHEMA)
        connection.execute("INSERT INTO kernel_accepted_seq VALUES (?,?)", ("session", 99))
    with pytest.raises(KernelMigrationError, match="conflicts"):
        migrate_legacy_kernel(source, destination, "build-a")
    assert query(destination, "SELECT * FROM kernel_runs") == []
    assert query(source, "SELECT name FROM sqlite_master WHERE type='trigger'") == []
    with sqlite3.connect(destination) as connection:
        connection.execute("DELETE FROM kernel_accepted_seq")
    migrate_legacy_kernel(source, destination, "build-a")
    with sqlite3.connect(source) as connection:
        connection.execute("DROP TRIGGER migrated_kernel_runs_UPDATE")
        connection.execute("UPDATE kernel_runs SET state='completed'")
    with pytest.raises(KernelMigrationError, match="changed after migration"):
        migrate_legacy_kernel(source, destination, "build-a")
    assert query(destination, "SELECT state FROM kernel_runs") == [("paused",)]


def test_crash_after_backup_fence_before_destination_commit_retries_original_facts(
    tmp_path, monkeypatch
):
    source, destination = legacy(tmp_path)
    original = sqlite3.connect

    class CommitFailure(sqlite3.Connection):
        def commit(self):
            raise OSError("simulated lost process before target commit")

    def connect(database, *args, **kwargs):
        if Path(database) == destination:
            kwargs["factory"] = CommitFailure
        return original(database, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(sqlite3, "connect", connect)
        with pytest.raises(OSError, match="simulated lost process"):
            migrate_legacy_kernel(source, destination, "build-a")
    assert query(destination, "SELECT * FROM kernel_runs") == []
    with (
        sqlite3.connect(source) as connection,
        pytest.raises(sqlite3.IntegrityError, match="old writer fenced"),
    ):
        connection.execute("DELETE FROM kernel_runs")
    migrate_legacy_kernel(source, destination, "build-a")
    assert query(destination, "SELECT run_id,state FROM kernel_runs") == [
        ("original-run", "paused")
    ]


def test_unknown_schema_and_wrong_build_refuse_migration(tmp_path):
    source, destination = legacy(tmp_path)
    with pytest.raises(KernelMigrationError, match="different Build"):
        migrate_legacy_kernel(source, destination, "other-build")
    with sqlite3.connect(source) as connection:
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    with pytest.raises(KernelMigrationError, match="newer"):
        migrate_legacy_kernel(source, destination, "build-a")
    assert not destination.exists()
