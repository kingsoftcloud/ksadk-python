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
    LocalExecutionGrantClock,
    admission_operation_digest,
    command_grant_error,
    execution_grant_id,
    grant_deadline_budget,
    grant_operation_digest,
    grant_renewal_digest,
    make_grant_barrier,
    renew_grant,
    require_grant_scope,
    transition_admission,
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
  attempt_epoch INTEGER CHECK (attempt_epoch > 0),
  expires_at TEXT,
  state TEXT NOT NULL CHECK (state IN ('active','suspended','revoked')),
  revision INTEGER NOT NULL CHECK (revision > 0),
  admission_allowed INTEGER NOT NULL DEFAULT 1 CHECK (admission_allowed IN (0,1)),
  admission_revision INTEGER NOT NULL DEFAULT 1 CHECK (admission_revision > 0),
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


class SQLiteExecutionGrantMixin(LocalExecutionGrantClock):
    async def _migrate_execution_grants(self, connection) -> None:
        # Called within BEGIN IMMEDIATE: two processes cannot race ALTER TABLE.
        cursor = await connection.execute("PRAGMA table_info(kernel_execution_grants)")
        columns = {row["name"] for row in await cursor.fetchall()}
        await cursor.close()
        if "attempt_epoch" not in columns:
            await connection.execute(
                "ALTER TABLE kernel_execution_grants ADD COLUMN attempt_epoch INTEGER "
                "CHECK (attempt_epoch > 0)"
            )
        if "expires_at" not in columns:
            await connection.execute(
                "ALTER TABLE kernel_execution_grants ADD COLUMN expires_at TEXT"
            )
        if "admission_allowed" not in columns:
            await connection.execute(
                "ALTER TABLE kernel_execution_grants ADD COLUMN admission_allowed "
                "INTEGER NOT NULL DEFAULT 1 CHECK (admission_allowed IN (0,1))"
            )
        if "admission_revision" not in columns:
            await connection.execute(
                "ALTER TABLE kernel_execution_grants ADD COLUMN admission_revision "
                "INTEGER NOT NULL DEFAULT 1 CHECK (admission_revision > 0)"
            )

    async def _sqlite_grant_record(self, connection, grant_id: str) -> ExecutionGrantRecord | None:
        row = await self._fetchone(
            connection, "SELECT * FROM kernel_execution_grants WHERE grant_id=?", (grant_id,)
        )
        return ExecutionGrantRecord.model_validate(dict(row)) if row is not None else None

    async def ensure_execution_grant(
        self,
        spec: ExecutionGrantSpec,
        *,
        expires_at: str | None = None,
        remaining_ttl_seconds: float | None = None,
    ) -> ExecutionGrantRecord:
        observed_deadline = grant_deadline_budget(remaining_ttl_seconds)
        async with self._write_lock:
            connection = await self._begin()
            try:
                record = await self._sqlite_grant_record(connection, spec.grant_id)
                created = record is None
                if record is not None:
                    require_grant_scope(record, spec)
                else:
                    record = ExecutionGrantRecord(
                        **spec.model_dump(),
                        expires_at=expires_at,
                        created_at=now_iso(),
                        updated_at=now_iso(),
                    )
                    await connection.execute(
                        "INSERT INTO kernel_execution_grants (grant_id, tenant_id, "
                        "agent_instance_id, "
                        "session_id, owner_ref, attempt_epoch, expires_at, state, revision, "
                        "created_at, updated_at) VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?)",
                        tuple(
                            getattr(record, name)
                            for name in (
                                "grant_id",
                                "tenant_id",
                                "agent_instance_id",
                                "session_id",
                                "owner_ref",
                                "attempt_epoch",
                                "expires_at",
                                "state",
                                "revision",
                                "created_at",
                                "updated_at",
                            )
                        ),
                    )
                await connection.commit()
                if created:
                    self._anchor_grant(record, observed_deadline=observed_deadline)
                return record
            except BaseException:
                await connection.rollback()
                raise

    async def require_execution_grant(self, spec: ExecutionGrantSpec) -> ExecutionGrantRecord:
        async with self._write_lock:
            connection = await self._begin()
            try:
                record = await self._sqlite_grant_record(connection, spec.grant_id)
                if record is None:
                    raise ExecutionGrantBlocked("not_found")
                require_grant_scope(record, spec)
                self._require_local_grant(record)
                await connection.commit()
                return record
            except BaseException:
                await connection.rollback()
                raise

    async def lookup_execution_grant_operation(
        self,
        spec: ExecutionGrantSpec,
        *,
        idempotency_key: str,
    ) -> ExecutionGrantBarrier | None:
        async with self._write_lock:
            connection = await self._begin()
            try:
                record = await self._sqlite_grant_record(connection, spec.grant_id)
                if record is None:
                    await connection.commit()
                    return None
                require_grant_scope(record, spec)
                row = await self._fetchone(
                    connection,
                    "SELECT receipt_json FROM kernel_execution_grant_operations "
                    "WHERE grant_id=? AND idempotency_key=?",
                    (spec.grant_id, idempotency_key),
                )
                await connection.commit()
                return (
                    ExecutionGrantBarrier.model_validate_json(row["receipt_json"]) if row else None
                )
            except BaseException:
                await connection.rollback()
                raise

    async def renew_execution_grant(
        self,
        spec: ExecutionGrantSpec,
        *,
        expected_revision: int,
        expires_at: str,
        renewal_id: str,
        remaining_ttl_seconds: float | None = None,
    ) -> ExecutionGrantBarrier:
        observed_deadline = grant_deadline_budget(remaining_ttl_seconds)
        digest = grant_renewal_digest(
            spec, expected_revision=expected_revision, expires_at=expires_at, renewal_id=renewal_id
        )
        async with self._write_lock:
            connection = await self._begin()
            try:
                record = await self._sqlite_grant_record(connection, spec.grant_id)
                if record is None:
                    raise InvalidCommandError("execution grant not found")
                require_grant_scope(record, spec)
                previous = await self._fetchone(
                    connection,
                    "SELECT * FROM kernel_execution_grant_operations "
                    "WHERE grant_id=? AND idempotency_key=?",
                    (spec.grant_id, renewal_id),
                )
                if previous:
                    if previous["request_digest"] != digest:
                        raise InvalidCommandError("execution grant idempotency conflict")
                    await connection.commit()
                    return ExecutionGrantBarrier.model_validate_json(previous["receipt_json"])
                self._require_local_renewal(record)
                updated = renew_grant(
                    record,
                    expected_revision=expected_revision,
                    expires_at=expires_at,
                    now=now_iso(),
                )
                await connection.execute(
                    "UPDATE kernel_execution_grants SET expires_at=?, revision=?, updated_at=? "
                    "WHERE grant_id=?",
                    (updated.expires_at, updated.revision, updated.updated_at, spec.grant_id),
                )
                barrier = await self._sqlite_grant_barrier(connection, updated)
                await connection.execute(
                    "INSERT INTO kernel_execution_grant_operations "
                    "(grant_id,idempotency_key,request_digest,receipt_json) VALUES (?,?,?,?)",
                    (spec.grant_id, renewal_id, digest, barrier.model_dump_json()),
                )
                await connection.commit()
                self._anchor_grant(updated, observed_deadline=observed_deadline)
                return barrier
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

    async def set_execution_admission(
        self,
        spec: ExecutionGrantSpec,
        allowed: bool,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> ExecutionGrantBarrier:
        if not idempotency_key or not idempotency_key.strip():
            raise InvalidCommandError("admission mutation requires idempotency_key")
        digest = admission_operation_digest(spec, allowed, expected_revision)
        async with self._write_lock:
            connection = await self._begin()
            try:
                record = await self._sqlite_grant_record(connection, spec.grant_id)
                if record is None:
                    raise InvalidCommandError("execution grant not found")
                require_grant_scope(record, spec)
                previous = await self._fetchone(
                    connection,
                    "SELECT * FROM kernel_execution_grant_operations "
                    "WHERE grant_id=? AND idempotency_key=?",
                    (spec.grant_id, idempotency_key),
                )
                if previous:
                    if previous["request_digest"] != digest:
                        raise InvalidCommandError("execution grant idempotency conflict")
                    await connection.commit()
                    return ExecutionGrantBarrier.model_validate_json(previous["receipt_json"])
                self._require_local_grant(record)
                updated = transition_admission(record, allowed, expected_revision, now_iso())
                await connection.execute(
                    "UPDATE kernel_execution_grants SET admission_allowed=?, "
                    "admission_revision=?, updated_at=? WHERE grant_id=?",
                    (
                        int(updated.admission_allowed),
                        updated.admission_revision,
                        updated.updated_at,
                        spec.grant_id,
                    ),
                )
                barrier = await self._sqlite_grant_barrier(connection, updated)
                await connection.execute(
                    "INSERT INTO kernel_execution_grant_operations "
                    "(grant_id,idempotency_key,request_digest,receipt_json) VALUES (?,?,?,?)",
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
        record = await self._sqlite_grant_record(connection, grant_id)
        return command_grant_error(command, record) or self._local_grant_error(record)

    async def _sqlite_require_claim_grant(self, connection, row) -> None:
        command = AgentControlCommand.model_validate_json(row["payload_json"])
        grant_id = execution_grant_id(command)
        if not grant_id:
            return
        record = await self._sqlite_grant_record(connection, grant_id)
        error = command_grant_error(command, record) or self._local_grant_error(record)
        if (
            row["status"] == "claimed"
            and record is not None
            and record.expires_at is None
            and error in (None, "execution_grant_revoked")
        ):
            return
        if error:
            raise ExecutionGrantBlocked(
                error.removeprefix("execution_grant_"), message_id=row["message_id"]
            )
        if record.state == "suspended":
            raise ExecutionGrantBlocked("suspended", message_id=row["message_id"])
        if row["status"] == "claimed":
            return
        if not record.admission_allowed:
            raise ExecutionGrantBlocked("admission_paused", message_id=row["message_id"])
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
