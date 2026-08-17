# -*- coding: utf-8 -*-
"""SQLite ``AgentKernelStore``（Phase 1 Task 3 Step 5）。

单文件 durable Inbox / Run / ActivationLease 存储：
- WAL journal + 每次 mutation ``BEGIN IMMEDIATE`` 做跨进程 CAS；
- schema migration 用 ``PRAGMA user_version`` 整数版本，重复启动幂等；
- 所有 fence 比较都发生在同一个写事务内，不匹配抛 :class:`StaleFenceError`；
- ControlEvent/v1 经注入的 SessionEventStore 追加，且只在事务 commit 之后。

只面向单机本地部署（local dev / serverless pod 单写者场景）；预发多写者
场景由 Task 4 的 PostgreSQL 适配器承接。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

from ksadk.events.session_event import SessionEventStore
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

SCHEMA_VERSION = 1

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
"""


class SQLiteAgentKernelStore:
    def __init__(
        self,
        db_path: str | Path,
        session_event_store: SessionEventStore,
    ) -> None:
        self.db_path = Path(db_path)
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

    async def _emit_admission(self, envelope: SessionEventEnvelope) -> None:
        await self._events.append(
            envelope,
            guard=AdmissionWriteGuard(
                authorization_ref="agent-kernel", command_id=uuid4()
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
                            )
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
                        )
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
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        # ControlEvent 只在事务 commit 之后追加。
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
            )
        )
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
                expires_iso = datetime.fromtimestamp(expires_at, tz=UTC).isoformat()
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
                expires_iso = datetime.fromtimestamp(expires_at, tz=UTC).isoformat()
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
            ),
            activation,
            expected_fence,
        )
        return stored


__all__ = ["SQLiteAgentKernelStore"]
