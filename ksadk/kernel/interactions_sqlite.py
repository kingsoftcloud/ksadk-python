"""Sqlite Interaction/v1 transactions, extracted without behavior changes."""

from __future__ import annotations

import json
import time
from typing import Any

import aiosqlite

from ksadk.events.session_event import (
    SessionServiceEventStore,
    envelope_to_session_event,
    session_event_storage_id,
    session_event_to_envelope,
    validate_write_guard,
)
from ksadk.interaction.contracts import (
    InteractionReceipt,
    InteractionRecord,
    InteractionSubmission,
    is_terminal,
)
from ksadk.interaction.ledger import (
    ALREADY_RESOLVED,
    REQUEST_CONFLICT,
    REVISION_MISMATCH,
    ResolvedInteractionResponse,
    interaction_event,
    request_digest,
    requested_event_payload,
    resolve_outcome,
    submission_digest,
)
from ksadk.interaction.submission import check_reserved_submission, normalized_submission
from ksadk.kernel.contracts import (
    ActivationWriteGuard,
    AdmissionWriteGuard,
    SessionEventEnvelope,
)
from ksadk.kernel.errors import InvalidCommandError, StaleFenceError
from ksadk.kernel.store import (
    now_iso,
)
from ksadk.kernel.submission_sqlite import SQLiteSubmissionStoreMixin
from ksadk.sessions._local_tables import KSADK_EVENTS_TABLE, KSADK_SESSIONS_TABLE
from ksadk.sessions.base import SessionEvent
from ksadk.sessions.local_service import LocalSessionService


class SQLiteInteractionStoreMixin(SQLiteSubmissionStoreMixin):
    # -------------------------------------------------------------- interactions

    async def _check_interaction_guard(
        self,
        connection: aiosqlite.Connection,
        agent_instance_id: str,
        session_id: str,
        guard: ActivationWriteGuard | AdmissionWriteGuard,
    ) -> dict[str, Any]:
        row = await self._fetchone(
            connection,
            "SELECT * FROM kernel_activations WHERE activation_id=?",
            (guard.activation_id,),
        )
        activation = self._activation_row(row)
        if (
            activation is None
            or activation["lease_expires_at"] <= time.time()
            or activation["fencing_token"] != int(guard.fencing_token)
            or activation["agent_instance_id"] != agent_instance_id
            or activation["session_id"] != session_id
        ):
            raise StaleFenceError(
                "interaction write guard does not match the current lease",
                details={
                    "activation_id": guard.activation_id,
                    "fencing_token": int(guard.fencing_token),
                    "session_id": session_id,
                },
            )
        return activation

    def _require_local_transactional_event_store(self) -> None:
        """Ensure an Interaction fact shares this store's SQLite transaction.

        ``SessionServiceEventStore(LocalSessionService)`` normally owns the
        canonical local SessionEvent log.  Giving the kernel a different file
        would make a ledger row and its event independently committable, so it
        is an invalid Interaction/v1 configuration rather than a best-effort
        fallback.  Other session backends remain usable for the pre-existing
        non-transactional local control path, but not for durable interactions.
        """

        service = (
            self._events.session_service
            if isinstance(self._events, SessionServiceEventStore)
            else None
        )
        if (
            not isinstance(service, LocalSessionService)
            or service.db_path != self.db_path.resolve()
        ):
            raise RuntimeError(
                "SQLite InteractionLedger requires SessionEventStore backed by the "
                "same SQLite database"
            )

    async def _append_interaction_event_on(
        self,
        connection: aiosqlite.Connection,
        envelope: SessionEventEnvelope,
        guard: ActivationWriteGuard,
    ) -> SessionEventEnvelope:
        """Append the canonical SessionEvent in the ledger writer transaction."""

        self._require_local_transactional_event_store()
        validate_write_guard(envelope, guard)
        packed = envelope_to_session_event(envelope)
        storage_id = session_event_storage_id(envelope.session_id, str(envelope.event_id))
        session_row = await self._fetchone(
            connection,
            f"SELECT id FROM {KSADK_SESSIONS_TABLE} WHERE id=?",
            (envelope.session_id,),
        )
        if session_row is None:
            raise InvalidCommandError(
                f"session {envelope.session_id!r} does not exist in the shared event log"
            )
        existing = await self._fetchone(
            connection,
            f"SELECT id, author, event_type, content_json, timestamp, seq_id,"
            f" invocation_id, metadata_json FROM {KSADK_EVENTS_TABLE}"
            " WHERE session_id=? AND id=?",
            (envelope.session_id, storage_id),
        )
        if existing is not None:
            stored = SessionEvent(
                id=existing["id"],
                session_id=envelope.session_id,
                author=existing["author"],
                event_type=existing["event_type"],
                content=json.loads(existing["content_json"]),
                timestamp=float(existing["timestamp"]),
                seq_id=int(existing["seq_id"]),
                invocation_id=existing["invocation_id"],
                metadata=json.loads(existing["metadata_json"]),
            )
            persisted = session_event_to_envelope(stored)
            if persisted is None:  # pragma: no cover - only our packed rows use this id
                raise RuntimeError("kernel interaction event lost its envelope marker")
            SessionServiceEventStore._assert_same_fact(persisted, envelope)
            return persisted

        next_seq_row = await self._fetchone(
            connection,
            f"SELECT COALESCE(MAX(seq_id), 0) + 1 AS next_seq FROM {KSADK_EVENTS_TABLE}"
            " WHERE session_id=?",
            (envelope.session_id,),
        )
        next_seq = int(next_seq_row["next_seq"])
        packed.bind_seq_id(next_seq)
        await connection.execute(
            f"INSERT INTO {KSADK_EVENTS_TABLE} ("
            "id, session_id, author, event_type, content_json, timestamp, "
            "state_delta_json, seq_id, invocation_id, metadata_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                storage_id,
                envelope.session_id,
                packed.author,
                packed.event_type,
                json.dumps(packed.content, ensure_ascii=False),
                packed.timestamp,
                json.dumps(packed.state_delta, ensure_ascii=False),
                next_seq,
                packed.invocation_id,
                json.dumps(packed.metadata, ensure_ascii=False),
            ),
        )
        await connection.execute(
            f"UPDATE {KSADK_SESSIONS_TABLE} SET updated_at=? WHERE id=?",
            (time.time(), envelope.session_id),
        )
        persisted = session_event_to_envelope(packed)
        if persisted is None:  # pragma: no cover - packed by this method
            raise RuntimeError("kernel interaction event lost its envelope marker")
        return persisted

    async def _interaction_row_for_guard(
        self,
        connection: aiosqlite.Connection,
        interaction_id: str,
        guard: ActivationWriteGuard,
    ) -> aiosqlite.Row | None:
        """Find a public id through the trusted activation scope, not by id alone."""

        activation_row = await self._fetchone(
            connection,
            "SELECT * FROM kernel_activations WHERE activation_id=?",
            (guard.activation_id,),
        )
        activation = self._activation_row(activation_row)
        if (
            activation is None
            or activation["released"]
            or activation["lease_expires_at"] <= time.time()
            or activation["fencing_token"] != int(guard.fencing_token)
        ):
            raise StaleFenceError(
                "interaction write guard does not match the current lease",
                details={
                    "activation_id": guard.activation_id,
                    "fencing_token": int(guard.fencing_token),
                },
            )
        rows = await connection.execute_fetchall(
            "SELECT * FROM kernel_interactions WHERE interaction_id=?"
            " AND agent_instance_id=? AND session_id=? LIMIT 2",
            (
                interaction_id,
                activation["agent_instance_id"],
                activation["session_id"],
            ),
        )
        if len(rows) > 1:
            raise InvalidCommandError(
                f"interaction_id {interaction_id!r} is ambiguous in the activation scope",
                details={"reason": REQUEST_CONFLICT, "interaction_id": interaction_id},
            )
        return rows[0] if rows else None

    @staticmethod
    def _row_to_record(row: aiosqlite.Row | None) -> InteractionRecord | None:
        if row is None:
            return None
        from ksadk.interaction.contracts import InteractionPresentation

        presentation = None
        if row["presentation_json"]:
            presentation = InteractionPresentation.model_validate_json(row["presentation_json"])
        return InteractionRecord(
            interaction_id=row["interaction_id"],
            tenant_id=row["tenant_id"],
            agent_instance_id=row["agent_instance_id"],
            session_id=row["session_id"],
            run_id=row["run_id"],
            kind=row["kind"],
            request_schema=json.loads(row["request_schema_json"]),
            revision=int(row["revision"]),
            status=row["status"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            presentation=presentation,
            provider_id=row["provider_id"] or "",
            native_target=(
                json.loads(row["native_target_json"]) if row["native_target_json"] else None
            ),
            continuation_metadata=(
                json.loads(row["continuation_json"]) if row["continuation_json"] else None
            ),
        )

    def _record_values(self, record: InteractionRecord, *, request_digest_: str) -> tuple:
        return (
            record.interaction_id,
            record.tenant_id,
            record.agent_instance_id,
            record.session_id,
            record.run_id,
            record.kind,
            json.dumps(record.request_schema, ensure_ascii=False),
            (record.presentation.model_dump_json() if record.presentation is not None else None),
            record.revision,
            record.status,
            record.created_at,
            record.expires_at,
            record.provider_id,
            (
                json.dumps(record.native_target, ensure_ascii=False)
                if record.native_target is not None
                else None
            ),
            (
                json.dumps(record.continuation_metadata, ensure_ascii=False)
                if record.continuation_metadata is not None
                else None
            ),
            request_digest_,
            now_iso(),
        )

    async def request(
        self, record: InteractionRecord, *, guard: ActivationWriteGuard
    ) -> InteractionRecord:
        digest = request_digest(record)
        async with self._write_lock:
            connection = await self._begin()
            try:
                await self._check_interaction_guard(
                    connection, record.agent_instance_id, record.session_id, guard
                )
                existing = await self._fetchone(
                    connection,
                    "SELECT * FROM kernel_interactions WHERE tenant_id=? AND interaction_id=?",
                    (record.tenant_id, record.interaction_id),
                )
                if existing is not None:
                    if existing["request_digest"] != digest:
                        raise InvalidCommandError(
                            "interaction_id reused with a different request digest",
                            details={
                                "reason": REQUEST_CONFLICT,
                                "interaction_id": record.interaction_id,
                            },
                        )
                    await connection.commit()
                    stored = self._row_to_record(existing)
                    assert stored is not None
                    return stored
                await connection.execute(
                    "INSERT INTO kernel_interactions (interaction_id, tenant_id,"
                    " agent_instance_id, session_id, run_id, kind, request_schema_json,"
                    " presentation_json, revision, status, created_at, expires_at,"
                    " provider_id, native_target_json, continuation_json,"
                    " request_digest, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    self._record_values(record, request_digest_=digest),
                )
                # pending 行与 canonical requested 事实共用同一 SQLite commit。
                await self._append_interaction_event_on(
                    connection, requested_event_payload(record, now_iso()), guard
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return record

    async def resolve(
        self, submission: InteractionSubmission, *, guard: ActivationWriteGuard
    ) -> InteractionReceipt:
        sub_digest = submission_digest(submission)
        async with self._write_lock:
            connection = await self._begin()
            try:
                row = await self._interaction_row_for_guard(
                    connection, submission.interaction_id, guard
                )
                if row is None:
                    raise InvalidCommandError(
                        f"unknown interaction_id {submission.interaction_id!r}"
                    )
                await self._check_interaction_guard(
                    connection, row["agent_instance_id"], row["session_id"], guard
                )
                current = self._row_to_record(row)
                assert current is not None
                if is_terminal(current.status):
                    existing_sub = await self._fetchone(
                        connection,
                        "SELECT * FROM kernel_interaction_submissions WHERE tenant_id=?"
                        " AND interaction_id=? AND idempotency_key=?",
                        (
                            current.tenant_id,
                            current.interaction_id,
                            submission.idempotency_key,
                        ),
                    )
                    if (
                        existing_sub is not None
                        and existing_sub["submission_digest"] == sub_digest
                        and InteractionReceipt.model_validate_json(
                            existing_sub["receipt_json"]
                        ).status
                        == current.status
                    ):
                        await connection.commit()
                        return InteractionReceipt.model_validate_json(existing_sub["receipt_json"])
                    raise InvalidCommandError(
                        f"interaction already reached terminal status {current.status!r}",
                        details={
                            "reason": ALREADY_RESOLVED,
                            "interaction_id": current.interaction_id,
                        },
                    )
                if current.revision != submission.expected_revision:
                    raise InvalidCommandError(
                        "interaction revision does not match expected_revision",
                        details={
                            "reason": REVISION_MISMATCH,
                            "interaction_id": current.interaction_id,
                            "expected_revision": submission.expected_revision,
                            "current_revision": current.revision,
                        },
                    )
                check_reserved_submission(current, submission)
                response = normalized_submission(current, submission).response
                outcome = resolve_outcome(submission.action)
                updated = current.model_copy(
                    update={"status": "resolved", "revision": current.revision + 1}
                )
                stored = await self._append_interaction_event_on(
                    connection,
                    interaction_event(
                        updated,
                        event_type="interaction.resolved",
                        timestamp=now_iso(),
                        outcome=outcome,
                        response=response,
                        actor_ref="user",
                    ),
                    guard,
                )
                receipt = InteractionReceipt(
                    interaction_id=updated.interaction_id,
                    revision=updated.revision,
                    status="resolved",
                    outcome=outcome,  # type: ignore[arg-type]
                    event_id=str(stored.event_id),
                    accepted_seq=stored.seq,
                )
                await connection.execute(
                    "UPDATE kernel_interactions SET revision=?, status=?,"
                    " response_json=?, outcome=?, actor=?, event_id=?, accepted_seq=?,"
                    " fencing_token=?, updated_at=? WHERE tenant_id=? AND interaction_id=?",
                    (
                        updated.revision,
                        "resolved",
                        json.dumps(response, ensure_ascii=False),
                        outcome,
                        "user",
                        str(stored.event_id),
                        stored.seq,
                        int(guard.fencing_token),
                        now_iso(),
                        updated.tenant_id,
                        updated.interaction_id,
                    ),
                )
                await connection.execute(
                    "INSERT INTO kernel_interaction_submissions (tenant_id,"
                    " interaction_id, idempotency_key, submission_digest, receipt_json)"
                    " VALUES (?,?,?,?,?) ON CONFLICT(tenant_id,interaction_id,idempotency_key)"
                    " DO UPDATE SET receipt_json=excluded.receipt_json",
                    (
                        updated.tenant_id,
                        updated.interaction_id,
                        submission.idempotency_key,
                        sub_digest,
                        receipt.model_dump_json(),
                    ),
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return receipt

    async def _terminal_command(
        self,
        interaction_id: str,
        expected_revision: int,
        *,
        guard: ActivationWriteGuard,
        status: str,
        reason: str,
    ) -> InteractionReceipt:
        async with self._write_lock:
            connection = await self._begin()
            try:
                row = await self._interaction_row_for_guard(connection, interaction_id, guard)
                if row is None:
                    raise InvalidCommandError(f"unknown interaction_id {interaction_id!r}")
                await self._check_interaction_guard(
                    connection, row["agent_instance_id"], row["session_id"], guard
                )
                current = self._row_to_record(row)
                assert current is not None
                if is_terminal(current.status):
                    raise InvalidCommandError(
                        f"interaction already reached terminal status {current.status!r}",
                        details={
                            "reason": ALREADY_RESOLVED,
                            "interaction_id": current.interaction_id,
                        },
                    )
                if current.revision != expected_revision:
                    raise InvalidCommandError(
                        "interaction revision does not match expected_revision",
                        details={
                            "reason": REVISION_MISMATCH,
                            "interaction_id": current.interaction_id,
                            "expected_revision": expected_revision,
                            "current_revision": current.revision,
                        },
                    )
                updated = current.model_copy(
                    update={"status": status, "revision": current.revision + 1}
                )
                event_type = (
                    "interaction.cancelled" if status == "cancelled" else "interaction.expired"
                )
                stored = await self._append_interaction_event_on(
                    connection,
                    interaction_event(
                        updated,
                        event_type=event_type,
                        timestamp=now_iso(),
                        reason=reason,
                    ),
                    guard,
                )
                receipt = InteractionReceipt(
                    interaction_id=updated.interaction_id,
                    revision=updated.revision,
                    status=updated.status,  # type: ignore[arg-type]
                    outcome=updated.status,  # type: ignore[arg-type]
                    event_id=str(stored.event_id),
                    accepted_seq=stored.seq,
                )
                await connection.execute(
                    "UPDATE kernel_interactions SET revision=?, status=?, outcome=?,"
                    " event_id=?, accepted_seq=?, fencing_token=?, updated_at=?"
                    " WHERE tenant_id=? AND interaction_id=?",
                    (
                        updated.revision,
                        status,
                        status,
                        str(stored.event_id),
                        stored.seq,
                        int(guard.fencing_token),
                        now_iso(),
                        updated.tenant_id,
                        updated.interaction_id,
                    ),
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return receipt

    async def cancel(
        self, interaction_id: str, expected_revision: int, *, guard: ActivationWriteGuard
    ) -> InteractionReceipt:
        return await self._terminal_command(
            interaction_id,
            expected_revision,
            guard=guard,
            status="cancelled",
            reason="cancelled by owner",
        )

    async def expire(
        self, interaction_id: str, expected_revision: int, *, guard: ActivationWriteGuard
    ) -> InteractionReceipt:
        return await self._terminal_command(
            interaction_id,
            expected_revision,
            guard=guard,
            status="expired",
            reason="interaction expired",
        )

    async def read_public_interaction(
        self, interaction_id, *, tenant_id, agent_instance_id, session_id
    ):
        row = await self._fetchone(
            await self._connect(),
            "SELECT * FROM kernel_interactions WHERE interaction_id=?"
            " AND tenant_id=? AND agent_instance_id=? AND session_id=?",
            (interaction_id, tenant_id, agent_instance_id, session_id),
        )
        record = self._row_to_record(row)
        return {**record.public_request(), "status": record.status} if record is not None else None

    async def read_resolved_response(
        self, record: InteractionRecord
    ) -> ResolvedInteractionResponse | None:
        if record.status != "resolved":
            return None
        # The shared connection can see its own uncommitted writes. Wait for
        # the existing writer to commit/roll back before reading a recovery value.
        async with self._write_lock:
            row = await self._fetchone(
                await self._connect(),
                "SELECT response_json FROM kernel_interactions WHERE tenant_id=?"
                " AND interaction_id=? AND agent_instance_id=? AND session_id=?"
                " AND run_id=? AND kind=? AND revision=? AND status='resolved'"
                " AND response_json IS NOT NULL",
                (record.tenant_id, record.interaction_id, record.agent_instance_id,
                 record.session_id, record.run_id, record.kind, record.revision),
            )
        return ResolvedInteractionResponse(json.loads(row["response_json"])) if row else None

    async def get(
        self,
        interaction_id: str,
        *,
        tenant_id: str | None = None,
        agent_instance_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> InteractionRecord | None:
        """Return an interaction only inside a complete trusted scope.

        An omitted scope is retained for local compatibility but is fail-closed
        when the opaque id exists in more than one tenant.  Partial scope is
        never sufficient for a security-sensitive worker lookup.
        """

        scope = (tenant_id, agent_instance_id, session_id, run_id)
        connection = await self._connect()
        if any(value is not None for value in scope):
            if not all(value is not None for value in scope):
                raise InvalidCommandError(
                    "interaction lookup requires a complete trusted scope",
                    details={"interaction_id": interaction_id},
                )
            row = await self._fetchone(
                connection,
                "SELECT * FROM kernel_interactions WHERE interaction_id=?"
                " AND tenant_id=? AND agent_instance_id=? AND session_id=? AND run_id=?",
                (interaction_id, tenant_id, agent_instance_id, session_id, run_id),
            )
            return self._row_to_record(row)
        rows = await connection.execute_fetchall(
            "SELECT * FROM kernel_interactions WHERE interaction_id=? LIMIT 2",
            (interaction_id,),
        )
        if len(rows) > 1:
            raise InvalidCommandError(
                f"interaction_id {interaction_id!r} is ambiguous without trusted scope",
                details={"reason": REQUEST_CONFLICT, "interaction_id": interaction_id},
            )
        return self._row_to_record(rows[0]) if rows else None

    async def get_terminal_receipt(
        self,
        interaction_id: str,
        *,
        tenant_id: str,
        agent_instance_id: str,
        session_id: str,
        run_id: str,
    ) -> InteractionReceipt | None:
        """Read the committed outcome inside a complete trusted scope.

        Resolved alone does not mean approved. Pending/resolving records never
        yield an authorization receipt; callers must still match the request.
        """
        if not all(
            isinstance(value, str) and value
            for value in (interaction_id, tenant_id, agent_instance_id, session_id, run_id)
        ):
            raise InvalidCommandError("interaction receipt requires a complete trusted scope")
        row = await self._fetchone(
            await self._connect(),
            "SELECT * FROM kernel_interactions WHERE interaction_id=? AND tenant_id=?"
            " AND agent_instance_id=? AND session_id=? AND run_id=?",
            (interaction_id, tenant_id, agent_instance_id, session_id, run_id),
        )
        if row is None or not is_terminal(row["status"]):
            return None
        return InteractionReceipt(
            interaction_id=row["interaction_id"],
            revision=int(row["revision"]),
            status=row["status"],
            outcome=row["outcome"],
            event_id=row["event_id"],
            accepted_seq=row["accepted_seq"],
        )

    async def list_pending_interactions(
        self, tenant_id: str, session_id: str
    ) -> list[InteractionRecord]:
        connection = await self._connect()
        rows = await connection.execute_fetchall(
            "SELECT * FROM kernel_interactions WHERE tenant_id=? AND session_id=?"
            " AND status IN ('pending','resolving') ORDER BY created_at",
            (tenant_id, session_id),
        )
        records = [self._row_to_record(row) for row in rows]
        return [r for r in records if r is not None]
