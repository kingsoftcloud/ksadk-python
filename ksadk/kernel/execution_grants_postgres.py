"""PostgreSQL grants: lock order is grant -> Inbox for every grant writer.

The grant row is locked FOR UPDATE before claim/revoke touch Inbox rows. Thus
concurrent writers cannot qualify work after a committed revocation barrier.
"""

from __future__ import annotations

import json

from ksadk.kernel.errors import InvalidCommandError
from ksadk.kernel.execution_grants import (
    ExecutionGrantBarrier,
    ExecutionGrantBlocked,
    ExecutionGrantRecord,
    ExecutionGrantSpec,
    GrantState,
    admission_operation_digest,
    command_grant_error,
    execution_grant_id,
    grant_deadline_budget,
    grant_operation_digest,
    grant_renewal_digest,
    make_grant_barrier,
    renew_grant,
    require_active_grant,
    require_grant_scope,
    transition_admission,
    transition_grant,
)
from ksadk.kernel.store import RunRecord, now_iso


class PostgresExecutionGrantMixin:
    async def _postgres_grant_now(self, connection) -> str:
        # clock_timestamp(), not transaction_timestamp(): lock waits consume TTL.
        return (await connection.fetchval("SELECT clock_timestamp()")).isoformat()

    def _require_grant_tenant(self, tenant_id: str) -> None:
        if tenant_id != self.tenant_id:
            raise InvalidCommandError("execution grant tenant does not match kernel store")

    async def _postgres_grant_record(
        self, connection, grant_id: str
    ) -> ExecutionGrantRecord | None:
        row = await connection.fetchrow(
            "SELECT * FROM kernel_execution_grants WHERE grant_id=$1 FOR UPDATE",
            grant_id,
        )
        return ExecutionGrantRecord.model_validate(dict(row)) if row is not None else None

    async def ensure_execution_grant(
        self,
        spec: ExecutionGrantSpec,
        *,
        expires_at: str | None = None,
        remaining_ttl_seconds: float | None = None,
    ) -> ExecutionGrantRecord:
        # PostgreSQL uses its authoritative clock, not a local transport anchor.
        grant_deadline_budget(remaining_ttl_seconds)
        self._require_grant_tenant(spec.tenant_id)
        now = now_iso()
        async with self._connection() as connection:
            async with connection.transaction():
                existing = await self._postgres_grant_record(connection, spec.grant_id)
                if existing is not None:
                    require_grant_scope(existing, spec)
                    return existing
                candidate = ExecutionGrantRecord(
                    **spec.model_dump(),
                    expires_at=expires_at,
                    created_at=now,
                    updated_at=now,
                )
                await connection.execute(
                    "INSERT INTO kernel_execution_grants (grant_id, tenant_id, "
                    "agent_instance_id, session_id, "
                    "owner_ref, state, revision, created_at, updated_at, "
                    "attempt_epoch, expires_at) VALUES "
                    "($1,$2,$3,$4,$5,'active',1,$6,$6,$7,$8) "
                    "ON CONFLICT (grant_id) DO NOTHING",
                    spec.grant_id,
                    spec.tenant_id,
                    spec.agent_instance_id,
                    spec.session_id,
                    spec.owner_ref,
                    now,
                    candidate.attempt_epoch,
                    candidate.expires_at,
                )
                record = await self._postgres_grant_record(connection, spec.grant_id)
                assert record is not None
                require_grant_scope(record, spec)
                return record

    async def require_execution_grant(self, spec: ExecutionGrantSpec) -> ExecutionGrantRecord:
        self._require_grant_tenant(spec.tenant_id)
        async with self._connection() as connection:
            async with connection.transaction():
                record = await self._postgres_grant_record(connection, spec.grant_id)
                if record is None:
                    raise ExecutionGrantBlocked("not_found")
                require_grant_scope(record, spec)
                require_active_grant(record, now=await self._postgres_grant_now(connection))
                return record

    async def lookup_execution_grant_operation(
        self,
        spec: ExecutionGrantSpec,
        *,
        idempotency_key: str,
    ) -> ExecutionGrantBarrier | None:
        self._require_grant_tenant(spec.tenant_id)
        async with self._connection() as connection:
            async with connection.transaction():
                record = await self._postgres_grant_record(connection, spec.grant_id)
                if record is None:
                    return None
                require_grant_scope(record, spec)
                row = await connection.fetchrow(
                    "SELECT receipt_json FROM kernel_execution_grant_operations "
                    "WHERE grant_id=$1 AND idempotency_key=$2",
                    spec.grant_id,
                    idempotency_key,
                )
                return (
                    ExecutionGrantBarrier.model_validate_json(row["receipt_json"]) if row else None
                )

    async def renew_execution_grant(
        self,
        spec: ExecutionGrantSpec,
        *,
        expected_revision: int,
        expires_at: str,
        renewal_id: str,
        remaining_ttl_seconds: float | None = None,
    ) -> ExecutionGrantBarrier:
        grant_deadline_budget(remaining_ttl_seconds)
        self._require_grant_tenant(spec.tenant_id)
        digest = grant_renewal_digest(
            spec, expected_revision=expected_revision, expires_at=expires_at, renewal_id=renewal_id
        )
        async with self._connection() as connection:
            async with connection.transaction():
                record = await self._postgres_grant_record(connection, spec.grant_id)
                if record is None:
                    raise InvalidCommandError("execution grant not found")
                require_grant_scope(record, spec)
                previous = await connection.fetchrow(
                    "SELECT * FROM kernel_execution_grant_operations "
                    "WHERE grant_id=$1 AND idempotency_key=$2",
                    spec.grant_id,
                    renewal_id,
                )
                if previous:
                    if previous["request_digest"] != digest:
                        raise InvalidCommandError("execution grant idempotency conflict")
                    return ExecutionGrantBarrier.model_validate_json(previous["receipt_json"])
                updated = renew_grant(
                    record,
                    expected_revision=expected_revision,
                    expires_at=expires_at,
                    now=await self._postgres_grant_now(connection),
                )
                await connection.execute(
                    "UPDATE kernel_execution_grants SET expires_at=$1, revision=$2, updated_at=$3 "
                    "WHERE grant_id=$4",
                    updated.expires_at,
                    updated.revision,
                    updated.updated_at,
                    spec.grant_id,
                )
                barrier = await self._postgres_grant_barrier(connection, updated)
                await connection.execute(
                    "INSERT INTO kernel_execution_grant_operations "
                    "(grant_id,idempotency_key,request_digest,receipt_json) VALUES ($1,$2,$3,$4)",
                    spec.grant_id,
                    renewal_id,
                    digest,
                    barrier.model_dump_json(),
                )
                return barrier

    async def _postgres_grant_barrier(self, connection, record) -> ExecutionGrantBarrier:
        rows = await connection.fetch(
            "SELECT * FROM kernel_inbox WHERE tenant_id=$1 AND agent_instance_id=$2 AND "
            "session_id=$3 "
            "ORDER BY accepted_seq",
            record.tenant_id,
            record.agent_instance_id,
            record.session_id,
        )
        messages = [self._row_to_message(row) for row in rows]
        rows = await connection.fetch(
            "SELECT * FROM kernel_runs WHERE tenant_id=$1 AND agent_instance_id=$2 AND "
            "session_id=$3",
            record.tenant_id,
            record.agent_instance_id,
            record.session_id,
        )
        runs = [
            RunRecord(
                run_id=row["run_id"],
                agent_instance_id=row["agent_instance_id"],
                session_id=row["session_id"],
                state=row["state"],
                metadata=json.loads(row["metadata"])
                if isinstance(row["metadata"], str)
                else dict(row["metadata"]),
            )
            for row in rows
        ]
        return make_grant_barrier(record, [m for m in messages if m], runs)

    async def get_execution_grant(self, spec: ExecutionGrantSpec) -> ExecutionGrantBarrier | None:
        self._require_grant_tenant(spec.tenant_id)
        async with self._connection() as connection:
            async with connection.transaction():
                record = await self._postgres_grant_record(connection, spec.grant_id)
                if record is None:
                    return None
                require_grant_scope(record, spec)
                return await self._postgres_grant_barrier(connection, record)

    async def set_execution_grant_state(
        self,
        spec: ExecutionGrantSpec,
        state: GrantState,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> ExecutionGrantBarrier:
        self._require_grant_tenant(spec.tenant_id)
        if not idempotency_key or not idempotency_key.strip():
            raise InvalidCommandError("grant mutation requires idempotency_key")
        digest = grant_operation_digest(spec, state, expected_revision)
        async with self._connection() as connection:
            async with connection.transaction():
                record = await self._postgres_grant_record(connection, spec.grant_id)
                if record is None:
                    raise InvalidCommandError("execution grant not found")
                require_grant_scope(record, spec)
                previous = await connection.fetchrow(
                    "SELECT * FROM kernel_execution_grant_operations WHERE grant_id=$1 AND "
                    "idempotency_key=$2",
                    spec.grant_id,
                    idempotency_key,
                )
                if previous is not None:
                    if previous["request_digest"] != digest:
                        raise InvalidCommandError("execution grant idempotency conflict")
                    return ExecutionGrantBarrier.model_validate_json(previous["receipt_json"])
                updated = transition_grant(record, state, expected_revision, now_iso())
                await connection.execute(
                    "UPDATE kernel_execution_grants SET state=$1, revision=$2, updated_at=$3 "
                    "WHERE grant_id=$4",
                    updated.state,
                    updated.revision,
                    updated.updated_at,
                    spec.grant_id,
                )
                if state == "revoked":
                    await connection.execute(
                        "UPDATE kernel_inbox SET status='discarded' WHERE tenant_id=$1 AND "
                        "agent_instance_id=$2 "
                        "AND session_id=$3 AND status='accepted' AND "
                        "payload->>'command_type'='enqueue' "
                        "AND payload->'payload'->>'execution_grant_id'=$4",
                        spec.tenant_id,
                        spec.agent_instance_id,
                        spec.session_id,
                        spec.grant_id,
                    )
                barrier = await self._postgres_grant_barrier(connection, updated)
                await connection.execute(
                    "INSERT INTO kernel_execution_grant_operations (grant_id, idempotency_key, "
                    "request_digest, receipt_json) "
                    "VALUES ($1,$2,$3,$4)",
                    spec.grant_id,
                    idempotency_key,
                    digest,
                    barrier.model_dump_json(),
                )
                return barrier

    async def set_execution_admission(
        self,
        spec: ExecutionGrantSpec,
        allowed: bool,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> ExecutionGrantBarrier:
        self._require_grant_tenant(spec.tenant_id)
        if not idempotency_key or not idempotency_key.strip():
            raise InvalidCommandError("admission mutation requires idempotency_key")
        digest = admission_operation_digest(spec, allowed, expected_revision)
        async with self._connection() as connection:
            async with connection.transaction():
                record = await self._postgres_grant_record(connection, spec.grant_id)
                if record is None:
                    raise InvalidCommandError("execution grant not found")
                require_grant_scope(record, spec)
                previous = await connection.fetchrow(
                    "SELECT * FROM kernel_execution_grant_operations "
                    "WHERE grant_id=$1 AND idempotency_key=$2",
                    spec.grant_id,
                    idempotency_key,
                )
                if previous:
                    if previous["request_digest"] != digest:
                        raise InvalidCommandError("execution grant idempotency conflict")
                    return ExecutionGrantBarrier.model_validate_json(previous["receipt_json"])
                updated = transition_admission(
                    record, allowed, expected_revision, await self._postgres_grant_now(connection)
                )
                await connection.execute(
                    "UPDATE kernel_execution_grants SET admission_allowed=$1, "
                    "admission_revision=$2, updated_at=$3 WHERE grant_id=$4",
                    updated.admission_allowed,
                    updated.admission_revision,
                    updated.updated_at,
                    spec.grant_id,
                )
                barrier = await self._postgres_grant_barrier(connection, updated)
                await connection.execute(
                    "INSERT INTO kernel_execution_grant_operations "
                    "(grant_id,idempotency_key,request_digest,receipt_json) VALUES ($1,$2,$3,$4)",
                    spec.grant_id,
                    idempotency_key,
                    digest,
                    barrier.model_dump_json(),
                )
                return barrier

    async def _postgres_lock_command_grant(self, connection, command):
        grant_id = execution_grant_id(command)
        if not grant_id:
            return None
        if command.tenant_id != self.tenant_id:
            return None
        return await self._postgres_grant_record(connection, grant_id)

    async def _postgres_admission_grant_error(self, connection, command) -> str | None:
        if not execution_grant_id(command):
            return None
        if command.tenant_id != self.tenant_id:
            return "execution_grant_scope_mismatch"
        record = await self._postgres_lock_command_grant(connection, command)
        return command_grant_error(command, record, now=await self._postgres_grant_now(connection))

    async def _postgres_require_claim_grant(self, connection, row, command, record) -> None:
        if not execution_grant_id(command):
            return
        error = command_grant_error(command, record, now=await self._postgres_grant_now(connection))
        if (
            row["status"] == "claimed"
            and record is not None
            and record.expires_at is None
            and error in (None, "execution_grant_revoked")
        ):
            return
        if error:
            raise ExecutionGrantBlocked(
                error.removeprefix("execution_grant_"), message_id=str(row["message_id"])
            )
        if record.state == "suspended":
            raise ExecutionGrantBlocked("suspended", message_id=str(row["message_id"]))
        if row["status"] == "claimed":
            return
        if not record.admission_allowed:
            raise ExecutionGrantBlocked("admission_paused", message_id=str(row["message_id"]))
        earlier = await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM kernel_inbox WHERE tenant_id=$1 "
            "AND agent_instance_id=$2 AND session_id=$3 AND accepted_seq < $4 "
            "AND status IN ('accepted','claimed') AND payload->>'command_type'='enqueue')",
            record.tenant_id,
            row["agent_instance_id"],
            row["session_id"],
            row["accepted_seq"],
        )
        busy = await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM kernel_runs WHERE tenant_id=$1 "
            "AND agent_instance_id=$2 AND session_id=$3 "
            "AND state IN ('pending','running','paused','waiting'))",
            record.tenant_id,
            row["agent_instance_id"],
            row["session_id"],
        )
        if earlier or busy:
            raise ExecutionGrantBlocked(
                "queued" if earlier else "busy",
                message_id=str(row["message_id"]),
            )
