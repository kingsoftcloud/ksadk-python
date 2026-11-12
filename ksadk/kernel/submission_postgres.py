"""PostgreSQL native reservations with the existing activation fence and row lock."""

from __future__ import annotations

import json

from ksadk.interaction.contracts import InteractionReceipt
from ksadk.interaction.ledger import submission_digest
from ksadk.interaction.submission import (
    SubmissionReservation,
    needs_recovery_check,
    plan_release,
    plan_reservation,
)
from ksadk.kernel.errors import InvalidCommandError


class PostgresSubmissionStoreMixin:
    async def reserve_submission(self, submission, *, guard, command_id=""):
        return await self._native_submission_transaction(
            submission, guard=guard, command_id=command_id, release=False
        )

    async def release_submission(self, submission, *, guard):
        reservation = await self._native_submission_transaction(
            submission, guard=guard, command_id="", release=True
        )
        return reservation.receipt

    async def _native_submission_transaction(self, submission, *, guard, command_id, release):
        async with self._connection(write=True) as connection:
            async with connection.transaction():
                row = await self._interaction_row_for_guard(
                    connection, submission.interaction_id, guard
                )
                if row is None:
                    raise InvalidCommandError("Unknown interaction")
                await self._assert_interaction_guard(
                    connection, row["agent_instance_id"], row["session_id"], guard
                )
                current = self._interaction_row_to_record(row)
                if release:
                    updated, receipt = plan_release(current, submission)
                    reservation = SubmissionReservation(receipt, True)
                else:
                    recovery_run = None
                    if needs_recovery_check(current, guard.fencing_token):
                        run_row = await connection.fetchrow(
                            "SELECT * FROM kernel_runs WHERE run_id=$1 FOR SHARE", current.run_id
                        )
                        if run_row is not None:
                            recovery_run = {**dict(run_row), "metadata": json.loads(run_row["metadata"])}
                    previous = await connection.fetchrow(
                        "SELECT submission_digest, receipt FROM kernel_interaction_submissions"
                        " WHERE tenant_id=$1 AND interaction_id=$2 AND idempotency_key=$3",
                        current.tenant_id,
                        current.interaction_id,
                        submission.idempotency_key,
                    )
                    existing = (
                        (
                            previous["submission_digest"],
                            InteractionReceipt.model_validate_json(previous["receipt"]),
                        )
                        if previous
                        else None
                    )
                    updated, reservation = plan_reservation(
                        current,
                        submission,
                        existing=existing,
                        command_id=command_id,
                        fence=guard.fencing_token,
                        recovery_run=recovery_run,
                    )
                if reservation.acquired:
                    await connection.execute(
                        "UPDATE kernel_interactions SET status=$1, continuation_metadata=$2::jsonb,"
                        " fencing_token=$3, updated_at=now()"
                        " WHERE tenant_id=$4 AND interaction_id=$5",
                        updated.status,
                        json.dumps(updated.continuation_metadata, allow_nan=False),
                        guard.fencing_token,
                        current.tenant_id,
                        current.interaction_id,
                    )
                    await connection.execute(
                        "INSERT INTO kernel_interaction_submissions"
                        " (tenant_id, interaction_id, idempotency_key, submission_digest, receipt)"
                        " VALUES ($1,$2,$3,$4,$5::jsonb)"
                        " ON CONFLICT(tenant_id,interaction_id,idempotency_key)"
                        " DO UPDATE SET receipt=excluded.receipt",
                        current.tenant_id,
                        current.interaction_id,
                        submission.idempotency_key,
                        submission_digest(submission),
                        reservation.receipt.model_dump_json(),
                    )
        return reservation
