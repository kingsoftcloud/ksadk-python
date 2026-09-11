"""SQLite grants share BEGIN IMMEDIATE with admission and Inbox claim.

Receipts live in this same database, so a lost acknowledgement is resolved by
the original mutation key even after another transition or a process restart.
SessionEvent publication is not the authority for a grant barrier.
"""

from __future__ import annotations

from ksadk.kernel.contracts import AgentControlCommand
from ksadk.kernel.errors import InvalidCommandError
from ksadk.kernel.execution_grants import (
    ExecutionGrantBarrier,
    ExecutionGrantBlocked,
    ExecutionGrantRecord,
    ExecutionGrantSpec,
    GrantState,
    command_grant_error,
    execution_grant_id,
    grant_operation_digest,
    make_grant_barrier,
    require_grant_scope,
    transition_grant,
)
from ksadk.kernel.store import now_iso

EXECUTION_GRANT_SCHEMA = """
CREATE TABLE IF NOT EXISTS kernel_execution_grants (
  grant_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  agent_instance_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  owner_ref TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('active','suspended','revoked')),
  revision INTEGER NOT NULL CHECK (revision > 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kernel_execution_grants_scope
  ON kernel_execution_grants (tenant_id, agent_instance_id, session_id);
CREATE TABLE IF NOT EXISTS kernel_execution_grant_operations (
  grant_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  receipt_json TEXT NOT NULL,
  PRIMARY KEY (grant_id, idempotency_key)
);
"""


class SQLiteExecutionGrantMixin:
    async def _sqlite_grant_record(self, connection, grant_id: str) -> ExecutionGrantRecord | None:
        row = await self._fetchone(
            connection, "SELECT * FROM kernel_execution_grants WHERE grant_id=?", (grant_id,)
        )
        return ExecutionGrantRecord.model_validate(dict(row)) if row is not None else None

    async def ensure_execution_grant(self, spec: ExecutionGrantSpec) -> ExecutionGrantRecord:
        async with self._write_lock:
            connection = await self._begin()
            try:
                record = await self._sqlite_grant_record(connection, spec.grant_id)
                if record is not None:
                    require_grant_scope(record, spec)
                else:
                    record = ExecutionGrantRecord(
                        **spec.model_dump(), created_at=now_iso(), updated_at=now_iso()
                    )
                    await connection.execute(
                        "INSERT INTO kernel_execution_grants (grant_id, tenant_id, "
                        "agent_instance_id, "
                        "session_id, owner_ref, state, revision, created_at, updated_at) VALUES "
                        "(?,?,?,?,?,?,?,?,?)",
                        tuple(record.model_dump().values()),
                    )
                await connection.commit()
                return record
            except BaseException:
                await connection.rollback()
                raise

    async def _sqlite_grant_barrier(self, connection, record) -> ExecutionGrantBarrier:
        cursor = await connection.execute(
            "SELECT message_id FROM kernel_inbox WHERE agent_instance_id=? AND session_id=? "
            "ORDER BY accepted_seq",
            (record.agent_instance_id, record.session_id),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        messages = [await self.load_message(row["message_id"]) for row in rows]
        cursor = await connection.execute(
            "SELECT run_id FROM kernel_runs WHERE agent_instance_id=? AND session_id=?",
            (record.agent_instance_id, record.session_id),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        runs = [await self.load_run(row["run_id"]) for row in rows]
        return make_grant_barrier(record, [m for m in messages if m], [r for r in runs if r])

    async def get_execution_grant(self, spec: ExecutionGrantSpec) -> ExecutionGrantBarrier | None:
        async with self._write_lock:
            connection = await self._begin()
            try:
                record = await self._sqlite_grant_record(connection, spec.grant_id)
                if record is None:
                    await connection.commit()
                    return None
                require_grant_scope(record, spec)
                barrier = await self._sqlite_grant_barrier(connection, record)
                await connection.commit()
                return barrier
            except BaseException:
                await connection.rollback()
                raise

    async def set_execution_grant_state(
        self,
        spec: ExecutionGrantSpec,
        state: GrantState,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> ExecutionGrantBarrier:
        if not idempotency_key or not idempotency_key.strip():
            raise InvalidCommandError("grant mutation requires idempotency_key")
        digest = grant_operation_digest(spec, state, expected_revision)
        async with self._write_lock:
            connection = await self._begin()
            try:
                record = await self._sqlite_grant_record(connection, spec.grant_id)
                if record is None:
                    raise InvalidCommandError("execution grant not found")
                require_grant_scope(record, spec)
                previous = await self._fetchone(
                    connection,
                    "SELECT * FROM kernel_execution_grant_operations WHERE grant_id=? AND "
                    "idempotency_key=?",
                    (spec.grant_id, idempotency_key),
                )
                if previous is not None:
                    if previous["request_digest"] != digest:
                        raise InvalidCommandError("execution grant idempotency conflict")
                    await connection.commit()
                    return ExecutionGrantBarrier.model_validate_json(previous["receipt_json"])
                updated = transition_grant(record, state, expected_revision, now_iso())
                await connection.execute(
                    "UPDATE kernel_execution_grants SET state=?, revision=?, updated_at=? WHERE "
                    "grant_id=?",
                    (updated.state, updated.revision, updated.updated_at, spec.grant_id),
                )
                if state == "revoked":
                    await connection.execute(
                        "UPDATE kernel_inbox SET status='discarded' WHERE agent_instance_id=? "
                        "AND session_id=? "
                        "AND status='accepted' AND json_extract(payload_json, "
                        "'$.command_type')='enqueue' "
                        "AND json_extract(payload_json, '$.payload.execution_grant_id')=?",
                        (spec.agent_instance_id, spec.session_id, spec.grant_id),
                    )
                barrier = await self._sqlite_grant_barrier(connection, updated)
                await connection.execute(
                    "INSERT INTO kernel_execution_grant_operations "
                    "(grant_id, idempotency_key, request_digest, receipt_json) VALUES (?,?,?,?)",
                    (spec.grant_id, idempotency_key, digest, barrier.model_dump_json()),
                )
                await connection.commit()
                return barrier
            except BaseException:
                await connection.rollback()
                raise

    async def _sqlite_admission_grant_error(self, connection, command) -> str | None:
        grant_id = execution_grant_id(command)
        if not grant_id:
            return None
        return command_grant_error(command, await self._sqlite_grant_record(connection, grant_id))

    async def _sqlite_require_claim_grant(self, connection, row) -> None:
        command = AgentControlCommand.model_validate_json(row["payload_json"])
        grant_id = execution_grant_id(command)
        if not grant_id:
            return
        record = await self._sqlite_grant_record(connection, grant_id)
        error = command_grant_error(command, record)
        if row["status"] == "claimed" and error in (None, "execution_grant_revoked"):
            return
        if error:
            raise ExecutionGrantBlocked(
                error.removeprefix("execution_grant_"), message_id=row["message_id"]
            )
        if record.state == "suspended":
            raise ExecutionGrantBlocked("suspended", message_id=row["message_id"])
        earlier = await self._fetchone(
            connection,
            "SELECT 1 FROM kernel_inbox WHERE agent_instance_id=? AND session_id=? "
            "AND accepted_seq < ? AND status IN ('accepted','claimed') "
            "AND json_extract(payload_json, '$.command_type')='enqueue' LIMIT 1",
            (row["agent_instance_id"], row["session_id"], row["accepted_seq"]),
        )
        busy = await self._fetchone(
            connection,
            "SELECT 1 FROM kernel_runs WHERE agent_instance_id=? AND session_id=? "
            "AND state IN ('pending','running','paused','waiting') LIMIT 1",
            (row["agent_instance_id"], row["session_id"]),
        )
        if earlier or busy:
            raise ExecutionGrantBlocked(
                "queued" if earlier else "busy", message_id=row["message_id"]
            )
