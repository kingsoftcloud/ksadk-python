"""In-memory execution-grant transactions, using the Inbox session lock."""

from __future__ import annotations

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
from ksadk.kernel.state import InboxState
from ksadk.kernel.store import now_iso


class MemoryExecutionGrantMixin:
    async def ensure_execution_grant(self, spec: ExecutionGrantSpec) -> ExecutionGrantRecord:
        async with self._lock(spec.agent_instance_id, spec.session_id):
            existing = self._execution_grants.get(spec.grant_id)
            if existing is not None:
                require_grant_scope(existing, spec)
                return existing
            record = ExecutionGrantRecord(
                **spec.model_dump(), created_at=now_iso(), updated_at=now_iso()
            )
            self._execution_grants[spec.grant_id] = record
            return record

    def _memory_grant_barrier(self, record: ExecutionGrantRecord) -> ExecutionGrantBarrier:
        return make_grant_barrier(
            record,
            [
                self._to_message(row)
                for row in self._session_rows(record.agent_instance_id, record.session_id)
            ],
            [
                run
                for run in self._runs.values()
                if run.agent_instance_id == record.agent_instance_id
                and run.session_id == record.session_id
            ],
        )

    async def get_execution_grant(self, spec: ExecutionGrantSpec) -> ExecutionGrantBarrier | None:
        async with self._lock(spec.agent_instance_id, spec.session_id):
            record = self._execution_grants.get(spec.grant_id)
            if record is None:
                return None
            require_grant_scope(record, spec)
            return self._memory_grant_barrier(record)

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
        async with self._lock(spec.agent_instance_id, spec.session_id):
            record = self._execution_grants.get(spec.grant_id)
            if record is None:
                raise InvalidCommandError("execution grant not found")
            require_grant_scope(record, spec)
            operation_key = (spec.grant_id, idempotency_key)
            previous = self._execution_grant_operations.get(operation_key)
            if previous is not None:
                if previous[0] != digest:
                    raise InvalidCommandError("execution grant idempotency conflict")
                return previous[1]
            updated = transition_grant(record, state, expected_revision, now_iso())
            # No await between state change, queue invalidation and receipt.
            # claim_message uses exactly this same session lock.
            self._execution_grants[spec.grant_id] = updated
            if state == "revoked":
                for row in self._session_rows(spec.agent_instance_id, spec.session_id):
                    if (
                        row["status"] == InboxState.ACCEPTED
                        and execution_grant_id(row["command"]) == spec.grant_id
                    ):
                        row["status"] = InboxState.DISCARDED
            barrier = self._memory_grant_barrier(updated)
            self._execution_grant_operations[operation_key] = (digest, barrier)
            return barrier

    def _memory_admission_grant_error(self, command) -> str | None:
        grant_id = execution_grant_id(command)
        return (
            command_grant_error(command, self._execution_grants.get(grant_id)) if grant_id else None
        )

    def _memory_require_claim_grant(self, row) -> None:
        command = row["command"]
        grant_id = execution_grant_id(command)
        if not grant_id:
            return
        record = self._execution_grants.get(grant_id)
        error = command_grant_error(command, record)
        # A previously qualified command remains in flight across a barrier.
        if row["status"] == InboxState.CLAIMED and error in (None, "execution_grant_revoked"):
            return
        if error:
            raise ExecutionGrantBlocked(
                error.removeprefix("execution_grant_"), message_id=row["message_id"]
            )
        if record.state == "suspended":
            raise ExecutionGrantBlocked("suspended", message_id=row["message_id"])
        earlier = any(
            other["accepted_seq"] < row["accepted_seq"]
            and other["status"] in (InboxState.ACCEPTED, InboxState.CLAIMED)
            and other["command"].command_type == "enqueue"
            for other in self._session_rows(row["agent_instance_id"], row["session_id"])
        )
        busy = any(
            run.agent_instance_id == row["agent_instance_id"]
            and run.session_id == row["session_id"]
            and str(run.state) in {"pending", "running", "paused", "waiting"}
            for run in self._runs.values()
        )
        if earlier or busy:
            raise ExecutionGrantBlocked(
                "queued" if earlier else "busy", message_id=row["message_id"]
            )
