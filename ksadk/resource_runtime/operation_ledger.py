"""Durable duplicate suppression for host-authorized resource writes.

This ledger is not an approval authority. The host supplies a stable event ID
across checkpoint recovery and an identity/binding digest after admission.
Claiming commits before dispatch. A lost claimant leaves an unknown operation;
opening the database never grants permission to resend it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ksadk.resource_runtime.memory_receipts import MemoryMutationReceipt

OperationState = Literal["unknown", "accepted_pending", "searchable", "succeeded", "failed"]
_TRANSITIONS = {
    "unknown": {"accepted_pending", "searchable", "succeeded", "failed"},
    "accepted_pending": {"searchable", "failed"},
    "searchable": set(),
    "succeeded": set(),
    "failed": set(),
}


@dataclass(frozen=True)
class OperationReceipt:
    operation_id: str
    state: OperationState
    claimed: bool


class OperationConflict(ValueError):
    """The same event was reused with different arguments or an invalid transition."""


class OperationLedger:
    """SQLite ledger in a host-owned private directory; no request bodies stored.

    Each method uses its own transaction so independent runtime processes share
    duplicate suppression. Retention must cover all recoverable checkpoints.
    Callers may record failed only after authoritative failure, never timeout.
    """

    def __init__(self, directory: Path):
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or directory.stat().st_mode & 0o077:
            raise ValueError("Operation ledger requires a private directory")
        self.path = directory / "resource-operations.sqlite3"
        if self.path.is_symlink():
            raise ValueError("Operation ledger cannot be a symbolic link")
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS operations ("
                "id TEXT PRIMARY KEY, request_digest TEXT NOT NULL, state TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS operation_owners ("
                "id TEXT PRIMARY KEY, authority_digest TEXT NOT NULL, operation TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS memory_mutation_results ("
                "id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
        self.path.chmod(0o600)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _key(authority_digest: str, event_id: str, operation: str) -> str:
        if not all(
            isinstance(v, str) and 0 < len(v) <= 1024
            for v in (authority_digest, event_id, operation)
        ):
            raise ValueError("Invalid operation identity")
        payload = json.dumps(
            ["resource-operation-v1", authority_digest, event_id, operation],
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def claim(
        self, *, authority_digest: str, event_id: str, operation: str, arguments: dict
    ) -> OperationReceipt:
        operation_id = self._key(authority_digest, event_id, operation)
        encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"), allow_nan=False)
        request_digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT request_digest, state FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
            if row is not None:
                if row[0] != request_digest:
                    raise OperationConflict("RESOURCE_OPERATION_ARGUMENTS_CHANGED")
                connection.execute(
                    "INSERT OR IGNORE INTO operation_owners VALUES (?, ?, ?)",
                    (operation_id, authority_digest, operation),
                )
                return OperationReceipt(operation_id, row[1], False)
            connection.execute(
                "INSERT INTO operations VALUES (?, ?, 'unknown')", (operation_id, request_digest)
            )
            connection.execute(
                "INSERT INTO operation_owners VALUES (?, ?, ?)",
                (operation_id, authority_digest, operation),
            )
        return OperationReceipt(operation_id, "unknown", True)

    def lookup(
        self, operation_id: str, *, authority_digest: str, operation: str
    ) -> OperationReceipt | None:
        """Opaque IDs alone grant no access. Legacy rows without owners stay hidden."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT o.state FROM operations o JOIN operation_owners p ON o.id=p.id "
                "WHERE o.id=? AND p.authority_digest=? AND p.operation=?",
                (operation_id, authority_digest, operation),
            ).fetchone()
        return None if row is None else OperationReceipt(operation_id, row[0], False)

    def record(self, operation_id: str, state: OperationState) -> OperationReceipt:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
            if row is None or state not in _TRANSITIONS:
                raise OperationConflict("RESOURCE_OPERATION_UNKNOWN")
            if state != row[0] and state not in _TRANSITIONS[row[0]]:
                raise OperationConflict("RESOURCE_OPERATION_STATE_CONFLICT")
            connection.execute(
                "UPDATE operations SET state = ? WHERE id = ?", (state, operation_id)
            )
        return OperationReceipt(operation_id, state, False)

    def record_memory_mutation(
        self,
        result: MemoryMutationReceipt,
        *,
        authority_digest: str,
        operation: str,
    ) -> OperationReceipt:
        """Commit terminal state and returned record IDs in one transaction."""
        result = MemoryMutationReceipt.model_validate(result.model_dump())
        if operation not in {"update_memory", "delete_memory"} or result.status == "unknown":
            raise OperationConflict("RESOURCE_OPERATION_RESULT_INVALID")
        if operation == "delete_memory" and result.new_memory_id:
            raise OperationConflict("RESOURCE_OPERATION_RESULT_INVALID")
        payload = result.model_dump_json(by_alias=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT o.state FROM operations o JOIN operation_owners p ON o.id=p.id "
                "WHERE o.id=? AND p.authority_digest=? AND p.operation=?",
                (result.operation_id, authority_digest, operation),
            ).fetchone()
            if row is None:
                raise OperationConflict("RESOURCE_OPERATION_UNKNOWN")
            if result.status != row[0] and result.status not in _TRANSITIONS[row[0]]:
                raise OperationConflict("RESOURCE_OPERATION_STATE_CONFLICT")
            previous = connection.execute(
                "SELECT payload FROM memory_mutation_results WHERE id=?",
                (result.operation_id,),
            ).fetchone()
            if previous is not None and previous[0] != payload:
                raise OperationConflict("RESOURCE_OPERATION_RESULT_CONFLICT")
            connection.execute(
                "INSERT OR IGNORE INTO memory_mutation_results VALUES (?, ?)",
                (result.operation_id, payload),
            )
            connection.execute(
                "UPDATE operations SET state=? WHERE id=?",
                (result.status, result.operation_id),
            )
        return OperationReceipt(result.operation_id, result.status, False)

    def memory_mutation_result(
        self,
        operation_id: str,
        *,
        authority_digest: str,
        operation: str,
    ) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT r.payload, o.state FROM memory_mutation_results r "
                "JOIN operations o ON o.id=r.id JOIN operation_owners p ON p.id=r.id "
                "WHERE r.id=? AND p.authority_digest=? AND p.operation=?",
                (operation_id, authority_digest, operation),
            ).fetchone()
        if row is None:
            return None
        result = MemoryMutationReceipt.model_validate_json(row[0])
        if result.operation_id != operation_id or result.status != row[1]:
            raise OperationConflict("RESOURCE_OPERATION_RESULT_CONFLICT")
        return result.model_dump(by_alias=True, mode="json")
