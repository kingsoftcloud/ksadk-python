"""Postgres Interaction/v1 transactions, extracted without behavior changes."""

from __future__ import annotations

import json
from typing import Any

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
from ksadk.kernel.errors import InvalidCommandError, StaleFenceError
from ksadk.kernel.postgres_time import parse_ts as _parse_ts
from ksadk.kernel.store import (
    now_iso,
)
from ksadk.kernel.submission_postgres import PostgresSubmissionStoreMixin


class PostgresInteractionStoreMixin(PostgresSubmissionStoreMixin):
    # -------------------------------------------------------------- interactions

    async def _assert_interaction_guard(
        self, connection: Any, agent_instance_id: str, session_id: str, guard: Any
    ) -> dict[str, Any]:
        row = await connection.fetchrow(
            "SELECT activation_id, agent_instance_id, session_id, fencing_token,"
            " lease_expires_at, released FROM kernel_activations"
            " WHERE activation_id = $1 AND agent_instance_id = $2"
            " AND session_id = $3 AND (session_namespace=$4 OR session_namespace IS NULL) FOR SHARE",
            guard.activation_id,
            agent_instance_id,
            session_id,
            self._events._namespace,
        )
        if (
            row is None
            or row["released"]
            or row["lease_expires_at"] <= await connection.fetchval("SELECT clock_timestamp()")
            or int(row["fencing_token"]) != int(guard.fencing_token)
            or row["agent_instance_id"] != agent_instance_id
            or row["session_id"] != session_id
        ):
            raise StaleFenceError(
                "interaction write guard does not match the current lease",
                details={
                    "activation_id": guard.activation_id,
                    "fencing_token": int(guard.fencing_token),
                    "session_id": session_id,
                },
            )
        return dict(row)

    async def _interaction_row_for_guard(
        self, connection: Any, interaction_id: str, guard: Any
    ) -> Any | None:
        """Resolve a public interaction id within its fenced activation scope.

        Interaction ids are opaque browser-visible handles, not tenant grants.
        A Runtime mutation already has the Server-admitted activation guard, so
        select the row by that trusted AgentInstance/session before taking the
        row lock. If tenant-keyed records collide within that activation scope,
        refuse the ambiguous id instead of choosing a tenant's row arbitrarily.
        """

        activation = await connection.fetchrow(
            "SELECT activation_id, agent_instance_id, session_id, fencing_token,"
            " lease_expires_at, released FROM kernel_activations"
            " WHERE activation_id=$1 FOR SHARE",
            guard.activation_id,
        )
        if (
            activation is None
            or activation["released"]
            or activation["lease_expires_at"]
            <= await connection.fetchval("SELECT clock_timestamp()")
            or int(activation["fencing_token"]) != int(guard.fencing_token)
        ):
            raise StaleFenceError(
                "interaction write guard does not match the current lease",
                details={
                    "activation_id": guard.activation_id,
                    "fencing_token": int(guard.fencing_token),
                },
            )
        rows = await connection.fetch(
            "SELECT * FROM kernel_interactions WHERE interaction_id=$1"
            " AND agent_instance_id=$2 AND session_id=$3 LIMIT 2 FOR UPDATE",
            interaction_id,
            activation["agent_instance_id"],
            activation["session_id"],
        )
        if len(rows) > 1:
            raise InvalidCommandError(
                f"interaction_id {interaction_id!r} is ambiguous in the activation scope",
                details={"reason": REQUEST_CONFLICT, "interaction_id": interaction_id},
            )
        return rows[0] if rows else None

    @staticmethod
    def _interaction_row_to_record(row: Any) -> InteractionRecord:
        from ksadk.interaction.contracts import InteractionPresentation

        presentation = None
        if row["presentation"] is not None:
            presentation = InteractionPresentation.model_validate(json.loads(row["presentation"]))
        return InteractionRecord(
            interaction_id=row["interaction_id"],
            tenant_id=row["tenant_id"],
            agent_instance_id=row["agent_instance_id"],
            session_id=row["session_id"],
            run_id=row["run_id"],
            kind=row["kind"],
            request_schema=json.loads(row["request_schema"]),
            revision=int(row["revision"]),
            status=row["status"],
            created_at=row["created_at"].isoformat(),
            expires_at=(row["expires_at"].isoformat() if row["expires_at"] is not None else None),
            presentation=presentation,
            provider_id=row["provider_id"] or "",
            native_target=(
                json.loads(row["native_target"]) if row["native_target"] is not None else None
            ),
            continuation_metadata=(
                json.loads(row["continuation_metadata"])
                if row["continuation_metadata"] is not None
                else None
            ),
        )

    async def request(self, record: InteractionRecord, *, guard: Any) -> InteractionRecord:
        digest = request_digest(record)
        async with self._connection(write=True) as connection:
            async with connection.transaction():
                await self._assert_interaction_guard(
                    connection, record.agent_instance_id, record.session_id, guard
                )
                existing = await connection.fetchrow(
                    "SELECT * FROM kernel_interactions WHERE tenant_id=$1 AND interaction_id=$2",
                    record.tenant_id,
                    record.interaction_id,
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
                    return self._interaction_row_to_record(existing)
                await connection.execute(
                    """
                    INSERT INTO kernel_interactions (
                        interaction_id, tenant_id, agent_instance_id, session_id,
                        run_id, kind, request_schema, presentation, revision, status,
                        created_at, expires_at, provider_id, native_target,
                        continuation_metadata, request_digest, updated_at, session_namespace
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9, 'pending',
                        $10::timestamptz, $11::timestamptz, $12, $13::jsonb,
                        $14::jsonb, $15, now(), $16
                    )
                    """,
                    record.interaction_id,
                    record.tenant_id,
                    record.agent_instance_id,
                    record.session_id,
                    record.run_id,
                    record.kind,
                    json.dumps(record.request_schema, ensure_ascii=False),
                    (
                        record.presentation.model_dump_json()
                        if record.presentation is not None
                        else None
                    ),
                    record.revision,
                    _parse_ts(record.created_at),
                    _parse_ts(record.expires_at),
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
                    digest,
                    self._events._namespace,
                )
                # requested 事件与 pending 行同一事务：commit 前被 kill 无半状态。
                await self._events.append_on(
                    connection, requested_event_payload(record, now_iso()), guard
                )
        return record

    async def resolve(self, submission: InteractionSubmission, *, guard: Any) -> InteractionReceipt:
        sub_digest = submission_digest(submission)
        async with self._connection(write=True) as connection:
            async with connection.transaction():
                row = await self._interaction_row_for_guard(
                    connection, submission.interaction_id, guard
                )
                if row is None:
                    raise InvalidCommandError(
                        f"unknown interaction_id {submission.interaction_id!r}"
                    )
                await self._assert_interaction_guard(
                    connection, row["agent_instance_id"], row["session_id"], guard
                )
                current = self._interaction_row_to_record(row)
                if is_terminal(current.status):
                    existing_sub = await connection.fetchrow(
                        "SELECT submission_digest, receipt FROM"
                        " kernel_interaction_submissions WHERE tenant_id=$1"
                        " AND interaction_id=$2 AND idempotency_key=$3",
                        current.tenant_id,
                        current.interaction_id,
                        submission.idempotency_key,
                    )
                    if (
                        existing_sub is not None
                        and existing_sub["submission_digest"] == sub_digest
                        and InteractionReceipt.model_validate_json(existing_sub["receipt"]).status
                        == current.status
                    ):
                        return InteractionReceipt.model_validate(
                            json.loads(existing_sub["receipt"])
                        )
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
                stored = await self._events.append_on(
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
                    "UPDATE kernel_interactions SET revision=$1, status='resolved',"
                    " response=$2::jsonb, outcome=$3, actor=$4, event_id=$5::uuid,"
                    " accepted_seq=$6, fencing_token=$7, updated_at=now()"
                    " WHERE tenant_id=$8 AND interaction_id=$9",
                    updated.revision,
                    json.dumps(response, ensure_ascii=False),
                    outcome,
                    "user",
                    str(stored.event_id),
                    stored.seq,
                    int(guard.fencing_token),
                    updated.tenant_id,
                    updated.interaction_id,
                )
                await connection.execute(
                    "INSERT INTO kernel_interaction_submissions (tenant_id,"
                    " interaction_id, idempotency_key, submission_digest, receipt)"
                    " VALUES ($1, $2, $3, $4, $5::jsonb)"
                    " ON CONFLICT(tenant_id,interaction_id,idempotency_key)"
                    " DO UPDATE SET receipt=excluded.receipt",
                    updated.tenant_id,
                    updated.interaction_id,
                    submission.idempotency_key,
                    sub_digest,
                    receipt.model_dump_json(),
                )
        return receipt

    async def _terminal_command(
        self,
        interaction_id: str,
        expected_revision: int,
        *,
        guard: Any,
        status: str,
        reason: str,
    ) -> InteractionReceipt:
        async with self._connection(write=True) as connection:
            async with connection.transaction():
                row = await self._interaction_row_for_guard(connection, interaction_id, guard)
                if row is None:
                    raise InvalidCommandError(f"unknown interaction_id {interaction_id!r}")
                await self._assert_interaction_guard(
                    connection, row["agent_instance_id"], row["session_id"], guard
                )
                current = self._interaction_row_to_record(row)
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
                stored = await self._events.append_on(
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
                    "UPDATE kernel_interactions SET revision=$1, status=$2,"
                    " outcome=$3, event_id=$4::uuid, accepted_seq=$5,"
                    " fencing_token=$6, updated_at=now()"
                    " WHERE tenant_id=$7 AND interaction_id=$8",
                    updated.revision,
                    status,
                    status,
                    str(stored.event_id),
                    stored.seq,
                    int(guard.fencing_token),
                    updated.tenant_id,
                    updated.interaction_id,
                )
        return receipt

    async def cancel(
        self, interaction_id: str, expected_revision: int, *, guard: Any
    ) -> InteractionReceipt:
        return await self._terminal_command(
            interaction_id,
            expected_revision,
            guard=guard,
            status="cancelled",
            reason="cancelled by owner",
        )

    async def expire(
        self, interaction_id: str, expected_revision: int, *, guard: Any
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
        async with self._connection() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM kernel_interactions WHERE interaction_id=$1 AND tenant_id=$2"
                " AND agent_instance_id=$3 AND session_id=$4",
                interaction_id,
                tenant_id,
                agent_instance_id,
                session_id,
            )
        record = self._interaction_row_to_record(row) if row is not None else None
        return {**record.public_request(), "status": record.status} if record is not None else None

    async def read_resolved_response(
        self, record: InteractionRecord
    ) -> ResolvedInteractionResponse | None:
        if record.status != "resolved":
            return None
        async with self._connection() as connection:
            row = await connection.fetchrow(
                "SELECT response FROM kernel_interactions WHERE tenant_id=$1"
                " AND interaction_id=$2 AND agent_instance_id=$3 AND session_id=$4"
                " AND run_id=$5 AND kind=$6 AND revision=$7 AND status='resolved'"
                " AND response IS NOT NULL",
                record.tenant_id, record.interaction_id, record.agent_instance_id,
                record.session_id, record.run_id, record.kind, record.revision,
            )
        return ResolvedInteractionResponse(json.loads(row["response"])) if row else None

    async def get(
        self,
        interaction_id: str,
        *,
        tenant_id: str | None = None,
        agent_instance_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> InteractionRecord | None:
        """Read public ids only through a complete trusted execution scope."""

        scope = (tenant_id, agent_instance_id, session_id, run_id)
        async with self._connection() as connection:
            if any(value is not None for value in scope):
                if not all(value is not None for value in scope):
                    raise InvalidCommandError(
                        "interaction lookup requires a complete trusted scope",
                        details={"interaction_id": interaction_id},
                    )
                row = await connection.fetchrow(
                    "SELECT * FROM kernel_interactions WHERE interaction_id=$1"
                    " AND tenant_id=$2 AND agent_instance_id=$3 AND session_id=$4"
                    " AND run_id=$5",
                    interaction_id,
                    tenant_id,
                    agent_instance_id,
                    session_id,
                    run_id,
                )
                return self._interaction_row_to_record(row) if row is not None else None
            rows = await connection.fetch(
                "SELECT * FROM kernel_interactions WHERE interaction_id=$1 LIMIT 2",
                interaction_id,
            )
        if len(rows) > 1:
            raise InvalidCommandError(
                f"interaction_id {interaction_id!r} is ambiguous without trusted scope",
                details={"reason": REQUEST_CONFLICT, "interaction_id": interaction_id},
            )
        return self._interaction_row_to_record(rows[0]) if rows else None

    async def list_pending_interactions(
        self, tenant_id: str, session_id: str
    ) -> list[InteractionRecord]:
        async with self._connection() as connection:
            rows = await connection.fetch(
                "SELECT * FROM kernel_interactions WHERE tenant_id=$1 AND session_id=$2"
                " AND status IN ('pending','resolving') ORDER BY created_at",
                tenant_id,
                session_id,
            )
        return [self._interaction_row_to_record(row) for row in rows]
