# -*- coding: utf-8 -*-
"""PostgreSQL ``AgentKernelStore``（Phase 1 Task 4）。

预发多写者 durable Inbox / Run / ActivationLease 存储：
- schema 见 ``ksadk/kernel/sql/001_agent_kernel.sql``（BIGINT fencing_token、
  TIMESTAMPTZ lease、JSONB payload、``(tenant_id, session_id, idempotency_key)`` 唯一）；
- claim 用 ``FOR UPDATE SKIP LOCKED`` 且仍按 ``accepted_seq`` 排序；
- activation takeover 用单条 ``INSERT .. ON CONFLICT .. DO UPDATE .. WHERE
  lease_expires_at <= now()``（或 released / 同 activation）原子 ``fencing_token + 1``；
- 每个 writer 事务的第一步是对 activation 行做 ``FOR SHARE`` compare-fence
  (:meth:`_assert_fence`)，token/expiry 不匹配抛 :class:`StaleFenceError`
  并回滚整个事务；
- 与 SQLite 版不同（Task 3 把 ControlEvent 放事务外），这里的 lease CAS、
  Inbox claim/complete、Run transition 与 SessionEvent append 都发生在
  **同一个** PostgreSQL 事务里，commit 前被 kill 不会留下半状态。
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ksadk.events.session_event import (
    _timestamp_to_float,
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
    RunState,
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
from ksadk.sessions._postgres_tables import (
    KSADK_PG_EVENTS_TABLE,
    KSADK_PG_SESSIONS_TABLE,
)

SCHEMA_PATH = Path(__file__).parent / "sql" / "001_agent_kernel.sql"

NONCE_RETENTION_SECONDS = 24 * 3600.0

ACTIVATION_FOR_SHARE_SQL = (
    "SELECT activation_id, fencing_token, lease_expires_at, released, runtime_type,"
    " bundle_digest, capability_digest, agent_instance_id, session_id"
    " FROM kernel_activations"
    " WHERE agent_instance_id = $1 AND session_id = $2"
    " FOR SHARE"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: str | None):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class PostgresKernelEventLog:
    """把 ``SessionEventEnvelope`` 写进共享 session log（ksadk_events）。

    ``append_on(connection, ...)`` 允许调用方把 event insert 并入一个已经
    打开的 kernel writer 事务；``append`` 则自开事务。
    """

    def __init__(
        self,
        pool: Any,
        *,
        namespace: str = "default",
        tenant_id: str = "default",
        workspace_id: str = "default",
    ) -> None:
        self._pool = pool
        self._namespace = namespace.strip() or "default"
        self._tenant_id = tenant_id.strip() or "default"
        self._workspace_id = workspace_id.strip() or "default"

    @asynccontextmanager
    async def _connection(self):
        if hasattr(self._pool, "acquire"):
            async with self._pool.acquire() as conn:
                yield conn
        else:
            yield self._pool

    async def append_on(
        self,
        connection: Any,
        envelope: SessionEventEnvelope,
        guard: Any | None = None,
    ) -> SessionEventEnvelope:
        if guard is not None:
            validate_write_guard(envelope, guard)
        if not envelope.session_id.strip():
            raise ValueError("session_id must be nonempty")
        packed = envelope_to_session_event(envelope)
        storage_id = session_event_storage_id(envelope.session_id, str(envelope.event_id))
        # 锁 session 行串行化 seq 分配，与 PostgresSessionService.append_event 相同。
        session_row = await connection.fetchrow(
            f"SELECT id FROM {KSADK_PG_SESSIONS_TABLE} WHERE namespace=$1 AND id=$2 FOR UPDATE",
            self._namespace,
            envelope.session_id,
        )
        if session_row is None:
            raise InvalidCommandError(
                f"session {envelope.session_id!r} does not exist in the shared event log"
            )
        next_seq = await connection.fetchval(
            f"SELECT COALESCE(MAX(seq_id), 0) + 1 FROM {KSADK_PG_EVENTS_TABLE}"
            " WHERE namespace=$1 AND session_id=$2",
            self._namespace,
            envelope.session_id,
        )
        seq = int(next_seq or 1)
        packed.bind_seq_id(seq)  # 把物理 seq 绑定回 runtime/session envelope 内容
        await connection.execute(
            f"""
            INSERT INTO {KSADK_PG_EVENTS_TABLE} (
                namespace, tenant_id, workspace_id, id, session_id, author,
                event_type, content_json, timestamp, state_delta_json,
                seq_id, invocation_id, metadata_json
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9,
                '{{}}'::jsonb, $10, $11, $12::jsonb
            )
            ON CONFLICT (namespace, id) DO NOTHING
            """,
            self._namespace,
            self._tenant_id,
            self._workspace_id,
            storage_id,
            envelope.session_id,
            packed.author,
            packed.event_type,
            json.dumps(packed.content, ensure_ascii=False),
            _timestamp_to_float(envelope.timestamp),
            int(next_seq or 1),
            packed.invocation_id,
            json.dumps(packed.metadata, ensure_ascii=False),
        )
        stored = await self._fetch_event(connection, envelope.session_id, storage_id)
        if stored is not None:
            return stored
        raise RuntimeError("kernel event insert did not persist")  # pragma: no cover

    async def _fetch_event(
        self, connection: Any, session_id: str, storage_id: str
    ) -> SessionEventEnvelope | None:
        row = await connection.fetchrow(
            f"SELECT content_json, metadata_json, seq_id, timestamp FROM {KSADK_PG_EVENTS_TABLE}"
            " WHERE namespace=$1 AND session_id=$2 AND id=$3",
            self._namespace,
            session_id,
            storage_id,
        )
        if row is None:
            return None
        from ksadk.sessions.base import SessionEvent

        event = SessionEvent(
            id=storage_id,
            session_id=session_id,
            author="",
            event_type="",
            content=json.loads(row["content_json"]),
            timestamp=row["timestamp"],
            seq_id=row["seq_id"],
            metadata=json.loads(row["metadata_json"]),
        )
        return session_event_to_envelope(event)

    async def append(
        self, envelope: SessionEventEnvelope, *, guard: Any | None = None
    ) -> SessionEventEnvelope:
        async with self._connection() as connection:
            async with connection.transaction():
                return await self.append_on(connection, envelope, guard)

    async def read(self, session_id: str, after_seq: int, limit: int) -> list[SessionEventEnvelope]:
        if limit < 1:
            raise ValueError("limit must be positive")
        async with self._connection() as connection:
            rows = await connection.fetch(
                f"SELECT id, content_json, metadata_json, seq_id, timestamp, author,"
                f" event_type, invocation_id FROM {KSADK_PG_EVENTS_TABLE}"
                " WHERE namespace=$1 AND session_id=$2 AND seq_id > $3"
                " ORDER BY seq_id LIMIT $4",
                self._namespace,
                session_id,
                int(after_seq),
                int(limit),
            )
        from ksadk.sessions.base import SessionEvent

        envelopes: list[SessionEventEnvelope] = []
        for row in rows:
            event = SessionEvent(
                id=row["id"],
                session_id=session_id,
                author=row["author"],
                event_type=row["event_type"],
                content=json.loads(row["content_json"]),
                timestamp=row["timestamp"],
                seq_id=row["seq_id"],
                invocation_id=row["invocation_id"],
                metadata=json.loads(row["metadata_json"]),
            )
            envelope = session_event_to_envelope(event)
            if envelope is not None:
                envelopes.append(envelope)
        return envelopes


class PostgresNonceStore:
    """跨 Pod / 重启 durable 的 mutation nonce 单次使用存储。

    单条 ``INSERT .. ON CONFLICT (nonce) DO NOTHING`` 原子占位；冲突时读回
    既有 identity 比较：同 ``(command_id, idempotency_key)`` 是网络重试，
    否则判为重放（返回 False）。注册成功时顺带清理超过 retention 的旧行。
    """

    def __init__(self, pool: Any, *, retention_seconds: float = NONCE_RETENTION_SECONDS) -> None:
        self._pool = pool
        self._retention = float(retention_seconds)

    @asynccontextmanager
    async def _connection(self):
        if hasattr(self._pool, "acquire"):
            async with self._pool.acquire() as conn:
                yield conn
        else:
            yield self._pool

    async def register(
        self, nonce: str, command_id: str, idempotency_key: str
    ) -> bool:
        async with self._connection() as connection:
            async with connection.transaction():
                inserted = await connection.fetchval(
                    "INSERT INTO kernel_permit_nonces (nonce, command_id,"
                    " idempotency_key) VALUES ($1, $2, $3)"
                    " ON CONFLICT (nonce) DO NOTHING RETURNING nonce",
                    nonce,
                    command_id,
                    idempotency_key,
                )
                if inserted is not None:
                    await connection.execute(
                        "DELETE FROM kernel_permit_nonces"
                        " WHERE created_at < now() - make_interval(secs => $1)",
                        self._retention,
                    )
                    return True
                existing = await connection.fetchrow(
                    "SELECT command_id, idempotency_key FROM kernel_permit_nonces"
                    " WHERE nonce=$1",
                    nonce,
                )
                return existing is not None and (
                    existing["command_id"] == command_id
                    and existing["idempotency_key"] == idempotency_key
                )


class PostgresAgentKernelStore:
    def __init__(
        self,
        pool: Any,
        session_event_log: PostgresKernelEventLog | None,
        *,
        tenant_id: str = "default",
        owns_pool: bool = False,
    ) -> None:
        self._pool = pool
        self._events = session_event_log or PostgresKernelEventLog(pool)
        self.tenant_id = tenant_id
        self._owns_pool = owns_pool

    # ------------------------------------------------------------- lifecycle

    @asynccontextmanager
    async def _connection(self):
        if hasattr(self._pool, "acquire"):
            async with self._pool.acquire() as conn:
                yield conn
        else:
            yield self._pool

    async def ensure_schema(self) -> None:
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        async with self._connection() as connection:
            await connection.execute(schema)

    async def reset_for_tests(self) -> None:
        async with self._connection() as connection:
            await connection.execute(
                "DELETE FROM kernel_inbox; DELETE FROM kernel_runs;"
                " DELETE FROM kernel_activations; DELETE FROM kernel_accepted_seq;"
                " DELETE FROM kernel_interactions; DELETE FROM kernel_interaction_submissions;"
                " DELETE FROM ksadk_events WHERE namespace = 'default';"
            )

    async def close(self) -> None:
        if self._owns_pool and hasattr(self._pool, "close"):
            await self._pool.close()

    # ---------------------------------------------------------------- helpers

    async def _assert_fence(
        self, connection: Any, agent_instance_id: str, session_id: str, expected_fence: int
    ) -> dict[str, Any]:
        """compare-fence：事务内 ``FOR SHARE`` 读 activation 并比较 token/expiry。"""

        row = await connection.fetchrow(
            ACTIVATION_FOR_SHARE_SQL, agent_instance_id, session_id
        )
        if (
            row is None
            or row["released"]
            or row["lease_expires_at"] <= _now()
            or int(row["fencing_token"]) != int(expected_fence)
        ):
            raise StaleFenceError(
                "activation lease does not match expected fence",
                details={
                    "agent_instance_id": agent_instance_id,
                    "session_id": session_id,
                    "expected_fence": int(expected_fence),
                },
            )
        return dict(row)

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

    async def _append_admission(
        self, connection: Any, command: AgentControlCommand, envelope: SessionEventEnvelope
    ) -> None:
        # accepted/rejected 事实的 write guard 绑定提交方的 permit 引用
        # （server permit_id 或本地 authority），不落内核自造 ref。
        await self._events.append_on(
            connection,
            envelope,
            AdmissionWriteGuard(
                authorization_ref=command.authorization_ref,
                command_id=command.command_id,
            ),
        )

    async def _append_activation_fact(
        self,
        connection: Any,
        envelope: SessionEventEnvelope,
        activation: dict[str, Any],
        fence: int,
    ) -> SessionEventEnvelope:
        return await self._events.append_on(
            connection,
            envelope,
            ActivationWriteGuard(
                activation_id=activation["activation_id"], fencing_token=int(fence)
            ),
        )

    # --------------------------------------------------------------- commands

    async def accept_command(
        self, command: AgentControlCommand, *, queue_limit: int
    ) -> AgentControlReceipt:
        if queue_limit < 1:
            raise InvalidCommandError("queue_limit must be positive")
        async with self._connection() as connection:
            async with connection.transaction():
                existing = await connection.fetchrow(
                    "SELECT message_id, accepted_seq, request_digest FROM kernel_inbox"
                    " WHERE tenant_id=$1 AND session_id=$2 AND idempotency_key=$3",
                    self.tenant_id,
                    command.session_id,
                    command.idempotency_key,
                )
                if existing is not None:
                    if existing["request_digest"] != command_digest(command):
                        await self._append_admission(
                            connection,
                            command,
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
                    return self._receipt(
                        command,
                        "duplicate",
                        message_id=str(existing["message_id"]),
                        accepted_seq=int(existing["accepted_seq"]),
                    )

                depth = await connection.fetchval(
                    "SELECT COUNT(*) FROM kernel_inbox WHERE tenant_id=$1"
                    " AND agent_instance_id=$2 AND session_id=$3 AND status='accepted'",
                    self.tenant_id,
                    command.agent_instance_id,
                    command.session_id,
                )
                if int(depth) >= queue_limit:
                    await self._append_admission(
                        connection,
                        command,
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

                accepted_seq = await connection.fetchval(
                    "UPDATE kernel_accepted_seq SET last_seq = last_seq + 1"
                    " WHERE tenant_id=$1 AND session_id=$2 RETURNING last_seq",
                    self.tenant_id,
                    command.session_id,
                )
                if accepted_seq is None:
                    await connection.execute(
                        "INSERT INTO kernel_accepted_seq (tenant_id, session_id, last_seq)"
                        " VALUES ($1, $2, 1) ON CONFLICT (tenant_id, session_id)"
                        " DO UPDATE SET last_seq = kernel_accepted_seq.last_seq + 1"
                        " RETURNING last_seq",
                        self.tenant_id,
                        command.session_id,
                    )
                    accepted_seq = await connection.fetchval(
                        "SELECT last_seq FROM kernel_accepted_seq"
                        " WHERE tenant_id=$1 AND session_id=$2",
                        self.tenant_id,
                        command.session_id,
                    )
                accepted_seq = int(accepted_seq or 1)
                message_id = new_message_id()
                await connection.execute(
                    "INSERT INTO kernel_inbox (message_id, tenant_id, agent_instance_id,"
                    " session_id, idempotency_key, request_digest, accepted_seq, status,"
                    " claimed_fence, payload) VALUES ($1::uuid, $2, $3, $4, $5, $6, $7,"
                    " 'accepted', NULL, $8::jsonb)",
                    message_id,
                    self.tenant_id,
                    command.agent_instance_id,
                    command.session_id,
                    command.idempotency_key,
                    command_digest(command),
                    accepted_seq,
                    command.model_dump_json(),
                )
                # ControlEvent 与 inbox insert 同一事务（SQLite 版在事务外，此处按计划收进）。
                await self._append_admission(
                    connection,
                    command,
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
                )
        return self._receipt(
            command, "accepted", message_id=message_id, accepted_seq=accepted_seq
        )

    async def load_message(self, message_id: str) -> InboxMessage | None:
        async with self._connection() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM kernel_inbox WHERE message_id=$1::uuid", str(message_id)
            )
        return self._row_to_message(row)

    @staticmethod
    def _row_to_message(row: Any) -> InboxMessage | None:
        if row is None:
            return None
        return InboxMessage(
            message_id=str(row["message_id"]),
            agent_instance_id=row["agent_instance_id"],
            session_id=row["session_id"],
            idempotency_key=row["idempotency_key"],
            request_digest=row["request_digest"],
            accepted_seq=int(row["accepted_seq"]),
            status=InboxState(row["status"]),
            claimed_fence=(
                int(row["claimed_fence"]) if row["claimed_fence"] is not None else None
            ),
            command=AgentControlCommand.model_validate_json(row["payload"]),
        )

    async def load_by_idempotency(
        self, session_id: str, idempotency_key: str
    ) -> InboxMessage | None:
        async with self._connection() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM kernel_inbox WHERE tenant_id=$1 AND session_id=$2"
                " AND idempotency_key=$3",
                self.tenant_id,
                session_id,
                idempotency_key,
            )
        return self._row_to_message(row)

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

        与 InMemory 版语义一致：只追加 ``control.command_rejected`` 事实，
        不写 Inbox 行。
        """
        async with self._connection() as connection:
            async with connection.transaction():
                await self._append_admission(
                    connection,
                    command,
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
                )
        return self._receipt(
            command,
            status,
            error=ControlError(code=code, message=message, retryable=retryable),
        )

    async def list_messages(
        self, agent_instance_id: str, session_id: str | None = None
    ) -> list[InboxMessage]:
        async with self._connection() as connection:
            rows = await connection.fetch(
                "SELECT * FROM kernel_inbox WHERE agent_instance_id=$1"
                + (" AND session_id=$2" if session_id else "")
                + " ORDER BY accepted_seq",
                agent_instance_id,
                *([session_id] if session_id else []),
            )
        return [m for m in (self._row_to_message(r) for r in rows) if m is not None]

    async def list_pending(
        self,
        agent_instance_id: str,
        session_id: str | None = None,
        *,
        fencing_token: int | None = None,
    ) -> list[InboxMessage]:
        """按 accepted_seq 返回可恢复消息。

        传入当前 fence 时，旧 fence 留下的 ``claimed`` 也必须可见；真正的
        owner 校验与 token 改写由 ``claim_message`` 在事务内完成。否则 Pod
        恰好在 claim 后退出，会让 stale claimed 永久挡住 FIFO 头。
        """
        sql = (
            "SELECT * FROM kernel_inbox WHERE agent_instance_id=$1"
            + (" AND session_id=$2" if session_id else "")
            + (
                " AND status IN ('accepted','claimed')"
                if fencing_token is not None
                else " AND status='accepted'"
            )
            + " ORDER BY accepted_seq"
        )
        args: list[Any] = [agent_instance_id]
        if session_id:
            args.append(session_id)
        async with self._connection() as connection:
            rows = await connection.fetch(sql, *args)
        return [m for m in (self._row_to_message(r) for r in rows) if m is not None]

    async def claim_message(self, message_id: str, fencing_token: int) -> InboxMessage:
        """按 message_id 认领（worker 选择性 FIFO）；同 fence 重复认领幂等。"""
        message_id = str(message_id)
        async with self._connection() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    "SELECT * FROM kernel_inbox WHERE message_id=$1::uuid FOR UPDATE",
                    message_id,
                )
                if row is None:
                    raise InvalidCommandError(f"unknown message_id {message_id!r}")
                if (
                    row["status"] == InboxState.CLAIMED.value
                    and int(row["claimed_fence"]) == int(fencing_token)
                ):
                    return self._row_to_message(row)  # type: ignore[return-value]
                activation = await self._assert_fence(
                    connection,
                    row["agent_instance_id"],
                    row["session_id"],
                    fencing_token,
                )
                if row["status"] not in (
                    InboxState.ACCEPTED.value,
                    InboxState.CLAIMED.value,
                ):
                    raise InvalidCommandError(
                        f"message {message_id!r} is not claimable"
                        f" at status {row['status']}"
                    )
                command = AgentControlCommand.model_validate_json(row["payload"])
                if command.command_type == "enqueue":
                    # Strict per-session FIFO is a database invariant, not a
                    # scheduler convention.  A second worker may have listed
                    # pending rows before the first worker committed its
                    # claim.  Never let it skip an earlier enqueue that is
                    # still accepted/claimed; the query also observes an
                    # uncommitted earlier update as ``accepted`` under READ
                    # COMMITTED, so the later claim fails closed.
                    earlier = await connection.fetchval(
                        "SELECT EXISTS (SELECT 1 FROM kernel_inbox"
                        " WHERE tenant_id=$1 AND agent_instance_id=$2"
                        " AND session_id=$3 AND accepted_seq < $4"
                        " AND status IN ('accepted','claimed')"
                        " AND payload->>'command_type'='enqueue')",
                        self.tenant_id,
                        row["agent_instance_id"],
                        row["session_id"],
                        int(row["accepted_seq"]),
                    )
                    if earlier:
                        raise InvalidCommandError(
                            f"message {message_id!r} is not the FIFO enqueue head"
                        )
                if row["status"] == InboxState.ACCEPTED.value:
                    assert_inbox_transition(
                        InboxState(row["status"]), InboxState.CLAIMED
                    )
                await connection.execute(
                    "UPDATE kernel_inbox SET status='claimed', claimed_fence=$1"
                    " WHERE message_id=$2::uuid",
                    int(fencing_token),
                    message_id,
                )
                await self._append_activation_fact(
                    connection,
                    control_event(
                        session_id=row["session_id"],
                        event_type="control.message_claimed",
                        payload={
                            "message_id": message_id,
                            "accepted_seq": int(row["accepted_seq"]),
                            "fencing_token": int(fencing_token),
                        },
                    ),
                    activation,
                    fencing_token,
                )
        return await self.load_message(message_id)  # type: ignore[return-value]

    async def discard_claim(self, message_id: str, *, expected_fence: int) -> None:
        """typed rejection 的确定性收口：CLAIMED -> DISCARDED。"""
        message_id = str(message_id)
        async with self._connection() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    "SELECT * FROM kernel_inbox WHERE message_id=$1::uuid FOR UPDATE",
                    message_id,
                )
                if row is None:
                    raise InvalidCommandError(f"unknown message_id {message_id!r}")
                activation = await self._assert_fence(
                    connection,
                    row["agent_instance_id"],
                    row["session_id"],
                    expected_fence,
                )
                if (
                    row["status"] != InboxState.CLAIMED.value
                    or int(row["claimed_fence"]) != int(expected_fence)
                ):
                    raise StaleFenceError(
                        f"message {message_id!r} is not claimed at fence {expected_fence}"
                    )
                assert_inbox_transition(InboxState(row["status"]), InboxState.DISCARDED)
                await connection.execute(
                    "UPDATE kernel_inbox SET status='discarded' WHERE message_id=$1::uuid",
                    message_id,
                )
                await self._append_activation_fact(
                    connection,
                    control_event(
                        session_id=row["session_id"],
                        event_type="control.message_discarded",
                        payload={
                            "message_id": message_id,
                            "fencing_token": int(expected_fence),
                        },
                    ),
                    activation,
                    expected_fence,
                )

    async def inbox_depth(
        self, agent_instance_id: str, session_id: str | None = None
    ) -> int:
        async with self._connection() as connection:
            return int(
                await connection.fetchval(
                    "SELECT COUNT(*) FROM kernel_inbox WHERE agent_instance_id=$1"
                    + (" AND session_id=$2" if session_id else "")
                    + " AND status='accepted'",
                    agent_instance_id,
                    *([session_id] if session_id else []),
                )
            )

    async def find_active_run(
        self, agent_instance_id: str, session_id: str | None = None
    ) -> RunRecord | None:
        async with self._connection() as connection:
            rows = await connection.fetch(
                "SELECT * FROM kernel_runs WHERE agent_instance_id=$1"
                + (" AND session_id=$2" if session_id else "")
                + " ORDER BY created_at",
                agent_instance_id,
                *([session_id] if session_id else []),
            )
        for row in rows:
            if is_active_run(RunState(row["state"])):
                return RunRecord(
                    run_id=row["run_id"],
                    agent_instance_id=row["agent_instance_id"],
                    session_id=row["session_id"],
                    state=row["state"],
                    activation_fence=int(row["activation_fence"]),
                    created_at=row["created_at"].isoformat(),
                    updated_at=row["updated_at"].isoformat(),
                    metadata=json.loads(row["metadata"]),
                )
        return None

    async def current_lease(
        self, agent_instance_id: str, session_id: str | None = None
    ) -> ActivationLease | None:
        async with self._connection() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM kernel_activations WHERE agent_instance_id=$1"
                + (" AND session_id=$2" if session_id else "")
                + " AND released=FALSE AND lease_expires_at > now()"
                + (" ORDER BY lease_expires_at DESC LIMIT 1"),
                agent_instance_id,
                *([session_id] if session_id else []),
            )
        if row is None:
            return None
        return ActivationLease(
            agent_instance_id=row["agent_instance_id"],
            activation_id=row["activation_id"],
            fencing_token=int(row["fencing_token"]),
            lease_expires_at=row["lease_expires_at"].isoformat(),
            bundle_digest=row["bundle_digest"],
            runtime_type=row["runtime_type"],
            capability_digest=row["capability_digest"],
        )

    async def claim_next(
        self, agent_instance_id: str, session_id: str, fencing_token: int
    ) -> InboxMessage | None:
        async with self._connection() as connection:
            async with connection.transaction():
                activation = await self._assert_fence(
                    connection, agent_instance_id, session_id, fencing_token
                )
                row = await connection.fetchrow(
                    "SELECT message_id, status FROM kernel_inbox"
                    " WHERE agent_instance_id=$1 AND session_id=$2"
                    " AND (status='accepted' OR (status='claimed' AND claimed_fence <> $3))"
                    " ORDER BY accepted_seq"
                    " FOR UPDATE SKIP LOCKED"
                    " LIMIT 1",
                    agent_instance_id,
                    session_id,
                    int(fencing_token),
                )
                if row is None:
                    return None
                if row["status"] == InboxState.ACCEPTED.value:
                    assert_inbox_transition(InboxState(row["status"]), InboxState.CLAIMED)
                await connection.execute(
                    "UPDATE kernel_inbox SET status='claimed', claimed_fence=$1"
                    " WHERE message_id=$2::uuid",
                    int(fencing_token),
                    str(row["message_id"]),
                )
                await self._append_activation_fact(
                    connection,
                    control_event(
                        session_id=session_id,
                        event_type="control.message_claimed",
                        payload={
                            "message_id": str(row["message_id"]),
                            "fencing_token": int(fencing_token),
                        },
                    ),
                    activation,
                    fencing_token,
                )
        return await self.load_message(row["message_id"])

    async def complete_claim(self, message_id: str, *, expected_fence: int) -> None:
        message_id = str(message_id)
        async with self._connection() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    "SELECT * FROM kernel_inbox WHERE message_id=$1::uuid", message_id
                )
                if row is None:
                    raise InvalidCommandError(f"unknown message_id {message_id!r}")
                activation = await self._assert_fence(
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
                    "UPDATE kernel_inbox SET status='completed' WHERE message_id=$1::uuid",
                    message_id,
                )
                await self._append_activation_fact(
                    connection,
                    control_event(
                        session_id=row["session_id"],
                        event_type="control.message_completed",
                        payload={"message_id": message_id, "fencing_token": int(expected_fence)},
                    ),
                    activation,
                    expected_fence,
                )

    # -------------------------------------------------------------- interactions

    async def _assert_interaction_guard(
        self, connection: Any, agent_instance_id: str, session_id: str, guard: Any
    ) -> dict[str, Any]:
        row = await connection.fetchrow(
            "SELECT activation_id, agent_instance_id, session_id, fencing_token,"
            " lease_expires_at, released FROM kernel_activations"
            " WHERE activation_id = $1 AND agent_instance_id = $2"
            " AND session_id = $3 FOR SHARE",
            guard.activation_id,
            agent_instance_id,
            session_id,
        )
        if (
            row is None
            or row["released"]
            or row["lease_expires_at"] <= _now()
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
        row lock.  This prevents a same-id record in another tenant from being
        selected and then rejected only after information has been consulted.
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
            or activation["lease_expires_at"] <= _now()
            or int(activation["fencing_token"]) != int(guard.fencing_token)
        ):
            raise StaleFenceError(
                "interaction write guard does not match the current lease",
                details={
                    "activation_id": guard.activation_id,
                    "fencing_token": int(guard.fencing_token),
                },
            )
        return await connection.fetchrow(
            "SELECT * FROM kernel_interactions WHERE interaction_id=$1"
            " AND agent_instance_id=$2 AND session_id=$3 FOR UPDATE",
            interaction_id,
            activation["agent_instance_id"],
            activation["session_id"],
        )

    @staticmethod
    def _interaction_row_to_record(row: Any) -> InteractionRecord:
        from ksadk.interaction.contracts import InteractionPresentation

        presentation = None
        if row["presentation"] is not None:
            presentation = InteractionPresentation.model_validate(
                json.loads(row["presentation"])
            )
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
            expires_at=(
                row["expires_at"].isoformat() if row["expires_at"] is not None else None
            ),
            presentation=presentation,
            provider_id=row["provider_id"] or "",
            native_target=(
                json.loads(row["native_target"])
                if row["native_target"] is not None
                else None
            ),
            continuation_metadata=(
                json.loads(row["continuation_metadata"])
                if row["continuation_metadata"] is not None
                else None
            ),
        )

    async def request(
        self, record: InteractionRecord, *, guard: Any
    ) -> InteractionRecord:
        digest = request_digest(record)
        async with self._connection() as connection:
            async with connection.transaction():
                await self._assert_interaction_guard(
                    connection, record.agent_instance_id, record.session_id, guard
                )
                existing = await connection.fetchrow(
                    "SELECT * FROM kernel_interactions"
                    " WHERE tenant_id=$1 AND interaction_id=$2",
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
                        continuation_metadata, request_digest, updated_at
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9, 'pending',
                        $10::timestamptz, $11::timestamptz, $12, $13::jsonb,
                        $14::jsonb, $15, now()
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
                )
                # requested 事件与 pending 行同一事务：commit 前被 kill 无半状态。
                await self._events.append_on(
                    connection, requested_event_payload(record, now_iso()), guard
                )
        return record

    async def resolve(
        self, submission: InteractionSubmission, *, guard: Any
    ) -> InteractionReceipt:
        sub_digest = submission_digest(submission)
        async with self._connection() as connection:
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
                    ):
                        return InteractionReceipt.model_validate(
                            json.loads(existing_sub["receipt"])
                        )
                    raise InvalidCommandError(
                        f"interaction already reached terminal status"
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
                stored = await self._events.append_on(
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
                    "UPDATE kernel_interactions SET revision=$1, status='resolved',"
                    " response=$2::jsonb, outcome=$3, actor=$4, event_id=$5::uuid,"
                    " accepted_seq=$6, fencing_token=$7, updated_at=now()"
                    " WHERE tenant_id=$8 AND interaction_id=$9",
                    updated.revision,
                    json.dumps(submission.response, ensure_ascii=False),
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
                    " VALUES ($1, $2, $3, $4, $5::jsonb) ON CONFLICT DO NOTHING",
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
        async with self._connection() as connection:
            async with connection.transaction():
                row = await self._interaction_row_for_guard(
                    connection, interaction_id, guard
                )
                if row is None:
                    raise InvalidCommandError(
                        f"unknown interaction_id {interaction_id!r}"
                    )
                await self._assert_interaction_guard(
                    connection, row["agent_instance_id"], row["session_id"], guard
                )
                current = self._interaction_row_to_record(row)
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
                " AND status='pending' ORDER BY created_at",
                tenant_id,
                session_id,
            )
        return [self._interaction_row_to_record(row) for row in rows]

    # ------------------------------------------------------------- activations

    async def acquire_activation(self, request: ActivationLeaseRequest) -> ActivationLease:
        async with self._connection() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    INSERT INTO kernel_activations (
                        agent_instance_id, session_id, activation_id, fencing_token,
                        lease_expires_at, runtime_type, bundle_digest, capability_digest
                    ) VALUES (
                        $1, $2, $3, 1, now() + make_interval(secs => $4), $5, $6, $7
                    )
                    ON CONFLICT (agent_instance_id, session_id) DO UPDATE SET
                        activation_id = excluded.activation_id,
                        fencing_token = CASE
                            WHEN kernel_activations.activation_id = excluded.activation_id
                                THEN kernel_activations.fencing_token
                            ELSE kernel_activations.fencing_token + 1
                        END,
                        lease_expires_at = excluded.lease_expires_at,
                        released = FALSE,
                        runtime_type = excluded.runtime_type,
                        bundle_digest = excluded.bundle_digest,
                        capability_digest = excluded.capability_digest
                    WHERE kernel_activations.released
                       OR kernel_activations.lease_expires_at <= now()
                       OR kernel_activations.activation_id = excluded.activation_id
                    RETURNING fencing_token, lease_expires_at
                    """,
                    request.agent_instance_id,
                    request.session_id,
                    request.activation_id,
                    request.lease_ttl_seconds,
                    request.runtime_type,
                    request.bundle_digest,
                    request.capability_digest,
                )
                if row is None:
                    holder = await connection.fetchrow(
                        "SELECT activation_id, lease_expires_at FROM kernel_activations"
                        " WHERE agent_instance_id=$1 AND session_id=$2",
                        request.agent_instance_id,
                        request.session_id,
                    )
                    raise InvalidCommandError(
                        "activation lease is still held by another owner",
                        details={
                            "holder": holder["activation_id"] if holder else None,
                            "lease_expires_at": (
                                holder["lease_expires_at"].isoformat() if holder else None
                            ),
                        },
                    )
        return ActivationLease(
            agent_instance_id=request.agent_instance_id,
            activation_id=request.activation_id,
            fencing_token=int(row["fencing_token"]),
            lease_expires_at=row["lease_expires_at"].isoformat(),
            bundle_digest=request.bundle_digest,
            runtime_type=request.runtime_type,
            capability_digest=request.capability_digest,
        )

    async def renew_activation(
        self, activation_id: str, *, expected_fence: int, lease_ttl_seconds: float
    ) -> ActivationLease:
        async with self._connection() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    "SELECT * FROM kernel_activations WHERE activation_id=$1 FOR UPDATE",
                    activation_id,
                )
                if row is None:
                    raise InvalidCommandError(f"unknown activation_id {activation_id!r}")
                if (
                    row["released"]
                    or row["lease_expires_at"] <= _now()
                    or int(row["fencing_token"]) != int(expected_fence)
                ):
                    raise StaleFenceError(
                        f"cannot renew activation {activation_id!r} at fence {expected_fence}"
                    )
                expires_at = await connection.fetchval(
                    "UPDATE kernel_activations SET lease_expires_at = now()"
                    " + make_interval(secs => $1) WHERE activation_id=$2"
                    " RETURNING lease_expires_at",
                    lease_ttl_seconds,
                    activation_id,
                )
        return ActivationLease(
            agent_instance_id=row["agent_instance_id"],
            activation_id=activation_id,
            fencing_token=int(row["fencing_token"]),
            lease_expires_at=expires_at.isoformat(),
            bundle_digest=row["bundle_digest"],
            runtime_type=row["runtime_type"],
            capability_digest=row["capability_digest"],
        )

    async def release_activation(self, activation_id: str, *, expected_fence: int) -> None:
        async with self._connection() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    "SELECT fencing_token, released FROM kernel_activations"
                    " WHERE activation_id=$1 FOR UPDATE",
                    activation_id,
                )
                if row is None:
                    raise InvalidCommandError(f"unknown activation_id {activation_id!r}")
                if row["released"] or int(row["fencing_token"]) != int(expected_fence):
                    raise StaleFenceError(
                        f"cannot release activation {activation_id!r} at fence {expected_fence}"
                    )
                await connection.execute(
                    "UPDATE kernel_activations SET released=TRUE, lease_expires_at=now()"
                    " WHERE activation_id=$1",
                    activation_id,
                )

    # ------------------------------------------------------------------ events

    async def append_event(
        self,
        envelope: SessionEventEnvelope,
        *,
        expected_fence: int,
        agent_instance_id: str | None = None,
    ) -> SessionEventEnvelope:
        async with self._connection() as connection:
            async with connection.transaction():
                activation = await self._resolve_activation(
                    connection, envelope.session_id, agent_instance_id
                )
                # fence 比较必须发生在 event insert 之前且同一事务。
                await self._assert_fence(
                    connection,
                    activation["agent_instance_id"],
                    envelope.session_id,
                    expected_fence,
                )
                return await self._append_activation_fact(
                    connection, envelope, activation, expected_fence
                )

    async def _resolve_activation(
        self, connection: Any, session_id: str, agent_instance_id: str | None
    ) -> dict[str, Any]:
        if agent_instance_id is not None:
            row = await connection.fetchrow(
                "SELECT activation_id, agent_instance_id, session_id, released,"
                " fencing_token, lease_expires_at FROM kernel_activations"
                " WHERE agent_instance_id=$1 AND session_id=$2",
                agent_instance_id,
                session_id,
            )
            if row is None or row["released"]:
                raise StaleFenceError(
                    "no active activation lease",
                    details={"agent_instance_id": agent_instance_id, "session_id": session_id},
                )
            return dict(row)
        rows = await connection.fetch(
            "SELECT activation_id, agent_instance_id, session_id, released,"
            " fencing_token, lease_expires_at FROM kernel_activations WHERE session_id=$1",
            session_id,
        )
        active = [dict(row) for row in rows if not row["released"]]
        if len(active) != 1:
            raise StaleFenceError(
                "cannot resolve a single activation lease for session",
                details={"session_id": session_id, "matches": len(active)},
            )
        return active[0]

    # -------------------------------------------------------------------- runs

    async def load_run(self, run_id: str) -> RunRecord | None:
        async with self._connection() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM kernel_runs WHERE run_id=$1", run_id
            )
        if row is None:
            return None
        return RunRecord(
            run_id=row["run_id"],
            agent_instance_id=row["agent_instance_id"],
            session_id=row["session_id"],
            state=row["state"],
            activation_fence=int(row["activation_fence"]),
            created_at=row["created_at"].isoformat(),
            updated_at=row["updated_at"].isoformat(),
            metadata=json.loads(row["metadata"]),
        )

    async def save_run_transition(
        self, run: RunRecord, *, expected_fence: int
    ) -> RunRecord:
        async with self._connection() as connection:
            async with connection.transaction():
                activation = await self._assert_fence(
                    connection, run.agent_instance_id, run.session_id, expected_fence
                )
                existing = await connection.fetchrow(
                    "SELECT * FROM kernel_runs WHERE run_id=$1", run.run_id
                )
                assert_run_transition(
                    RunState(existing["state"]) if existing is not None else None, run.state
                )
                if is_active_run(run.state):
                    clash = await connection.fetchval(
                        "SELECT run_id FROM kernel_runs WHERE session_id=$1 AND run_id <> $2"
                        " AND state IN ('running','paused','waiting')",
                        run.session_id,
                        run.run_id,
                    )
                    if clash is not None:
                        raise InvalidCommandError(
                            "session already has an active run",
                            details={"session_id": run.session_id, "active_run_id": clash},
                        )
                timestamp = now_iso()
                stored = run.model_copy(
                    update={
                        "activation_fence": int(expected_fence),
                        "created_at": (
                            existing["created_at"].isoformat() if existing else timestamp
                        ),
                        "updated_at": timestamp,
                    }
                )
                await connection.execute(
                    """
                    INSERT INTO kernel_runs (run_id, tenant_id, agent_instance_id, session_id,
                        state, activation_fence, created_at, updated_at, metadata)
                    VALUES ($1, $2, $3, $4, $5, $6, now(), now(), $7::jsonb)
                    ON CONFLICT (run_id) DO UPDATE SET
                        state = excluded.state,
                        activation_fence = excluded.activation_fence,
                        updated_at = now(),
                        metadata = excluded.metadata
                    """,
                    stored.run_id,
                    self.tenant_id,
                    stored.agent_instance_id,
                    stored.session_id,
                    stored.state.value,
                    stored.activation_fence,
                    json.dumps(stored.metadata, ensure_ascii=False),
                )
                await self._append_activation_fact(
                    connection,
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


class PostgresFencedSessionEventStore:
    """事务级 fenced ``SessionEventStore``（Task 4 Step 5）。

    typed RuntimeEvent 写路径（``RuntimeEventStore.append -> append(envelope,
    guard=ActivationWriteGuard)``）的缺口修复：每个 ActivationWriteGuard
    append 都在**同一个 PostgreSQL 事务**里先对 activation 行做
    ``FOR SHARE`` compare-fence（activation_id / fencing_token / 未过期 /
    未 released），再执行 event insert——被 takeover 的旧 owner 在写出任何
    runtime/progress/terminal 事实之前就被 :class:`StaleFenceError` 回滚。

    AdmissionWriteGuard（accepted/rejected admission 事实）继续由
    :class:`PostgresAgentKernelStore` 的 writer 事务内联处理；独立调用时
    走 event log 自开事务。
    """

    def __init__(self, store: "PostgresAgentKernelStore") -> None:
        self._store = store
        self._log = store._events

    @asynccontextmanager
    async def _connection(self):
        async with self._store._connection() as connection:
            yield connection

    async def append(
        self, envelope: SessionEventEnvelope, *, guard: Any
    ) -> SessionEventEnvelope:
        from ksadk.events.session_event import validate_write_guard

        validate_write_guard(envelope, guard)
        if isinstance(guard, ActivationWriteGuard):
            async with self._connection() as connection:
                async with connection.transaction():
                    await self._assert_activation_fence(
                        connection, guard, envelope.session_id
                    )
                    return await self._log.append_on(connection, envelope, guard)
        return await self._log.append(envelope, guard=guard)

    async def _assert_activation_fence(
        self, connection: Any, guard: ActivationWriteGuard, session_id: str
    ) -> None:
        row = await connection.fetchrow(
            "SELECT activation_id, fencing_token, lease_expires_at, released"
            " FROM kernel_activations WHERE activation_id = $1 AND session_id = $2"
            " FOR SHARE",
            guard.activation_id,
            session_id,
        )
        if (
            row is None
            or row["released"]
            or row["lease_expires_at"] <= _now()
            or int(row["fencing_token"]) != int(guard.fencing_token)
        ):
            raise StaleFenceError(
                "activation lease does not match runtime event write guard",
                details={
                    "activation_id": guard.activation_id,
                    "expected_fence": int(guard.fencing_token),
                    "observed_activation_id": (
                        str(row["activation_id"]) if row is not None else None
                    ),
                    "observed_fence": (
                        int(row["fencing_token"]) if row is not None else None
                    ),
                    "released": bool(row["released"]) if row is not None else None,
                    "lease_expires_at": (
                        row["lease_expires_at"].isoformat() if row is not None else None
                    ),
                },
            )

    async def read(
        self, session_id: str, after_seq: int, limit: int
    ) -> list[SessionEventEnvelope]:
        return await self._log.read(session_id, after_seq, limit)

    async def subscribe(
        self, session_id: str, after_seq: int, *, poll_interval: float = 0.25
    ):
        import asyncio

        cursor = int(after_seq or 0)
        while True:
            envelopes = await self._log.read(session_id, cursor, 1000)
            for envelope in envelopes:
                cursor = max(cursor, int(envelope.seq))
                yield envelope
            await asyncio.sleep(poll_interval)


__all__ = [
    "PostgresAgentKernelStore",
    "PostgresFencedSessionEventStore",
    "PostgresKernelEventLog",
    "PostgresNonceStore",
    "SCHEMA_PATH",
    "NONCE_RETENTION_SECONDS",
]
