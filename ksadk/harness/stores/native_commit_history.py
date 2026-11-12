"""Incremental, integrity-checked commit retries; current snapshots stay canonical.

This codec changes storage, not the atomic commit or event/outbox contract.
Legacy full snapshots remain readable. Checkpoints bound reconstruction work;
growing strings and arrays store new content rather than their entire prefix.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from ksadk.harness.native.state import RunSnapshot

from .native_contract import StoreConflict

SCHEMA_VERSION = 3
HISTORY_FORMAT = "ksadk.native-commit-snapshot/v1"
PAYLOAD_FORMAT = "ksadk.native-commit-digest/v2"
_LEGACY_PAYLOAD_FORMAT = "ksadk.native-commit-digest/v1"
HISTORY_MIGRATION_ID = "native.sqlite.schema3.incremental-commit-history/v1"
CHAIN_USAGE_MIGRATION_ID = "native.sqlite.schema3.task-chain-usage/v1"
MAX_DELTA_DEPTH = 128

HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS native_commit_heads (
    scope TEXT NOT NULL, run_id TEXT NOT NULL, commit_id TEXT NOT NULL,
    snapshot_digest TEXT NOT NULL, delta_depth INTEGER NOT NULL,
    delta_bytes INTEGER NOT NULL,
    PRIMARY KEY (scope, run_id),
    FOREIGN KEY (scope, run_id, commit_id)
        REFERENCES native_commits(scope, run_id, commit_id)
);
"""


def _json(value) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def payload_digest(raw: str, snapshot_digest: str) -> str:
    # The admitted request and actual result can differ: terminal settlement
    # appends command dispositions under the same transaction. Bind both.
    request = json.loads(raw.split("\n", 1)[0])
    return _json({
        "format": PAYLOAD_FORMAT,
        "sha256": _digest(raw),
        "snapshot_sha256": snapshot_digest,
        # These commit admission facts are not part of the result snapshot.
        # Keep them readable rather than replacing the audit trail with a hash.
        "admission": {
            "consumed_command_id": request["consumed_command_id"],
            "require_no_pending_commands": request.get("require_no_pending_commands", False),
        },
    })


def _stored_snapshot_digest(payload: str) -> str | None:
    # Legacy child deliveries are two canonical JSON records joined by a newline.
    data = json.loads(payload.split("\n", 1)[0])
    if isinstance(data, dict) and "commit_id" in data and "snapshot" in data:
        return None
    if not isinstance(data, dict):
        raise StoreConflict("invalid commit history payload")
    expected_keys = {"format", "sha256", "snapshot_sha256"}
    if data.get("format") == PAYLOAD_FORMAT:
        expected_keys.add("admission")
        admission = data.get("admission")
        if (
            not isinstance(admission, dict)
            or set(admission) != {"consumed_command_id", "require_no_pending_commands"}
            or not isinstance(admission["consumed_command_id"], (str, type(None)))
            or type(admission["require_no_pending_commands"]) is not bool
            or (admission["require_no_pending_commands"] and admission["consumed_command_id"]
                is not None)
        ):
            raise StoreConflict("invalid commit history admission")
    elif data.get("format") != _LEGACY_PAYLOAD_FORMAT:
        raise StoreConflict("invalid commit history payload format")
    if set(data) != expected_keys:
        raise StoreConflict("invalid commit history payload")
    digest = data["snapshot_sha256"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise StoreConflict("invalid commit history payload digest")
    return digest


def matches_payload(saved: str, proposed: str) -> bool:
    if saved == proposed:
        return True
    try:
        digest = _stored_snapshot_digest(saved)
        if digest is None:
            return False
        if json.loads(saved)["format"] == _LEGACY_PAYLOAD_FORMAT:
            # Old digest rows cannot retroactively acquire admission evidence.
            return saved == _json({
                "format": _LEGACY_PAYLOAD_FORMAT,
                "sha256": _digest(proposed),
                "snapshot_sha256": digest,
            })
        return saved == payload_digest(proposed, digest)
    except (StoreConflict, ValueError, TypeError):
        return False


def schema_digest(connection: sqlite3.Connection) -> str:
    """Fingerprint the source DDL, not all execution data, for this additive upgrade."""
    ddl = connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE name LIKE 'native_%' ORDER BY name"
    ).fetchall()
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    return _digest(_json({"kind": "native-schema-only/v1", "version": version, "ddl": ddl}))


def validate_history_schema(connection: sqlite3.Connection) -> None:
    kind = connection.execute(
        "SELECT type FROM sqlite_master WHERE name='native_commit_heads'"
    ).fetchone()
    columns = connection.execute("PRAGMA table_info(native_commit_heads)").fetchall()
    foreign_keys = connection.execute("PRAGMA foreign_key_list(native_commit_heads)").fetchall()
    expected_columns = [
        ("scope", "TEXT", 1, None, 1),
        ("run_id", "TEXT", 1, None, 2),
        ("commit_id", "TEXT", 1, None, 0),
        ("snapshot_digest", "TEXT", 1, None, 0),
        ("delta_depth", "INTEGER", 1, None, 0),
        ("delta_bytes", "INTEGER", 1, None, 0),
    ]
    expected_foreign_keys = [
        (0, index, "native_commits", name, name, "NO ACTION", "NO ACTION", "NONE")
        for index, name in enumerate(("scope", "run_id", "commit_id"))
    ]
    if (
        kind != ("table",)
        or [row[1:] for row in columns] != expected_columns
        or sorted(foreign_keys) != expected_foreign_keys
    ):
        raise StoreConflict("invalid native commit history schema")


CHAIN_USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS native_task_chain_usage (
    scope TEXT NOT NULL, task_chain_id TEXT NOT NULL,
    model_calls INTEGER NOT NULL CHECK (model_calls >= 0),
    tool_calls INTEGER NOT NULL CHECK (tool_calls >= 0),
    execution_elapsed_seconds REAL NOT NULL CHECK (execution_elapsed_seconds >= 0),
    compaction_count INTEGER NOT NULL CHECK (compaction_count >= 0),
    descendant_model_calls INTEGER NOT NULL CHECK (descendant_model_calls >= 0),
    descendant_tool_calls INTEGER NOT NULL CHECK (descendant_tool_calls >= 0),
    notification_turns INTEGER NOT NULL CHECK (notification_turns >= 0),
    PRIMARY KEY (scope, task_chain_id)
);
CREATE TABLE IF NOT EXISTS native_task_chain_settled_runs (
    scope TEXT NOT NULL, task_chain_id TEXT NOT NULL, run_id TEXT NOT NULL,
    commit_id TEXT NOT NULL,
    model_calls INTEGER NOT NULL CHECK (model_calls >= 0),
    tool_calls INTEGER NOT NULL CHECK (tool_calls >= 0),
    execution_elapsed_seconds REAL NOT NULL CHECK (execution_elapsed_seconds >= 0),
    compaction_count INTEGER NOT NULL CHECK (compaction_count >= 0),
    descendant_model_calls INTEGER NOT NULL CHECK (descendant_model_calls >= 0),
    descendant_tool_calls INTEGER NOT NULL CHECK (descendant_tool_calls >= 0),
    is_notification INTEGER NOT NULL CHECK (is_notification IN (0, 1)),
    settled_at REAL NOT NULL,
    PRIMARY KEY (scope, task_chain_id, run_id)
);
CREATE TABLE IF NOT EXISTS native_task_chain_registrations (
    scope TEXT NOT NULL, task_chain_id TEXT NOT NULL, run_id TEXT NOT NULL,
    registered_at REAL NOT NULL,
    PRIMARY KEY (scope, task_chain_id, run_id)
);
"""


def validate_chain_usage_schema(connection: sqlite3.Connection) -> None:
    """Strict structural validation of the task-chain ledger tables.

    Mirrors ``validate_history_schema``: checks table kind, column names/types,
    and primary key. A pre-existing table with wrong columns (e.g. only an
    ``unexpected`` TEXT column) is rejected, not silently accepted.
    """
    _validate_table(connection, "native_task_chain_usage", [
        ("scope", "TEXT", 1, None, 1),
        ("task_chain_id", "TEXT", 1, None, 2),
        ("model_calls", "INTEGER", 1, None, 0),
        ("tool_calls", "INTEGER", 1, None, 0),
        ("execution_elapsed_seconds", "REAL", 1, None, 0),
        ("compaction_count", "INTEGER", 1, None, 0),
        ("descendant_model_calls", "INTEGER", 1, None, 0),
        ("descendant_tool_calls", "INTEGER", 1, None, 0),
        ("notification_turns", "INTEGER", 1, None, 0),
    ])
    _validate_table(connection, "native_task_chain_settled_runs", [
        ("scope", "TEXT", 1, None, 1),
        ("task_chain_id", "TEXT", 1, None, 2),
        ("run_id", "TEXT", 1, None, 3),
        ("commit_id", "TEXT", 1, None, 0),
        ("model_calls", "INTEGER", 1, None, 0),
        ("tool_calls", "INTEGER", 1, None, 0),
        ("execution_elapsed_seconds", "REAL", 1, None, 0),
        ("compaction_count", "INTEGER", 1, None, 0),
        ("descendant_model_calls", "INTEGER", 1, None, 0),
        ("descendant_tool_calls", "INTEGER", 1, None, 0),
        ("is_notification", "INTEGER", 1, None, 0),
        ("settled_at", "REAL", 1, None, 0),
    ])
    _validate_table(connection, "native_task_chain_registrations", [
        ("scope", "TEXT", 1, None, 1),
        ("task_chain_id", "TEXT", 1, None, 2),
        ("run_id", "TEXT", 1, None, 3),
        ("registered_at", "REAL", 1, None, 0),
    ])


def _validate_table(
    connection: sqlite3.Connection, table_name: str,
    expected_columns: list[tuple],
) -> None:
    kind = connection.execute(
        f"SELECT type FROM sqlite_master WHERE name='{table_name}'"
    ).fetchone()
    columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    actual = [row[1:] for row in columns]
    if kind != ("table",) or actual != expected_columns:
        raise StoreConflict(f"invalid {table_name} schema")


def _difference(before, after):
    if isinstance(before, dict) and isinstance(after, dict):
        changes = {}
        for key, value in after.items():
            change = (
                _difference(before[key], value) if key in before else {"op": "set", "value": value}
            )
            if change is not None:
                changes[key] = change
        removed = sorted(before.keys() - after.keys())
        return (
            {"op": "dict", "changes": changes, "removed": removed} if changes or removed else None
        )
    if isinstance(before, list) and isinstance(after, list):
        overlap = min(len(before), len(after))
        changes = {}
        for index in range(overlap):
            change = _difference(before[index], after[index])
            if change is not None:
                changes[str(index)] = change
        if changes or len(before) != len(after):
            return {
                "op": "list",
                "changes": changes,
                "length": len(after),
                "appended": after[overlap:],
            }
        return None
    if type(before) is type(after) and before == after:
        return None
    if isinstance(before, str) and isinstance(after, str) and after.startswith(before):
        return {"op": "text", "append": after[len(before) :]}
    return {"op": "set", "value": after}


def _apply(before, patch):
    operation = patch["op"]
    if operation == "set":
        return patch["value"]
    if operation == "text" and isinstance(before, str):
        return before + patch["append"]
    if operation == "dict" and isinstance(before, dict):
        result = dict(before)
        for key in patch["removed"]:
            del result[key]
        for key, change in patch["changes"].items():
            result[key] = _apply(result.get(key), change)
        return result
    if operation == "list" and isinstance(before, list):
        result = before[: patch["length"]]
        for index, change in patch["changes"].items():
            result[int(index)] = _apply(result[int(index)], change)
        result.extend(patch["appended"])
        if len(result) != patch["length"]:
            raise ValueError("invalid list patch length")
        return result
    raise ValueError("invalid snapshot patch")


def encode_snapshot(connection, scope: str, run_id: str, previous: str, saved: str):
    """Return history data and the next head, in the caller's write transaction."""
    digest = _digest(saved)
    head = connection.execute(
        "SELECT commit_id, snapshot_digest, delta_depth, delta_bytes "
        "FROM native_commit_heads WHERE scope=? AND run_id=?",
        (scope, run_id),
    ).fetchone()
    if head is None:
        return saved, digest, 0, 0
    base, base_digest, depth, accumulated = head
    if base_digest != _digest(previous) or depth >= MAX_DELTA_DEPTH:
        # Legacy field upgrades / explicit host maintenance may replace the
        # current row without rewriting prior retry results. Start a checkpoint
        # rather than deriving a patch from a different historical image.
        return saved, digest, 0, 0
    delta = _json(
        {
            "format": HISTORY_FORMAT,
            "base_commit_id": base,
            "base_digest": base_digest,
            "snapshot_digest": digest,
            "patch": _difference(json.loads(previous), json.loads(saved)),
        }
    )
    size = len(delta.encode("utf-8"))
    # Adaptive checkpoints avoid repeatedly writing an ever-growing snapshot
    # at a small fixed interval. A depth bound also caps old retry read work.
    if accumulated + size >= len(saved.encode("utf-8")):
        return saved, digest, 0, 0
    return delta, digest, depth + 1, accumulated + size


def decode_snapshot(connection, scope: str, run_id: str, commit_id: str) -> RunSnapshot:
    """Reconstruct the exact result of an earlier accepted commit, never replay I/O."""
    deltas = []
    seen = set()
    try:
        for _ in range(MAX_DELTA_DEPTH + 1):
            if commit_id in seen:
                raise StoreConflict("commit history contains a cycle")
            seen.add(commit_id)
            row = connection.execute(
                "SELECT payload, snapshot FROM native_commits "
                "WHERE scope=? AND run_id=? AND commit_id=?",
                (scope, run_id, commit_id),
            ).fetchone()
            if row is None:
                raise StoreConflict("commit history base is missing")
            expected_digest = _stored_snapshot_digest(row[0])
            data = json.loads(row[1])
            if not isinstance(data, dict):
                raise StoreConflict("invalid commit history snapshot")
            if data.get("format") != HISTORY_FORMAT:
                if expected_digest is not None and _digest(_json(data)) != expected_digest:
                    raise StoreConflict("commit history checkpoint digest mismatch")
                for delta, expected_digest in reversed(deltas):
                    if _digest(_json(data)) != delta["base_digest"]:
                        raise StoreConflict("commit history base digest mismatch")
                    data = _apply(data, delta["patch"])
                    if _digest(_json(data)) != delta["snapshot_digest"]:
                        raise StoreConflict("commit history snapshot digest mismatch")
                    if expected_digest is not None and _digest(_json(data)) != expected_digest:
                        raise StoreConflict("commit history result digest mismatch")
                snapshot = RunSnapshot.model_validate(data)
                if (
                    snapshot.run_id != run_id
                    or _json(snapshot.scope.model_dump(mode="json")) != scope
                ):
                    raise StoreConflict("commit history identity mismatch")
                return snapshot
            deltas.append((data, expected_digest))
            commit_id = data["base_commit_id"]
        raise StoreConflict("commit history checkpoint depth exceeded")
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise StoreConflict("invalid commit history snapshot") from exc
