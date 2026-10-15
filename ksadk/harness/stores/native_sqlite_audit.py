"""Additive SQLite audit tables; records never rewrite execution facts."""

import sqlite3

CONSERVATIVE_MIGRATION_ID = "native.sqlite.schema2.conservative-session-heads/v1"


def initialize_audit_tables(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS native_store_migrations (
        migration_id TEXT NOT NULL, reader_version TEXT NOT NULL,
        source_digest TEXT NOT NULL, explicit_heads TEXT NOT NULL,
        PRIMARY KEY(migration_id,source_digest)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS native_restore_audits (
        audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT NOT NULL, run_id TEXT NOT NULL, data TEXT NOT NULL,
        FOREIGN KEY(scope,run_id) REFERENCES native_runs(scope,run_id)
    )""")
    connection.execute("""CREATE INDEX IF NOT EXISTS native_restore_audits_run
        ON native_restore_audits(scope,run_id,audit_id)""")


def historical_migrations(connection: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT migration_id FROM native_store_migrations ORDER BY migration_id"
        )
    )
