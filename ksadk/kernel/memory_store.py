# -*- coding: utf-8 -*-
"""InMemory ``AgentKernelStore``（Phase 1 Task 3 Step 4）。

只用于单进程开发与 conformance 测试，不宣称跨进程 durable。
以 per-(agent, session) asyncio lock 保证 accept/claim/transition 的原子语义；
所有 mutation 与对应 ``ControlEvent/v1`` 的追加在同一个临界区内完成。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ksadk.events.session_event import SessionEventStore
from ksadk.interaction.contracts import (
    InteractionRecord,
    InteractionReceipt,
    InteractionSubmission,
    is_terminal,
)
from ksadk.interaction.ledger import (
    ALREADY_RESOLVED,
    REVISION_MISMATCH,
    REQUEST_CONFLICT,
    interaction_event,
    request_digest,
    requested_event_payload,
    resolve_outcome,
    submission_digest,
)
from ksadk.kernel.contracts import (
    ActivationLease,
    ActivationWriteGuard,
    AdmissionWriteGuard,
    AgentControlCommand,
    AgentControlReceipt,
    ControlError,
    SessionEventEnvelope,
)
from ksadk.kernel.errors import InvalidCommandError, StaleFenceError
from ksadk.kernel.state import (
    InboxState,
    assert_inbox_transition,
    assert_run_transition,
    is_active_run,
)
from ksadk.kernel.store import (
    ActivationLeaseRequest,
    InboxMessage,
    RunRecord,
    command_digest,
    control_event,
    new_message_id,
    now_iso,
)


class InMemoryAgentKernelStore:
    def __init__(self, session_event_store: SessionEventStore) -> None:
        self._events = session_event_store
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._messages: dict[str, dict[str, Any]] = {}
        self._idempotency: dict[tuple[str, str], str] = {}  # (session, key) -> message_id
        self._accepted_seq: dict[str, int] = {}
        self._activations: dict[tuple[str, str], dict[str, Any]] = {}
        self._runs: dict[str, RunRecord] = {}
        # Interaction ledger（Task 5）：(tenant_id, interaction_id) -> row。
        self._interactions: dict[tuple[str, str], dict[str, Any]] = {}
        self._interaction_submissions: dict[tuple[str, str, str], dict[str, Any]] = {}

    # ------------------------------------------------------------------ locks

    def _lock(self, agent_instance_id: str, session_id: str) -> asyncio.Lock:
        key = (agent_instance_id, session_id)
        return self._locks.setdefault(key, asyncio.Lock())

    # ---------------------------------------------------------------- helpers

    def _activation_row(
        self, agent_instance_id: str, session_id: str
    ) -> dict[str, Any] | None:
        row = self._activations.get((agent_instance_id, session_id))
        if row is None or row["released"]:
            return None
        return row

    @staticmethod
    def _lease_expired(row: dict[str, Any]) -> bool:
        return row["lease_expires_at"] <= time.time()

    def _check_fence(self, agent_instance_id: str, session_id: str, expected_fence: int) -> dict[str, Any]:
        row = self._activation_row(agent_instance_id, session_id)
        if (
            row is None
            or self._lease_expired(row)
            or row["fencing_token"] != int(expected_fence)
        ):
            raise StaleFenceError(
                "activation lease does not match expected fence",
                details={
                    "agent_instance_id": agent_instance_id,
                    "session_id": session_id,
                    "expected_fence": int(expected_fence),
                },
            )
        return row

    async def _emit(
        self,
        envelope: SessionEventEnvelope,
        *,
        activation_row: dict[str, Any] | None,
        admission_command: AgentControlCommand | None = None,
    ) -> SessionEventEnvelope:
        if activation_row is not None:
            guard = ActivationWriteGuard(
                activation_id=activation_row["activation_id"],
                fencing_token=activation_row["fencing_token"],
            )
        elif admission_command is not None:
            # admission 事实的 guard 绑定提交方 permit 引用与 command_id。
            guard = AdmissionWriteGuard(
                authorization_ref=admission_command.authorization_ref,
                command_id=admission_command.command_id,
            )
        else:
            guard = AdmissionWriteGuard(
                authorization_ref="agent-kernel", command_id=uuid4()
            )
        return await self._events.append(envelope, guard=guard)

    async def _emit_admission(
        self, envelope: SessionEventEnvelope, command: AgentControlCommand
    ) -> None:
        # admission 事实的 guard 绑定提交方 permit 引用与 command_id。
        await self._events.append(
            envelope,
            guard=AdmissionWriteGuard(
                authorization_ref=command.authorization_ref,
                command_id=command.command_id,
            ),
        )

    @staticmethod
    def _receipt(
        command: AgentControlCommand,
        status: str,
        *,
        message_id: str | None = None,
        accepted_seq: int | None = None,
        error: ControlError | None = None,
    ) -> AgentControlReceipt:
        return AgentControlReceipt(
            command_id=command.command_id,
            status=status,  # type: ignore[arg-type]
            message_id=message_id,
            accepted_seq=accepted_seq,
            error=error,
        )

    # --------------------------------------------------------------- commands

    async def accept_command(
        self, command: AgentControlCommand, *, queue_limit: int
    ) -> AgentControlReceipt:
        if queue_limit < 1:
            raise InvalidCommandError("queue_limit must be positive")
        async with self._lock(command.agent_instance_id, command.session_id):
            existing_id = self._idempotency.get(
                (command.session_id, command.idempotency_key)
            )
            if existing_id is not None:
                existing = self._messages[existing_id]
                if existing["request_digest"] != command_digest(command):
                    await self._emit_admission(
                            control_event(
                            session_id=command.session_id,
                            event_type="control.command_rejected",
                            payload={
                                "command_id": str(command.command_id),
                                "status": "rejected",
                                "reason": "idempotency_conflict",
                            },
                            causation_id=str(command.command_id),
                            ),
                            command,
                        )
                    return self._receipt(
                        command,
                        "rejected",
                        error=ControlError(
                            code="idempotency_conflict",
                            message="idempotency key reused with a different request digest",
                            retryable=False,
                        ),
                    )
                return self._receipt(
                    command,
                    "duplicate",
                    message_id=existing["message_id"],
                    accepted_seq=existing["accepted_seq"],
                )

            depth = sum(
                1
                for row in self._messages.values()
                if row["agent_instance_id"] == command.agent_instance_id
                and row["session_id"] == command.session_id
                and row["status"] == InboxState.ACCEPTED
            )
            if depth >= queue_limit:
                await self._emit_admission(
                        control_event(
                        session_id=command.session_id,
                        event_type="control.command_rejected",
                        payload={
                            "command_id": str(command.command_id),
                            "status": "queue_full",
                            "queue_limit": queue_limit,
                        },
                        causation_id=str(command.command_id),
                        ),
                        command,
                    )
                return self._receipt(
                    command,
                    "queue_full",
                    error=ControlError(
                        code="queue_full",
                        message=f"inbox reached queue_limit={queue_limit}",
                        retryable=True,
                    ),
                )

            accepted_seq = self._accepted_seq.get(command.session_id, 0) + 1
            self._accepted_seq[command.session_id] = accepted_seq
            message_id = new_message_id()
            self._messages[message_id] = {
                "message_id": message_id,
                "agent_instance_id": command.agent_instance_id,
                "session_id": command.session_id,
                "idempotency_key": command.idempotency_key,
                "request_digest": command_digest(command),
                "accepted_seq": accepted_seq,
                "status": InboxState.ACCEPTED,
                "claimed_fence": None,
                "command": command,
            }
            self._idempotency[(command.session_id, command.idempotency_key)] = message_id
            await self._emit_admission(
                    control_event(
                    session_id=command.session_id,
                    event_type="control.command_accepted",
                    payload={
                        "command_id": str(command.command_id),
                        "status": "accepted",
                        "message_id": message_id,
                        "accepted_seq": accepted_seq,
                        "command_type": command.command_type,
                    },
                    causation_id=str(command.command_id),
                    ),
                    command,
                )
            return self._receipt(
                command,
                "accepted",
                message_id=message_id,
                accepted_seq=accepted_seq,
            )

    async def load_message(self, message_id: str) -> InboxMessage | None:
        row = self._messages.get(str(message_id))
        return self._to_message(row) if row is not None else None

    async def load_by_idempotency(
        self, session_id: str, idempotency_key: str
    ) -> InboxMessage | None:
        message_id = self._idempotency.get((session_id, idempotency_key))
        if message_id is None:
            return None
        return await self.load_message(message_id)

    async def reject_command(
        self,
        command: AgentControlCommand,
        *,
        status: str,
        code: str,
        message: str,
        retryable: bool = False,
    ) -> AgentControlReceipt:
        """admission 拒绝（invalid_permit / unsupported / ...）的脱敏审计 + receipt。

        只在 SessionEventStore 里追加 ``control.command_rejected`` 事实，
        不写 Inbox 行；payload 仅含 command_id/status/reason。
        """
        await self._emit_admission(
                control_event(
                session_id=command.session_id,
                event_type="control.command_rejected",
                payload={
                    "command_id": str(command.command_id),
                    "status": status,
                    "reason": code,
                },
                causation_id=str(command.command_id),
                ),
                command,
            )
        return self._receipt(
            command,
            status,
            error=ControlError(code=code, message=message, retryable=retryable),
        )

    def _session_rows(
        self, agent_instance_id: str, session_id: str | None
    ) -> list[dict[str, Any]]:
        return [
            row
            for row in self._messages.values()
            if row["agent_instance_id"] == agent_instance_id
            and (session_id is None or row["session_id"] == session_id)
        ]

    async def list_messages(
        self, agent_instance_id: str, session_id: str | None = None
    ) -> list[InboxMessage]:
        """全部状态的 Inbox 行（审计/测试视角），按 accepted_seq 排序。"""
        rows = sorted(
            self._session_rows(agent_instance_id, session_id),
            key=lambda row: row["accepted_seq"],
        )
        return [self._to_message(row) for row in rows]

    async def list_pending(
        self,
        agent_instance_id: str,
        session_id: str | None = None,
        *,
        fencing_token: int | None = None,
    ) -> list[InboxMessage]:
        """按 accepted_seq 排序的待处理消息。

        ACCEPTED 总是 pending；CLAIMED 只在传入相同 fencing_token（本 owner
        自我重试视角）时可见，用于 retryable failure 后的恢复。
        """
        rows = []
        for row in self._session_rows(agent_instance_id, session_id):
            if row["status"] == InboxState.ACCEPTED:
                rows.append(row)
            elif (
                fencing_token is not None
                and row["status"] == InboxState.CLAIMED
                and row["claimed_fence"] == int(fencing_token)
            ):
                rows.append(row)
        rows.sort(key=lambda row: row["accepted_seq"])
        return [self._to_message(row) for row in rows]

    async def claim_message(
        self, message_id: str, fencing_token: int
    ) -> InboxMessage:
        """按 message_id 认领（worker 选择性 FIFO 使用）。同 fence 重复认领幂等。"""
        row = self._messages.get(str(message_id))
        if row is None:
            raise InvalidCommandError(f"unknown message_id {message_id!r}")
        async with self._lock(row["agent_instance_id"], row["session_id"]):
            fresh = self._messages[str(message_id)]
            if (
                fresh["status"] == InboxState.CLAIMED
                and fresh["claimed_fence"] == int(fencing_token)
            ):
                return self._to_message(fresh)
            activation = self._check_fence(
                fresh["agent_instance_id"], fresh["session_id"], fencing_token
            )
            if fresh["status"] != InboxState.ACCEPTED:
                raise InvalidCommandError(
                    f"message {message_id!r} is not claimable at status {fresh['status']}"
                )
            assert_inbox_transition(InboxState(fresh["status"]), InboxState.CLAIMED)
            fresh["status"] = InboxState.CLAIMED
            fresh["claimed_fence"] = int(fencing_token)
            await self._emit(
                control_event(
                    session_id=fresh["session_id"],
                    event_type="control.message_claimed",
                    payload={
                        "message_id": fresh["message_id"],
                        "fencing_token": int(fencing_token),
                    },
                ),
                activation_row=activation,
            )
            return self._to_message(fresh)

    async def discard_claim(self, message_id: str, *, expected_fence: int) -> None:
        """typed rejection 的确定性收口：CLAIMED -> DISCARDED。"""
        message_id = str(message_id)
        row = self._messages.get(message_id)
        if row is None:
            raise InvalidCommandError(f"unknown message_id {message_id!r}")
        async with self._lock(row["agent_instance_id"], row["session_id"]):
            fresh = self._messages[message_id]
            activation = self._check_fence(
                fresh["agent_instance_id"], fresh["session_id"], expected_fence
            )
            if (
                fresh["status"] != InboxState.CLAIMED
                or fresh["claimed_fence"] != int(expected_fence)
            ):
                raise StaleFenceError(
                    f"message {message_id!r} is not claimed at fence {expected_fence}"
                )
            assert_inbox_transition(InboxState(fresh["status"]), InboxState.DISCARDED)
            fresh["status"] = InboxState.DISCARDED
            await self._emit(
                control_event(
                    session_id=fresh["session_id"],
                    event_type="control.message_discarded",
                    payload={
                        "message_id": message_id,
                        "fencing_token": int(expected_fence),
                    },
                ),
                activation_row=activation,
            )

    async def inbox_depth(
        self, agent_instance_id: str, session_id: str | None = None
    ) -> int:
        return sum(
            1
            for row in self._session_rows(agent_instance_id, session_id)
            if row["status"] == InboxState.ACCEPTED
        )

    async def find_active_run(
        self, agent_instance_id: str, session_id: str | None = None
    ) -> RunRecord | None:
        for run in self._runs.values():
            if run.agent_instance_id != agent_instance_id:
                continue
            if session_id is not None and run.session_id != session_id:
                continue
            if is_active_run(run.state):
                return run
        return None

    async def current_lease(
        self, agent_instance_id: str, session_id: str | None = None
    ) -> ActivationLease | None:
        for (agent, session), row in self._activations.items():
            if agent != agent_instance_id:
                continue
            if session_id is not None and session != session_id:
                continue
            if row.get("released") or self._lease_expired(row):
                continue
            request = ActivationLeaseRequest(
                agent_instance_id=agent,
                session_id=session,
                activation_id=row["activation_id"],
                runtime_type=row["runtime_type"],
                bundle_digest=row["bundle_digest"],
                capability_digest=row["capability_digest"],
            )
            return self._lease(request, row)
        return None

    @staticmethod
    def _to_message(row: dict[str, Any]) -> InboxMessage:
        return InboxMessage(
            message_id=row["message_id"],
            agent_instance_id=row["agent_instance_id"],
            session_id=row["session_id"],
            idempotency_key=row["idempotency_key"],
            request_digest=row["request_digest"],
            accepted_seq=row["accepted_seq"],
            status=InboxState(row["status"]),
            claimed_fence=row["claimed_fence"],
            command=row.get("command"),
        )

    async def claim_next(
        self, agent_instance_id: str, session_id: str, fencing_token: int
    ) -> InboxMessage | None:
        async with self._lock(agent_instance_id, session_id):
            activation = self._check_fence(agent_instance_id, session_id, fencing_token)
            def _claimable(row: dict[str, Any]) -> bool:
                if row["status"] == InboxState.ACCEPTED:
                    return True
                # 过期/被 takeover 的 claim 只能被更高 fence 的 owner reclaim。
                return row["status"] == InboxState.CLAIMED and row[
                    "claimed_fence"
                ] != int(fencing_token)

            candidates = sorted(
                (
                    row
                    for row in self._messages.values()
                    if row["agent_instance_id"] == agent_instance_id
                    and row["session_id"] == session_id
                    and _claimable(row)
                ),
                key=lambda row: row["accepted_seq"],
            )
            if not candidates:
                return None
            row = candidates[0]
            if row["status"] == InboxState.ACCEPTED:
                assert_inbox_transition(InboxState(row["status"]), InboxState.CLAIMED)
            row["status"] = InboxState.CLAIMED
            row["claimed_fence"] = int(fencing_token)
            await self._emit(
                control_event(
                    session_id=session_id,
                    event_type="control.message_claimed",
                    payload={
                        "message_id": row["message_id"],
                        "fencing_token": int(fencing_token),
                    },
                ),
                activation_row=activation,
            )
            return self._to_message(row)

    async def complete_claim(self, message_id: str, *, expected_fence: int) -> None:
        message_id = str(message_id)
        row = self._messages.get(message_id)
        if row is None:
            raise InvalidCommandError(f"unknown message_id {message_id!r}")
        async with self._lock(row["agent_instance_id"], row["session_id"]):
            fresh = self._messages[message_id]
            activation = self._check_fence(
                fresh["agent_instance_id"], fresh["session_id"], expected_fence
            )
            if (
                fresh["status"] != InboxState.CLAIMED
                or fresh["claimed_fence"] != int(expected_fence)
            ):
                raise StaleFenceError(
                    f"message {message_id!r} is not claimed at fence {expected_fence}"
                )
            assert_inbox_transition(InboxState(fresh["status"]), InboxState.COMPLETED)
            fresh["status"] = InboxState.COMPLETED
            await self._emit(
                control_event(
                    session_id=fresh["session_id"],
                    event_type="control.message_completed",
                    payload={"message_id": message_id, "fencing_token": int(expected_fence)},
                ),
                activation_row=activation,
            )

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

    def _interaction_scope_for_guard(
        self, guard: ActivationWriteGuard
    ) -> tuple[str, str]:
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
            self._check_interaction_guard(
                record.agent_instance_id, record.session_id, guard
            )
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
            await self._events.append(
                requested_event_payload(record, now_iso()), guard=guard
            )
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
            raise InvalidCommandError(
                f"unknown interaction_id {submission.interaction_id!r}"
            )
        record = row["record"]
        async with self._lock(record.agent_instance_id, record.session_id):
            self._check_interaction_guard(
                record.agent_instance_id, record.session_id, guard
            )
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
                ):
                    return existing_sub["receipt"]
                raise self._terminal_conflict(
                    current.interaction_id, current.status
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
                    response=submission.response,
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
            self._check_interaction_guard(
                record.agent_instance_id, record.session_id, guard
            )
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
            event_type = (
                "interaction.cancelled" if status == "cancelled" else "interaction.expired"
            )
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
            and row["record"].status == "pending"
        ]

    # ------------------------------------------------------------- activations

    async def acquire_activation(self, request: ActivationLeaseRequest) -> ActivationLease:
        key = (request.agent_instance_id, request.session_id)
        async with self._lock(*key):
            row = self._activations.get(key)
            expires_at = time.time() + request.lease_ttl_seconds
            if row is None:
                token = 1
            elif row["released"] or self._lease_expired(row):
                token = row["fencing_token"] + 1
            elif row["activation_id"] == request.activation_id:
                token = row["fencing_token"]
            else:
                raise InvalidCommandError(
                    "activation lease is still held by another owner",
                    details={
                        "holder": row["activation_id"],
                        "lease_expires_at": row["lease_expires_at_iso"],
                    },
                )
            new_row = {
                "agent_instance_id": request.agent_instance_id,
                "session_id": request.session_id,
                "activation_id": request.activation_id,
                "fencing_token": token,
                "lease_expires_at": expires_at,
                "lease_expires_at_iso": datetime.fromtimestamp(
                    expires_at, tz=timezone.utc
                ).isoformat(),
                "released": False,
                "runtime_type": request.runtime_type,
                "bundle_digest": request.bundle_digest,
                "capability_digest": request.capability_digest,
            }
            self._activations[key] = new_row
            return self._lease(request, new_row)

    @staticmethod
    def _lease(request: ActivationLeaseRequest, row: dict[str, Any]) -> ActivationLease:
        return ActivationLease(
            agent_instance_id=request.agent_instance_id,
            activation_id=row["activation_id"],
            fencing_token=row["fencing_token"],
            lease_expires_at=row["lease_expires_at_iso"],
            bundle_digest=row["bundle_digest"],
            runtime_type=row["runtime_type"],
            capability_digest=row["capability_digest"],
        )

    def _find_activation(self, activation_id: str) -> dict[str, Any]:
        for row in self._activations.values():
            if row["activation_id"] == activation_id:
                return row
        raise InvalidCommandError(f"unknown activation_id {activation_id!r}")

    async def renew_activation(
        self, activation_id: str, *, expected_fence: int, lease_ttl_seconds: float
    ) -> ActivationLease:
        row = self._find_activation(activation_id)
        key = self._activation_key(row)
        async with self._lock(*key):
            fresh = self._find_activation(activation_id)
            if (
                fresh["released"]
                or self._lease_expired(fresh)
                or fresh["fencing_token"] != int(expected_fence)
            ):
                raise StaleFenceError(
                    f"cannot renew activation {activation_id!r} at fence {expected_fence}"
                )
            fresh["lease_expires_at"] = time.time() + lease_ttl_seconds
            fresh["lease_expires_at_iso"] = now_iso()
            request = ActivationLeaseRequest(
                agent_instance_id=key[0],
                session_id=key[1],
                activation_id=fresh["activation_id"],
                runtime_type=fresh["runtime_type"],
                bundle_digest=fresh["bundle_digest"],
                capability_digest=fresh["capability_digest"],
            )
            return self._lease(request, fresh)

    async def release_activation(self, activation_id: str, *, expected_fence: int) -> None:
        row = self._find_activation(activation_id)
        key = self._activation_key(row)
        async with self._lock(*key):
            fresh = self._find_activation(activation_id)
            if (
                fresh["released"]
                or fresh["fencing_token"] != int(expected_fence)
            ):
                raise StaleFenceError(
                    f"cannot release activation {activation_id!r} at fence {expected_fence}"
                )
            fresh["released"] = True
            fresh["lease_expires_at"] = time.time()

    @staticmethod
    def _activation_key(row: dict[str, Any]) -> tuple[str, str]:
        return (row["agent_instance_id"], row["session_id"])

    # ------------------------------------------------------------------ events

    async def validate_write_fence(
        self,
        envelope: SessionEventEnvelope,
        guard: ActivationWriteGuard,
    ) -> None:
        """SessionEventStore fence seam：guard 必须是当前未过期 lease 的 owner。

        被 takeover（activation 行被替换/释放）或 token 滞后的旧 owner 得到
        :class:`StaleFenceError`；不做任何写入。
        """

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
                "activation write guard does not match the current lease",
                details={
                    "activation_id": guard.activation_id,
                    "fencing_token": int(guard.fencing_token),
                    "session_id": envelope.session_id,
                },
            )

    async def append_event(
        self,
        envelope: SessionEventEnvelope,
        *,
        expected_fence: int,
        agent_instance_id: str | None = None,
    ) -> SessionEventEnvelope:
        row = self._resolve_activation(envelope.session_id, agent_instance_id)
        async with self._lock(row["agent_instance_id"], envelope.session_id):
            fresh = self._resolve_activation(envelope.session_id, agent_instance_id)
            activation = self._check_fence(
                fresh["agent_instance_id"], envelope.session_id, expected_fence
            )
            return await self._events.append(
                envelope, guard=ActivationWriteGuard(
                    activation_id=activation["activation_id"],
                    fencing_token=int(expected_fence),
                )
            )

    def _resolve_activation(
        self, session_id: str, agent_instance_id: str | None
    ) -> dict[str, Any]:
        if agent_instance_id is not None:
            row = self._activation_row(agent_instance_id, session_id)
            if row is None:
                raise StaleFenceError(
                    "no active activation lease",
                    details={"agent_instance_id": agent_instance_id, "session_id": session_id},
                )
            return row
        matches = [
            row
            for (agent, session), row in self._activations.items()
            if session == session_id and row.get("released") is not True
        ]
        if len(matches) != 1:
            raise StaleFenceError(
                "cannot resolve a single activation lease for session",
                details={"session_id": session_id, "matches": len(matches)},
            )
        return matches[0]

    # -------------------------------------------------------------------- runs

    async def load_run(self, run_id: str) -> RunRecord | None:
        return self._runs.get(run_id)

    async def save_run_transition(
        self, run: RunRecord, *, expected_fence: int
    ) -> RunRecord:
        async with self._lock(run.agent_instance_id, run.session_id):
            activation = self._check_fence(
                run.agent_instance_id, run.session_id, expected_fence
            )
            existing = self._runs.get(run.run_id)
            assert_run_transition(existing.state if existing else None, run.state)
            if is_active_run(run.state):
                for other in self._runs.values():
                    if (
                        other.run_id != run.run_id
                        and other.session_id == run.session_id
                        and is_active_run(other.state)
                    ):
                        raise InvalidCommandError(
                            "session already has an active run",
                            details={
                                "session_id": run.session_id,
                                "active_run_id": other.run_id,
                            },
                        )
            stored = run.model_copy(
                update={
                    "activation_fence": int(expected_fence),
                    "created_at": existing.created_at if existing else now_iso(),
                    "updated_at": now_iso(),
                }
            )
            self._runs[run.run_id] = stored
            await self._emit(
                control_event(
                    session_id=run.session_id,
                    event_type="control.run_transition",
                    payload={
                        "run_id": run.run_id,
                        "state": run.state.value,
                        "fencing_token": int(expected_fence),
                    },
                    run_id=run.run_id,
                    causation_id=str(run.metadata.get("command_id") or "") or None,
                ),
                activation_row=activation,
            )
            return stored


__all__ = ["InMemoryAgentKernelStore"]
