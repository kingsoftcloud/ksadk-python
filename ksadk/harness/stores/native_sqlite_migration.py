"""Conservative legacy session admission inside the schema upgrade transaction."""

import hashlib
import json
import os
import sqlite3
import tempfile
import time
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from ksadk.harness.native.state import RunScope, RunSnapshot

from .native_commit_history import (
    CHAIN_USAGE_MIGRATION_ID,
    HISTORY_MIGRATION_ID,
    HISTORY_SCHEMA,
    SCHEMA_VERSION,
    validate_history_schema,
)
from .native_contract import StoreConflict, encode

READER_VERSION = "ksadk.native-local-migration/v1"
EXPORT_MIGRATION_ID = "native.sqlite.schema3.explicit-session-heads/v1"


@dataclass(frozen=True)
class LegacySessionHead:
    scope: RunScope
    session_id: str
    run_id: str


@dataclass(frozen=True)
class LegacySessionCandidates:
    scope: RunScope
    session_id: str
    run_ids: tuple[str, ...]
    pinned_run_id: str | None


@dataclass(frozen=True)
class NativeStoreInspection:
    schema_version: int
    source_digest: str
    sessions: tuple[LegacySessionCandidates, ...]


@dataclass(frozen=True)
class NativeStoreExport:
    source_digest: str
    schema_version: int
    migration_id: str = EXPORT_MIGRATION_ID
    reader_version: str = READER_VERSION


def _legacy_sessions(connection: sqlite3.Connection):
    groups: dict[tuple[str, str], list[str]] = {}
    identities: dict[tuple[str, str], str] = {}
    scopes: dict[str, RunScope] = {}
    for scope, run_id, raw in connection.execute("SELECT scope, run_id, snapshot FROM native_runs"):
        try:
            snapshot = RunSnapshot.model_validate_json(raw)
        except ValueError as exc:
            raise StoreConflict("legacy snapshot cannot be safely migrated") from exc
        if snapshot.run_id != run_id or encode(snapshot.scope) != scope:
            raise StoreConflict("legacy snapshot identity mismatch")
        scopes[scope] = snapshot.scope
        groups.setdefault((scope, snapshot.session_id), []).append(run_id)
        identities[(scope, run_id)] = snapshot.session_id
    return groups, identities, scopes


def _pins(connection: sqlite3.Connection, identities) -> dict[tuple[str, str], str]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_session_heads'"
    ).fetchone()
    if not exists:
        return {}
    pins = {}
    for scope, session_id, run_id in connection.execute(
        "SELECT scope, session_id, run_id FROM native_session_heads"
    ):
        if identities.get((scope, run_id)) != session_id:
            raise StoreConflict("legacy session head identity mismatch")
        pins[(scope, session_id)] = run_id
    return pins


def _fingerprint(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    digest.update(str(connection.execute("PRAGMA user_version").fetchone()[0]).encode())
    # Logical SQLite image: includes fences, receipts, commands, unknown
    # auxiliary tables and migration history, independent of WAL/page layout.
    for statement in connection.iterdump():
        digest.update(statement.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def inspect_native_store(path: str | Path) -> NativeStoreInspection:
    """Read a consistent source image without constructing a writable Store."""
    path = Path(path).resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("BEGIN")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1, 2, SCHEMA_VERSION}:
            raise StoreConflict("unsupported native store schema")
        groups, identities, scopes = _legacy_sessions(connection)
        pins = _pins(connection, identities)
        return NativeStoreInspection(
            version,
            _fingerprint(connection),
            tuple(
                LegacySessionCandidates(
                    scopes[scope], session, tuple(sorted(runs)), pins.get((scope, session))
                )
                for (scope, session), runs in sorted(groups.items())
            ),
        )


def export_native_store(
    source: str | Path,
    destination: str | Path,
    *,
    expected_source_digest: str,
    session_heads: Sequence[LegacySessionHead] = (),
    offline_acknowledged: bool = False,
) -> NativeStoreExport:
    """Publish an offline upgraded copy; never replace or execute the source.

    The operator must stop all users of the source first. Original unleased
    schemas cannot prove process death, so this acknowledgement is a host
    precondition, not a claim of hot migration or ownership transfer.
    Existing live leases are refused even when that precondition is asserted.
    """
    if offline_acknowledged is not True:
        raise ValueError("native store export requires an explicitly offline source")
    source = Path(source).resolve(strict=True)
    destination = Path(destination)
    destination.parent.resolve(strict=True)
    if source == destination.resolve():
        raise ValueError("migration destination must differ from the source")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    descriptor, staging_name = tempfile.mkstemp(prefix=".native-migration-", dir=destination.parent)
    os.close(descriptor)
    staging = Path(staging_name)
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(staging)) as copied:
                original.backup(copied)
        with closing(sqlite3.connect(staging, isolation_level=None)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            source_digest = _fingerprint(connection)
            if source_digest != expected_source_digest:
                raise StoreConflict("native store changed after inspection")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1, 2, SCHEMA_VERSION}:
                raise StoreConflict("unsupported native store schema")
            groups, identities, _ = _legacy_sessions(connection)
            pins = _pins(connection, identities)
            has_leases = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_run_leases'"
            ).fetchone()
            if (
                has_leases
                and connection.execute(
                    "SELECT 1 FROM native_run_leases WHERE expires_at > ? LIMIT 1", (time.time(),)
                ).fetchone()
            ):
                raise StoreConflict("native store has a live lease")
            chosen = {}
            for head in session_heads:
                key = (encode(head.scope), head.session_id)
                if key in chosen or head.run_id not in groups.get(key, ()):
                    raise StoreConflict("invalid explicit legacy session head")
                if key in pins and pins[key] != head.run_id:
                    raise StoreConflict("existing legacy session pin cannot be replaced")
                chosen[key] = head.run_id
            from .native_commit_history import CHAIN_USAGE_SCHEMA, validate_chain_usage_schema
            from .native_sqlite import _SCHEMA

            # executescript commits a pending transaction; restart it in the
            # script before schema/heads are touched. The staged copy is private.
            connection.executescript(
                "BEGIN IMMEDIATE;\n" + _SCHEMA + HISTORY_SCHEMA + CHAIN_USAGE_SCHEMA
            )
            validate_history_schema(connection)
            validate_chain_usage_schema(connection)
            for (scope, session), run_id in chosen.items():
                if (scope, session) not in pins:
                    connection.execute(
                        "INSERT INTO native_session_heads VALUES (?,?,?)", (scope, session, run_id)
                    )
            restore_legacy_session_heads(connection)
            connection.execute("""CREATE TABLE IF NOT EXISTS native_store_migrations (
                migration_id TEXT NOT NULL, reader_version TEXT NOT NULL,
                source_digest TEXT NOT NULL, explicit_heads TEXT NOT NULL,
                PRIMARY KEY(migration_id,source_digest)
            )""")
            connection.execute(
                "INSERT INTO native_store_migrations VALUES (?,?,?,?)",
                (
                    EXPORT_MIGRATION_ID,
                    READER_VERSION,
                    source_digest,
                    json.dumps(
                        [
                            {"scope": json.loads(scope), "session_id": session, "run_id": run}
                            for (scope, session), run in sorted(chosen.items())
                        ],
                        sort_keys=True,
                    ),
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO native_store_migrations VALUES (?,?,?,?)",
                (HISTORY_MIGRATION_ID, READER_VERSION, source_digest, "[]"),
            )
            # The export also creates the chain-usage tables, so record that
            # migration too — otherwise the first open of the exported DB would
            # see the tables exist but the migration is unrecorded.
            connection.execute(
                "INSERT OR IGNORE INTO native_store_migrations VALUES (?,?,?,?)",
                (
                    CHAIN_USAGE_MIGRATION_ID,
                    READER_VERSION,
                    source_digest,
                    "[]",
                ),
            )
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.commit()
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise StoreConflict("native store export integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise StoreConflict("native store export foreign key check failed")
        with staging.open("rb") as stream:
            os.fsync(stream.fileno())
        # link is atomic and refuses an existing destination, including a race
        # after the first check. No partially migrated image becomes visible.
        os.link(staging, destination)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return NativeStoreExport(source_digest, SCHEMA_VERSION)
    finally:
        for path in (
            staging,
            Path(str(staging) + "-wal"),
            Path(str(staging) + "-shm"),
            Path(str(staging) + "-journal"),
        ):
            path.unlink(missing_ok=True)


def restore_legacy_session_heads(connection: sqlite3.Connection) -> None:
    """Restore only facts proven by an existing pin or a unique legacy Run.

    The original schema had no chronological Run identity or Session CAS.
    Multiple unpinned Runs cannot safely be ordered by rowid or UUID. Keep the
    whole upgrade reversible rather than authorizing a guessed parent.
    """
    groups, identities, _ = _legacy_sessions(connection)
    pinned = _pins(connection, identities)
    for (scope, session_id), runs in groups.items():
        if (scope, session_id) in pinned:
            continue
        if len(runs) != 1:
            raise StoreConflict("legacy session head is ambiguous; explicit migration required")
        connection.execute(
            "INSERT INTO native_session_heads VALUES (?, ?, ?)", (scope, session_id, runs[0])
        )
