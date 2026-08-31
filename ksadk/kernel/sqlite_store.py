# -*- coding: utf-8 -*-
"""SQLite ``AgentKernelStore``（Phase 1 Task 3 Step 5）。

单文件 durable Inbox / Run / ActivationLease 存储：
- WAL journal + 每次 mutation ``BEGIN IMMEDIATE`` 做跨进程 CAS；
- schema migration 用 ``PRAGMA user_version`` 整数版本，重复启动幂等；
- 所有 fence 比较都发生在同一个写事务内，不匹配抛 :class:`StaleFenceError`；
- ControlEvent/v1 经注入的 SessionEventStore 追加；accepted 事件在 kernel
  事务 commit 之前追加（persist-before-ack，见 :meth:`accept_command`），
  事件写入失败时回滚 Inbox。

只面向单机本地部署（local dev / serverless pod 单写者场景）；预发多写者
场景由 Task 4 的 PostgreSQL 适配器承接。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from uuid import uuid4

import aiosqlite

from ksadk.events.session_event import (
    SessionEventStore,
    SessionServiceEventStore,
    envelope_to_session_event,
    session_event_storage_id,
    session_event_to_envelope,
    validate_write_guard,
)
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
from ksadk.sessions._local_tables import KSADK_EVENTS_TABLE, KSADK_SESSIONS_TABLE
from ksadk.sessions.base import SessionEvent
from ksadk.sessions.local_service import LocalSessionService

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kernel_inbox (
  message_id TEXT PRIMARY KEY,
  agent_instance_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  accepted_seq INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('accepted','claimed','completed','discarded')),
  claimed_fence INTEGER,
  payload_json TEXT NOT NULL,
  UNIQUE(session_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_kernel_inbox_claim
  ON kernel_inbox (agent_instance_id, session_id, status, accepted_seq);
CREATE INDEX IF NOT EXISTS idx_kernel_inbox_idempotency
  ON kernel_inbox (session_id, idempotency_key);

CREATE TABLE IF NOT EXISTS kernel_runs (
  run_id TEXT PRIMARY KEY,
  agent_instance_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN (
    'pending','running','paused','waiting','completed','failed','cancelled','interrupted'
  )),
  activation_fence INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_kernel_runs_session_state
  ON kernel_runs (session_id, state);

CREATE TABLE IF NOT EXISTS kernel_activations (
  agent_instance_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  activation_id TEXT NOT NULL,
  fencing_token INTEGER NOT NULL,
  lease_expires_at REAL NOT NULL,
  lease_expires_at_iso TEXT NOT NULL,
  released INTEGER NOT NULL DEFAULT 0,
  runtime_type TEXT NOT NULL DEFAULT 'ksadk',
  bundle_digest TEXT NOT NULL DEFAULT '',
  capability_digest TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (agent_instance_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_kernel_activations_expiry
  ON kernel_activations (lease_expires_at);

CREATE TABLE IF NOT EXISTS kernel_accepted_seq (
  session_id TEXT PRIMARY KEY,
  last_seq INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kernel_interactions (
  interaction_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  agent_instance_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('approval','structured_input','plan_review','custom')),
  request_schema_json TEXT NOT NULL,
  presentation_json TEXT,
  revision INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN (
    'pending','resolving','resolved','cancelled','expired'
  )),
  created_at TEXT NOT NULL,
  expires_at TEXT,
  provider_id TEXT NOT NULL DEFAULT '',
  native_target_json TEXT,
  continuation_json TEXT,
  request_digest TEXT NOT NULL,
  response_json TEXT,
  outcome TEXT,
  actor TEXT,
  event_id TEXT,
  accepted_seq INTEGER,
  fencing_token INTEGER,
  updated_at TEXT,
  PRIMARY KEY (tenant_id, interaction_id)
);
CREATE INDEX IF NOT EXISTS idx_kernel_interactions_pending
  ON kernel_interactions (tenant_id, session_id, status);

CREATE TABLE IF NOT EXISTS kernel_interaction_submissions (
  tenant_id TEXT NOT NULL,
  interaction_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  submission_digest TEXT NOT NULL,
  receipt_json TEXT NOT NULL,
  PRIMARY KEY (tenant_id, interaction_id, idempotency_key)
);
"""


class SQLiteAgentKernelStore:
    def __init__(
        self,
        db_path: str | Path,
        session_event_store: SessionEventStore,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._events = session_event_store
        self._write_lock = asyncio.Lock()
        self._connection: aiosqlite.Connection | None = None
        self._ready: asyncio.Future[None] | None = None

    # ------------------------------------------------------------- lifecycle

    async def _connect(self) -> aiosqlite.Connection:
        if self._connection is None:
            self._connection = await aiosqlite.connect(str(self.db_path))
            self._connection.row_factory = aiosqlite.Row
            await self._connection.execute("PRAGMA journal_mode=WAL")
            await self._connection.execute("PRAGMA synchronous=FULL")
        return self._connection

    async def ensure_schema(self) -> None:
        connection = await self._connect()
        async with self._write_lock:
            # CREATE ... IF NOT EXISTS + 整数 user_version，重复启动幂等。
            await connection.executescript(_SCHEMA)
            await connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            await connection.commit()

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    # ---------------------------------------------------------------- helpers

    async def _begin(self) -> aiosqlite.Connection:
        connection = await self._connect()
        await connection.execute("BEGIN IMMEDIATE")
        return connection

    @staticmethod
    async def _fetchone(connection: aiosqlite.Connection, sql: str, params: tuple) -> Any:
        cursor = await connection.execute(sql, params)
        row = await cursor.fetchone()
        await cursor.close()
        return row

    @staticmethod
    def _activation_row(row: aiosqlite.Row | None) -> dict[str, Any] | None:
        if row is None or row["released"]:
            return None
        return dict(row)

    async def _check_fence(
        self, connection: aiosqlite.Connection, agent_instance_id: str, session_id: str,
        expected_fence: int,
    ) -> dict[str, Any]:
        row = await self._fetchone(
            connection,
            "SELECT * FROM kernel_activations WHERE agent_instance_id=? AND session_id=?",
            (agent_instance_id, session_id),
        )
        activation = self._activation_row(row)
        if (
            activation is None
            or activation["lease_expires_at"] <= time.time()
            or activation["fencing_token"] != int(expected_fence)
        ):
            raise StaleFenceError(
                "activation lease does not match expected fence",
                details={
                    "agent_instance_id": agent_instance_id,
                    "session_id": session_id,
                    "expected_fence": int(expected_fence),
                },
            )
        return activation

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

    async def _emit_activation(
        self, envelope: SessionEventEnvelope, activation: dict[str, Any], fence: int
    ) -> SessionEventEnvelope:
        return await self._events.append(
            envelope,
            guard=ActivationWriteGuard(
                activation_id=activation["activation_id"], fencing_token=int(fence)
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
        async with self._write_lock:
            connection = await self._begin()
            try:
                existing = await self._fetchone(
                    connection,
                    "SELECT * FROM kernel_inbox WHERE session_id=? AND idempotency_key=?",
                    (command.session_id, command.idempotency_key),
                )
                if existing is not None:
                    if existing["request_digest"] != command_digest(command):
                        await connection.commit()
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
                                message=(
                                    "idempotency key reused with a different request digest"
                                ),
                                retryable=False,
                            ),
                        )
                    await connection.commit()
                    return self._receipt(
                        command,
                        "duplicate",
                        message_id=existing["message_id"],
                        accepted_seq=existing["accepted_seq"],
                    )

                depth_row = await self._fetchone(
                    connection,
                    "SELECT COUNT(*) AS depth FROM kernel_inbox "
                    "WHERE agent_instance_id=? AND session_id=? AND status='accepted'",
                    (command.agent_instance_id, command.session_id),
                )
                if depth_row["depth"] >= queue_limit:
                    await connection.commit()
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

                seq_row = await self._fetchone(
                    connection,
                    "SELECT last_seq FROM kernel_accepted_seq WHERE session_id=?",
                    (command.session_id,),
                )
                accepted_seq = (seq_row["last_seq"] if seq_row else 0) + 1
                message_id = new_message_id()
                await connection.execute(
                    "INSERT INTO kernel_accepted_seq (session_id, last_seq) VALUES (?, ?) "
                    "ON CONFLICT(session_id) DO UPDATE SET last_seq=excluded.last_seq",
                    (command.session_id, accepted_seq),
                )
                await connection.execute(
                    "INSERT INTO kernel_inbox (message_id, agent_instance_id, session_id,"
                    " idempotency_key, request_digest, accepted_seq, status, claimed_fence,"
                    " payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        message_id,
                        command.agent_instance_id,
                        command.session_id,
                        command.idempotency_key,
                        command_digest(command),
                        accepted_seq,
                        InboxState.ACCEPTED.value,
                        None,
                        command.model_dump_json(),
                    ),
                )
                # persist-before-ack：session 事件库与 kernel 库是两个独立
                # SQLite 文件，无法共享一个事务。诚实取舍是在 kernel 事务
                # commit 之前追加 accepted 事件：事件写入失败 -> 回滚 Inbox，
                # 不产生 "persisted-but-untracked" 半状态，客户端可安全重试。
                # 残余窗口：事件已追加但 kernel commit 崩溃 -> 出现一条孤儿
                # accepted 事件而无 Inbox 行；该窗口不返回 ack，重试会重新
                # 走完整路径（seq 单调，可能产生一条重复 accepted 事件），
                # 不存在已 ack 但未持久化的状态。
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
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return self._receipt(
            command, "accepted", message_id=message_id, accepted_seq=accepted_seq
        )

    async def load_message(self, message_id: str) -> InboxMessage | None:
        connection = await self._connect()
        row = await self._fetchone(
            connection, "SELECT * FROM kernel_inbox WHERE message_id=?", (str(message_id),)
        )
        if row is None:
            return None
        return InboxMessage(
            message_id=row["message_id"],
            agent_instance_id=row["agent_instance_id"],
            session_id=row["session_id"],
            idempotency_key=row["idempotency_key"],
            request_digest=row["request_digest"],
            accepted_seq=row["accepted_seq"],
            status=InboxState(row["status"]),
            claimed_fence=row["claimed_fence"],
            command=AgentControlCommand.model_validate_json(row["payload_json"]),
        )

    async def claim_next(
        self, agent_instance_id: str, session_id: str, fencing_token: int
    ) -> InboxMessage | None:
        async with self._write_lock:
            connection = await self._begin()
            try:
                activation = self._activation_row(await self._fetchone(
                    connection,
                    "SELECT * FROM kernel_activations WHERE agent_instance_id=? AND session_id=?",
                    (agent_instance_id, session_id),
                ))
                if (
                    activation is None
                    or activation["lease_expires_at"] <= time.time()
                    or activation["fencing_token"] != int(fencing_token)
                ):
                    raise StaleFenceError(
                        "activation lease does not match expected fence",
                        details={
                            "agent_instance_id": agent_instance_id,
                            "session_id": session_id,
                            "expected_fence": int(fencing_token),
                        },
                    )
                row = await self._fetchone(
                    connection,
                    "SELECT * FROM kernel_inbox WHERE agent_instance_id=? AND session_id=? "
                    "AND (status='accepted' OR (status='claimed' AND claimed_fence != ?)) "
                    "ORDER BY accepted_seq LIMIT 1",
                    (agent_instance_id, session_id, int(fencing_token)),
                )
                if row is None:
                    await connection.commit()
                    return None
                if row["status"] == InboxState.ACCEPTED.value:
                    assert_inbox_transition(InboxState(row["status"]), InboxState.CLAIMED)
                await connection.execute(
                    "UPDATE kernel_inbox SET status='claimed', claimed_fence=? WHERE message_id=?",
                    (int(fencing_token), row["message_id"]),
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        await self._emit_activation(
            control_event(
                session_id=session_id,
                event_type="control.message_claimed",
                payload={"message_id": row["message_id"], "fencing_token": int(fencing_token)},
            ),
            activation,
            fencing_token,
        )
        return await self.load_message(row["message_id"])

    async def complete_claim(self, message_id: str, *, expected_fence: int) -> None:
        message_id = str(message_id)
        async with self._write_lock:
            connection = await self._begin()
            try:
                row = await self._fetchone(
                    connection, "SELECT * FROM kernel_inbox WHERE message_id=?", (message_id,)
                )
                if row is None:
                    raise InvalidCommandError(f"unknown message_id {message_id!r}")
                activation = await self._check_fence(
                    connection, row["agent_instance_id"], row["session_id"], expected_fence
                )
                if (
                    row["status"] != InboxState.CLAIMED.value
                    or row["claimed_fence"] != int(expected_fence)
                ):
                    raise StaleFenceError(
                        f"message {message_id!r} is not claimed at fence {expected_fence}"
                    )
                assert_inbox_transition(InboxState(row["status"]), InboxState.COMPLETED)
                await connection.execute(
                    "UPDATE kernel_inbox SET status='completed' WHERE message_id=?",
                    (message_id,),
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        await self._emit_activation(
            control_event(
                session_id=row["session_id"],
                event_type="control.message_completed",
                payload={"message_id": message_id, "fencing_token": int(expected_fence)},
            ),
            activation,
            expected_fence,
        )

    # -------------------------------------------------------------- interactions

    async def _check_interaction_guard(
        self,
        connection: aiosqlite.Connection,
        agent_instance_id: str,
        session_id: str,
        guard: ActivationWriteGuard,
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
        if not isinstance(service, LocalSessionService) or service.db_path != self.db_path.resolve():
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
        return await self._fetchone(
            connection,
            "SELECT * FROM kernel_interactions WHERE interaction_id=?"
            " AND agent_instance_id=? AND session_id=?",
            (
                interaction_id,
                activation["agent_instance_id"],
                activation["session_id"],
            ),
        )

    @staticmethod
    def _row_to_record(row: aiosqlite.Row | None) -> InteractionRecord | None:
        if row is None:
            return None
        from ksadk.interaction.contracts import InteractionPresentation

        presentation = None
        if row["presentation_json"]:
            presentation = InteractionPresentation.model_validate_json(
                row["presentation_json"]
            )
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
                json.loads(row["native_target_json"])
                if row["native_target_json"]
                else None
            ),
            continuation_metadata=(
                json.loads(row["continuation_json"])
                if row["continuation_json"]
                else None
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
            (
                record.presentation.model_dump_json()
                if record.presentation is not None
                else None
            ),
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
                    ):
                        await connection.commit()
                        return InteractionReceipt.model_validate_json(
                            existing_sub["receipt_json"]
                        )
                    raise InvalidCommandError(
                        "interaction already reached terminal status"
                        f" {current.status!r}",
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
                        response=submission.response,
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
                        json.dumps(submission.response, ensure_ascii=False),
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
                    " VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
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
                row = await self._interaction_row_for_guard(
                    connection, interaction_id, guard
                )
                if row is None:
                    raise InvalidCommandError(
                        f"unknown interaction_id {interaction_id!r}"
                    )
                await self._check_interaction_guard(
                    connection, row["agent_instance_id"], row["session_id"], guard
                )
                current = self._row_to_record(row)
                assert current is not None
                if is_terminal(current.status):
                    raise InvalidCommandError(
                        f"interaction already reached terminal status"
                        f" {current.status!r}",
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
                    "interaction.cancelled"
                    if status == "cancelled"
                    else "interaction.expired"
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
        cursor = await connection.execute(
            "SELECT * FROM kernel_interactions WHERE interaction_id=? LIMIT 2",
            (interaction_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        if len(rows) > 1:
            raise InvalidCommandError(
                f"interaction_id {interaction_id!r} is ambiguous without trusted scope",
                details={"reason": REQUEST_CONFLICT, "interaction_id": interaction_id},
            )
        return self._row_to_record(rows[0]) if rows else None

    async def list_pending_interactions(
        self, tenant_id: str, session_id: str
    ) -> list[InteractionRecord]:
        connection = await self._connect()
        cursor = await connection.execute(
            "SELECT * FROM kernel_interactions WHERE tenant_id=? AND session_id=?"
            " AND status='pending' ORDER BY created_at",
            (tenant_id, session_id),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        records = [self._row_to_record(row) for row in rows]
        return [r for r in records if r is not None]

    # ------------------------------------------------------------- activations

    async def acquire_activation(self, request: ActivationLeaseRequest) -> ActivationLease:
        async with self._write_lock:
            connection = await self._begin()
            try:
                row = await self._fetchone(
                    connection,
                    "SELECT * FROM kernel_activations WHERE agent_instance_id=? AND session_id=?",
                    (request.agent_instance_id, request.session_id),
                )
                expires_at = time.time() + request.lease_ttl_seconds
                if row is None:
                    token = 1
                elif row["released"] or row["lease_expires_at"] <= time.time():
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
                expires_iso = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()
                await connection.execute(
                    "INSERT INTO kernel_activations (agent_instance_id, session_id,"
                    " activation_id, fencing_token, lease_expires_at, lease_expires_at_iso,"
                    " released, runtime_type, bundle_digest, capability_digest)"
                    " VALUES (?,?,?,?,?,?,0,?,?,?)"
                    " ON CONFLICT(agent_instance_id, session_id) DO UPDATE SET"
                    " activation_id=excluded.activation_id,"
                    " fencing_token=excluded.fencing_token,"
                    " lease_expires_at=excluded.lease_expires_at,"
                    " lease_expires_at_iso=excluded.lease_expires_at_iso,"
                    " released=0, runtime_type=excluded.runtime_type,"
                    " bundle_digest=excluded.bundle_digest,"
                    " capability_digest=excluded.capability_digest",
                    (
                        request.agent_instance_id,
                        request.session_id,
                        request.activation_id,
                        token,
                        expires_at,
                        expires_iso,
                        request.runtime_type,
                        request.bundle_digest,
                        request.capability_digest,
                    ),
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return ActivationLease(
            agent_instance_id=request.agent_instance_id,
            activation_id=request.activation_id,
            fencing_token=token,
            lease_expires_at=expires_iso,
            bundle_digest=request.bundle_digest,
            runtime_type=request.runtime_type,
            capability_digest=request.capability_digest,
        )

    async def renew_activation(
        self, activation_id: str, *, expected_fence: int, lease_ttl_seconds: float
    ) -> ActivationLease:
        async with self._write_lock:
            connection = await self._begin()
            try:
                row = await self._fetchone(
                    connection,
                    "SELECT * FROM kernel_activations WHERE activation_id=?",
                    (activation_id,),
                )
                if row is None:
                    raise InvalidCommandError(f"unknown activation_id {activation_id!r}")
                if (
                    row["released"]
                    or row["lease_expires_at"] <= time.time()
                    or row["fencing_token"] != int(expected_fence)
                ):
                    raise StaleFenceError(
                        f"cannot renew activation {activation_id!r} at fence {expected_fence}"
                    )
                expires_at = time.time() + lease_ttl_seconds
                expires_iso = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()
                await connection.execute(
                    "UPDATE kernel_activations SET lease_expires_at=?, lease_expires_at_iso=?"
                    " WHERE activation_id=?",
                    (expires_at, expires_iso, activation_id),
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        return ActivationLease(
            agent_instance_id=row["agent_instance_id"],
            activation_id=activation_id,
            fencing_token=row["fencing_token"],
            lease_expires_at=expires_iso,
            bundle_digest=row["bundle_digest"],
            runtime_type=row["runtime_type"],
            capability_digest=row["capability_digest"],
        )

    async def release_activation(self, activation_id: str, *, expected_fence: int) -> None:
        async with self._write_lock:
            connection = await self._begin()
            try:
                row = await self._fetchone(
                    connection,
                    "SELECT * FROM kernel_activations WHERE activation_id=?",
                    (activation_id,),
                )
                if row is None:
                    raise InvalidCommandError(f"unknown activation_id {activation_id!r}")
                if row["released"] or row["fencing_token"] != int(expected_fence):
                    raise StaleFenceError(
                        f"cannot release activation {activation_id!r} at fence {expected_fence}"
                    )
                await connection.execute(
                    "UPDATE kernel_activations SET released=1, lease_expires_at=?"
                    " WHERE activation_id=?",
                    (time.time(), activation_id),
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    # ------------------------------------------------------------------ events

    async def append_event(
        self,
        envelope: SessionEventEnvelope,
        *,
        expected_fence: int,
        agent_instance_id: str | None = None,
    ) -> SessionEventEnvelope:
        activation = await self._resolve_activation(envelope.session_id, agent_instance_id)
        await self._check_fence(
            await self._connect(),
            activation["agent_instance_id"],
            envelope.session_id,
            expected_fence,
        )
        return await self._events.append(
            envelope,
            guard=ActivationWriteGuard(
                activation_id=activation["activation_id"],
                fencing_token=int(expected_fence),
            ),
        )

    async def _resolve_activation(
        self, session_id: str, agent_instance_id: str | None
    ) -> dict[str, Any]:
        connection = await self._connect()
        if agent_instance_id is not None:
            row = self._activation_row(await self._fetchone(
                connection,
                "SELECT * FROM kernel_activations WHERE agent_instance_id=? AND session_id=?",
                (agent_instance_id, session_id),
            ))
            if row is None:
                raise StaleFenceError(
                    "no active activation lease",
                    details={"agent_instance_id": agent_instance_id, "session_id": session_id},
                )
            return row
        cursor = await connection.execute(
            "SELECT * FROM kernel_activations WHERE session_id=? AND released=0",
            (session_id,),
        )
        rows = [self._activation_row(row) for row in await cursor.fetchall()]
        await cursor.close()
        rows = [row for row in rows if row is not None]
        if len(rows) != 1:
            raise StaleFenceError(
                "cannot resolve a single activation lease for session",
                details={"session_id": session_id, "matches": len(rows)},
            )
        return rows[0]

    # -------------------------------------------------------------------- runs

    async def load_run(self, run_id: str) -> RunRecord | None:
        row = await self._fetchone(
            await self._connect(), "SELECT * FROM kernel_runs WHERE run_id=?", (run_id,)
        )
        if row is None:
            return None
        return RunRecord(
            run_id=row["run_id"],
            agent_instance_id=row["agent_instance_id"],
            session_id=row["session_id"],
            state=row["state"],
            activation_fence=row["activation_fence"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            metadata=json.loads(row["metadata_json"]),
        )

    async def save_run_transition(
        self, run: RunRecord, *, expected_fence: int
    ) -> RunRecord:
        async with self._write_lock:
            connection = await self._begin()
            try:
                activation = await self._check_fence(
                    connection, run.agent_instance_id, run.session_id, expected_fence
                )
                existing = await self.load_run(run.run_id)
                assert_run_transition(existing.state if existing else None, run.state)
                if is_active_run(run.state):
                    cursor = await connection.execute(
                        "SELECT run_id FROM kernel_runs WHERE session_id=? AND run_id != ?"
                        " AND state IN ('running','paused','waiting')",
                        (run.session_id, run.run_id),
                    )
                    clash = await cursor.fetchone()
                    await cursor.close()
                    if clash is not None:
                        raise InvalidCommandError(
                            "session already has an active run",
                            details={
                                "session_id": run.session_id,
                                "active_run_id": clash["run_id"],
                            },
                        )
                timestamp = now_iso()
                stored = run.model_copy(
                    update={
                        "activation_fence": int(expected_fence),
                        "created_at": existing.created_at if existing else timestamp,
                        "updated_at": timestamp,
                    }
                )
                await connection.execute(
                    "INSERT INTO kernel_runs (run_id, agent_instance_id, session_id, state,"
                    " activation_fence, created_at, updated_at, metadata_json)"
                    " VALUES (?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(run_id) DO UPDATE SET state=excluded.state,"
                    " activation_fence=excluded.activation_fence,"
                    " updated_at=excluded.updated_at,"
                    " metadata_json=excluded.metadata_json",
                    (
                        stored.run_id,
                        stored.agent_instance_id,
                        stored.session_id,
                        stored.state.value,
                        stored.activation_fence,
                        stored.created_at,
                        stored.updated_at,
                        json.dumps(stored.metadata, ensure_ascii=False),
                    ),
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        await self._emit_activation(
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
            activation,
            expected_fence,
        )
        return stored


__all__ = ["SQLiteAgentKernelStore"]
