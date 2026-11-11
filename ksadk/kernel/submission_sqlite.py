"""SQLite native submission reservations in the existing ledger transaction."""

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
from ksadk.kernel.store import now_iso


class SQLiteSubmissionStoreMixin:
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
        async with self._write_lock:
            connection = await self._begin()
            try:
                row = await self._interaction_row_for_guard(
                    connection, submission.interaction_id, guard
                )
                if row is None:
                    raise InvalidCommandError("Unknown interaction")
                await self._check_interaction_guard(
                    connection, row["agent_instance_id"], row["session_id"], guard
                )
                current = self._row_to_record(row)
                assert current is not None
                if release:
                    updated, receipt = plan_release(current, submission)
                    reservation = SubmissionReservation(receipt, True)
                else:
                    recovery_run = None
                    if needs_recovery_check(current, guard.fencing_token):
                        run_row = await self._fetchone(connection, "SELECT * FROM kernel_runs WHERE run_id=?", (current.run_id,))
                        if run_row is not None:
                            recovery_run = {**dict(run_row), "metadata": json.loads(run_row["metadata_json"])}
                    previous = await self._fetchone(
                        connection,
                        "SELECT submission_digest, receipt_json FROM kernel_interaction_submissions"
                        " WHERE tenant_id=? AND interaction_id=? AND idempotency_key=?",
                        (current.tenant_id, current.interaction_id, submission.idempotency_key),
                    )
                    existing = (
                        (
                            previous["submission_digest"],
                            InteractionReceipt.model_validate_json(previous["receipt_json"]),
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
                        "UPDATE kernel_interactions SET status=?, continuation_json=?,"
                        " fencing_token=?, updated_at=? WHERE tenant_id=? AND interaction_id=?",
                        (
                            updated.status,
                            json.dumps(updated.continuation_metadata, allow_nan=False),
                            guard.fencing_token,
                            now_iso(),
                            current.tenant_id,
                            current.interaction_id,
                        ),
                    )
                    await connection.execute(
                        "INSERT INTO kernel_interaction_submissions"
                        " (tenant_id, interaction_id, idempotency_key,"
                        " submission_digest, receipt_json)"
                        " VALUES (?,?,?,?,?) ON CONFLICT(tenant_id,interaction_id,idempotency_key)"
                        " DO UPDATE SET receipt_json=excluded.receipt_json",
                        (
                            current.tenant_id,
                            current.interaction_id,
                            submission.idempotency_key,
                            submission_digest(submission),
                            reservation.receipt.model_dump_json(),
                        ),
                    )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return reservation
