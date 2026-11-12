"""Memory Interaction/v1 transactions, extracted without behavior changes."""

from __future__ import annotations

from copy import deepcopy
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
from ksadk.interaction.submission import (
    check_reserved_submission,
    needs_recovery_check,
    normalized_submission,
    plan_release,
    plan_reservation,
)
from ksadk.kernel.contracts import (
    ActivationWriteGuard,
)
from ksadk.kernel.errors import InvalidCommandError, StaleFenceError
from ksadk.kernel.store import (
    now_iso,
)


class MemoryInteractionStoreMixin:
    async def reserve_submission(self, submission, *, guard, command_id=""):
        agent, session = self._interaction_scope_for_guard(guard)
        async with self._lock(agent, session):
            self._check_interaction_guard(agent, session, guard)
            row = self._find_interaction(
                submission.interaction_id, agent_instance_id=agent, session_id=session
            )
            if row is None:
                raise InvalidCommandError("Unknown interaction")
            current = row["record"]
            key = (current.tenant_id, current.interaction_id, submission.idempotency_key)
            prior = self._interaction_submissions.get(key)
            run = self._runs.get(current.run_id) if needs_recovery_check(current, guard.fencing_token) else None
            updated, reservation = plan_reservation(
                current,
                submission,
                existing=(prior["digest"], prior["receipt"]) if prior else None,
                command_id=command_id,
                fence=guard.fencing_token,
                recovery_run={
                    "run_id": run.run_id, "agent_instance_id": run.agent_instance_id,
                    "session_id": run.session_id, "state": run.state.value,
                    "activation_fence": run.activation_fence, "metadata": run.metadata,
                } if run is not None else None,
            )
            if reservation.acquired:
                row["record"] = updated
                self._interaction_submissions[key] = {
                    "digest": submission_digest(submission),
                    "receipt": reservation.receipt,
                }
            return reservation

    async def release_submission(self, submission, *, guard):
        agent, session = self._interaction_scope_for_guard(guard)
        async with self._lock(agent, session):
            self._check_interaction_guard(agent, session, guard)
            row = self._find_interaction(
                submission.interaction_id, agent_instance_id=agent, session_id=session
            )
            if row is None:
                raise InvalidCommandError("Unknown interaction")
            current = row["record"]
            updated, receipt = plan_release(current, submission)
            row["record"] = updated
            self._interaction_submissions[
                (current.tenant_id, current.interaction_id, submission.idempotency_key)
            ] = {"digest": submission_digest(submission), "receipt": receipt}
            return receipt

    # -------------------------------------------------------------- interactions

    def _check_interaction_guard(
        self, agent_instance_id: str, session_id: str, guard: ActivationWriteGuard
    ) -> dict[str, Any]:
        """Interaction 台账的 fence CAS：guard 必须命中当前未过期 lease。"""

        row = next(
            (
                candidate
                for candidate in self._activations.values()
                if candidate["activation_id"] == guard.activation_id
                and candidate["agent_instance_id"] == agent_instance_id
                and candidate["session_id"] == session_id
            ),
            None,
        )
        if (
            row is None
            or row.get("released")
            or self._lease_expired(row)
            or row["fencing_token"] != int(guard.fencing_token)
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
        return row

    def _find_interaction(
        self,
        interaction_id: str,
        *,
        agent_instance_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Resolve an opaque public id only inside an activation-owned scope.

        ``kernel_interactions`` is tenant-keyed, while a public submission
        intentionally does not carry a tenant.  The trusted activation guard
        is consequently the lookup boundary for mutations.  An unscoped read
        is permitted only when the id is globally unambiguous; it must never
        return an arbitrary tenant's row.
        """

        matches = [
            row
            for (_, key), row in self._interactions.items()
            if key == interaction_id
            and (agent_instance_id is None or row["record"].agent_instance_id == agent_instance_id)
            and (session_id is None or row["record"].session_id == session_id)
        ]
        if len(matches) > 1:
            raise InvalidCommandError(
                f"interaction_id {interaction_id!r} is ambiguous without trusted scope",
                details={"reason": REQUEST_CONFLICT, "interaction_id": interaction_id},
            )
        return matches[0] if matches else None

    def _interaction_scope_for_guard(self, guard: ActivationWriteGuard) -> tuple[str, str]:
        row = next(
            (
                candidate
                for candidate in self._activations.values()
                if candidate["activation_id"] == guard.activation_id
            ),
            None,
        )
        if (
            row is None
            or row.get("released")
            or self._lease_expired(row)
            or row["fencing_token"] != int(guard.fencing_token)
        ):
            raise StaleFenceError(
                "interaction write guard does not match the current lease",
                details={
                    "activation_id": guard.activation_id,
                    "fencing_token": int(guard.fencing_token),
                },
            )
        return str(row["agent_instance_id"]), str(row["session_id"])

    @staticmethod
    def _terminal_conflict(interaction_id: str, status: str) -> InvalidCommandError:
        return InvalidCommandError(
            f"interaction {interaction_id!r} already reached terminal status {status!r}",
            details={"reason": ALREADY_RESOLVED, "interaction_id": interaction_id},
        )

    async def request(
        self, record: InteractionRecord, *, guard: ActivationWriteGuard
    ) -> InteractionRecord:
        async with self._lock(record.agent_instance_id, record.session_id):
            self._check_interaction_guard(record.agent_instance_id, record.session_id, guard)
            key = (record.tenant_id, record.interaction_id)
            existing = self._interactions.get(key)
            digest = request_digest(record)
            if existing is not None:
                if existing["request_digest"] != digest:
                    raise InvalidCommandError(
                        "interaction_id reused with a different request digest",
                        details={
                            "reason": REQUEST_CONFLICT,
                            "interaction_id": record.interaction_id,
                        },
                    )
                return existing["record"]
            # persist-before-ack：事件追加失败时不留下 pending 行。
            await self._events.append(requested_event_payload(record, now_iso()), guard=guard)
            self._interactions[key] = {"record": record, "request_digest": digest}
            return record

    async def resolve(
        self, submission: InteractionSubmission, *, guard: ActivationWriteGuard
    ) -> InteractionReceipt:
        agent_instance_id, session_id = self._interaction_scope_for_guard(guard)
        row = self._find_interaction(
            submission.interaction_id,
            agent_instance_id=agent_instance_id,
            session_id=session_id,
        )
        if row is None:
            raise InvalidCommandError(f"unknown interaction_id {submission.interaction_id!r}")
        record = row["record"]
        async with self._lock(record.agent_instance_id, record.session_id):
            self._check_interaction_guard(record.agent_instance_id, record.session_id, guard)
            fresh = self._interactions[(record.tenant_id, record.interaction_id)]
            current: InteractionRecord = fresh["record"]
            if is_terminal(current.status):
                sub_key = (
                    current.tenant_id,
                    current.interaction_id,
                    submission.idempotency_key,
                )
                existing_sub = self._interaction_submissions.get(sub_key)
                if (
                    existing_sub is not None
                    and existing_sub["digest"] == submission_digest(submission)
                    and existing_sub["receipt"].status == current.status
                ):
                    return existing_sub["receipt"]
                raise self._terminal_conflict(current.interaction_id, current.status)
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
            response = deepcopy(normalized_submission(current, submission).response)
            outcome = resolve_outcome(submission.action)
            updated = current.model_copy(
                update={
                    "status": "resolved",
                    "revision": current.revision + 1,
                }
            )
            stored = await self._events.append(
                interaction_event(
                    updated,
                    event_type="interaction.resolved",
                    timestamp=now_iso(),
                    outcome=outcome,
                    response=response,
                    actor_ref="user",
                ),
                guard=guard,
            )
            receipt = InteractionReceipt(
                interaction_id=updated.interaction_id,
                revision=updated.revision,
                status="resolved",
                outcome=outcome,  # type: ignore[arg-type]
                event_id=stored.event_id,
                accepted_seq=stored.seq,
            )
            fresh["record"] = updated
            fresh["response"] = deepcopy(response)
            self._interaction_submissions[
                (updated.tenant_id, updated.interaction_id, submission.idempotency_key)
            ] = {"digest": submission_digest(submission), "receipt": receipt}
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
        agent_instance_id, session_id = self._interaction_scope_for_guard(guard)
        row = self._find_interaction(
            interaction_id,
            agent_instance_id=agent_instance_id,
            session_id=session_id,
        )
        if row is None:
            raise InvalidCommandError(f"unknown interaction_id {interaction_id!r}")
        record = row["record"]
        async with self._lock(record.agent_instance_id, record.session_id):
            self._check_interaction_guard(record.agent_instance_id, record.session_id, guard)
            fresh = self._interactions[(record.tenant_id, record.interaction_id)]
            current: InteractionRecord = fresh["record"]
            if is_terminal(current.status):
                raise self._terminal_conflict(current.interaction_id, current.status)
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
            event_type = "interaction.cancelled" if status == "cancelled" else "interaction.expired"
            stored = await self._events.append(
                interaction_event(
                    updated,
                    event_type=event_type,
                    timestamp=now_iso(),
                    reason=reason,
                ),
                guard=guard,
            )
            receipt = InteractionReceipt(
                interaction_id=updated.interaction_id,
                revision=updated.revision,
                status=updated.status,  # type: ignore[arg-type]
                outcome=updated.status,  # type: ignore[arg-type]
                event_id=stored.event_id,
                accepted_seq=stored.seq,
            )
            fresh["record"] = updated
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
        row = self._interactions.get((tenant_id, interaction_id))
        record = row["record"] if row is not None else None
        if (
            record is None
            or record.agent_instance_id != agent_instance_id
            or record.session_id != session_id
        ):
            return None
        return {**record.public_request(), "status": record.status}

    async def read_resolved_response(
        self, record: InteractionRecord
    ) -> ResolvedInteractionResponse | None:
        row = self._interactions.get((record.tenant_id, record.interaction_id))
        if record.status != "resolved" or row is None or "response" not in row:
            return None
        current = row["record"]
        if current.status != "resolved" or any(
            getattr(current, key) != getattr(record, key)
            for key in (
                "tenant_id", "interaction_id", "agent_instance_id", "session_id",
                "run_id", "kind", "revision",
            )
        ):
            return None
        return ResolvedInteractionResponse(deepcopy(row["response"]))

    async def get(
        self,
        interaction_id: str,
        *,
        tenant_id: str | None = None,
        agent_instance_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> InteractionRecord | None:
        """Read an opaque id only when it is unique or fully trusted-scoped.

        Public interaction ids are not tenant grants.  The worker always has
        the Server-admitted command scope and must pass all four dimensions;
        legacy local callers may omit all dimensions only while the id is
        globally unambiguous.
        """

        scope = (tenant_id, agent_instance_id, session_id, run_id)
        if any(value is not None for value in scope):
            if not all(value is not None for value in scope):
                raise InvalidCommandError(
                    "interaction lookup requires a complete trusted scope",
                    details={"interaction_id": interaction_id},
                )
            matches = [
                row
                for (_, key), row in self._interactions.items()
                if key == interaction_id
                and row["record"].tenant_id == tenant_id
                and row["record"].agent_instance_id == agent_instance_id
                and row["record"].session_id == session_id
                and row["record"].run_id == run_id
            ]
            if len(matches) > 1:  # pragma: no cover - backend key prevents it
                raise InvalidCommandError(
                    f"interaction_id {interaction_id!r} is ambiguous in trusted scope",
                    details={"reason": REQUEST_CONFLICT, "interaction_id": interaction_id},
                )
            return matches[0]["record"] if matches else None
        row = self._find_interaction(interaction_id)
        return row["record"] if row is not None else None

    async def list_pending_interactions(
        self, tenant_id: str, session_id: str
    ) -> list[InteractionRecord]:
        return [
            row["record"]
            for (tenant, _), row in sorted(self._interactions.items())
            if tenant == tenant_id
            and row["record"].session_id == session_id
            and row["record"].status in {"pending", "resolving"}
        ]
