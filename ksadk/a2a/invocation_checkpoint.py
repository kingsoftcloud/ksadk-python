"""Private caller checkpoints, separate from the public canonical event log."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator
from sqlalchemy import text

from ksadk.a2a.invocation_types import A2AInvocationHandle, A2AInvocationRequest


class A2AInvocationCheckpoint(BaseModel):
    """Private recovery input and protocol state; never a public Task payload.

    ``state`` is a versioned adapter/service checkpoint, not DI objects, permits
    or transport credentials. Canonical events remain in their existing store.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    version: Literal[1] = 1
    request: A2AInvocationRequest
    handle: A2AInvocationHandle
    send_entered: bool = False
    state: dict[str, JsonValue] = Field(default_factory=dict, repr=False)

    @model_validator(mode="after")
    def identity_matches(self):
        excluded = {"platform_task_id", "remote_task_id", "remote_context_id"}
        if self.request.handle.model_dump(exclude=excluded) != self.handle.model_dump(
            exclude=excluded
        ):
            raise ValueError("A2A_CHECKPOINT_IDENTITY_CONFLICT")
        return self


class InvocationCheckpointBusy(RuntimeError):
    pass


class _CheckpointLease:
    def __init__(self, connection, key, scope_id):
        self._connection, self._key, self._scope_id = connection, key, scope_id
        self._active = True

    def _require_active(self):
        if not self._active or self._connection.closed or self._connection.invalidated:
            raise RuntimeError("A2A_CHECKPOINT_LEASE_CLOSED")

    async def load(self) -> A2AInvocationCheckpoint | None:
        self._require_active()
        result = await self._connection.execute(
            text(
                "SELECT checkpoint_json FROM ksadk_a2a_invocation_checkpoints "
                "WHERE owner_scope_key=:key"
            ),
            {"key": self._key},
        )
        payload = result.scalar_one_or_none()
        await self._connection.commit()
        if payload is None:
            return None
        checkpoint = A2AInvocationCheckpoint.model_validate_json(payload)
        if checkpoint.handle.scope_id != self._scope_id:
            raise ValueError("A2A_CHECKPOINT_IDENTITY_CONFLICT")
        return checkpoint

    async def cancel_requested(self) -> bool:
        self._require_active()
        result = await self._connection.scalar(
            text(
                "SELECT cancel_requested FROM ksadk_a2a_invocation_checkpoints "
                "WHERE owner_scope_key=:key"
            ),
            {"key": self._key},
        )
        await self._connection.commit()
        return bool(result)

    async def clear_cancel_request(self):
        self._require_active()
        await self._connection.execute(
            text(
                "UPDATE ksadk_a2a_invocation_checkpoints SET cancel_requested=FALSE "
                "WHERE owner_scope_key=:key"
            ),
            {"key": self._key},
        )
        await self._connection.commit()

    async def save(self, checkpoint: A2AInvocationCheckpoint) -> None:
        self._require_active()
        # Revalidate even a model_copy from a caller. JSON encoding disallows
        # nonfinite numbers and arbitrary Python object serialization.
        checkpoint = A2AInvocationCheckpoint.model_validate(checkpoint.model_dump())
        if checkpoint.handle.scope_id != self._scope_id:
            raise ValueError("A2A_CHECKPOINT_IDENTITY_CONFLICT")
        previous = await self.load()
        if previous is not None:
            if previous.request != checkpoint.request:
                raise ValueError("A2A_CHECKPOINT_REQUEST_CONFLICT")
            if previous.send_entered and not checkpoint.send_entered:
                raise ValueError("A2A_CHECKPOINT_SEND_REGRESSION")
            for locator in ("platform_task_id", "remote_task_id", "remote_context_id"):
                before = getattr(previous.handle, locator)
                if before is not None and before != getattr(checkpoint.handle, locator):
                    raise ValueError("A2A_CHECKPOINT_BINDING_CONFLICT")
        payload = json.dumps(
            checkpoint.model_dump(mode="json"), allow_nan=False, separators=(",", ":")
        )
        if len(payload.encode()) > 16 * 1024 * 1024:
            raise ValueError("A2A_CHECKPOINT_TOO_LARGE")
        await self._connection.execute(
            text(
                "INSERT INTO ksadk_a2a_invocation_checkpoints(owner_scope_key, checkpoint_json) "
                "VALUES (:key, :payload) ON CONFLICT(owner_scope_key) DO UPDATE SET "
                "checkpoint_json=EXCLUDED.checkpoint_json"
            ),
            {"key": self._key, "payload": payload},
        )
        # Commit each send receipt before the wire side effect. An invocation
        # crash must not roll back its already-recorded possible-send fact.
        await self._connection.commit()


class PostgresA2AInvocationCheckpointStore:
    """One owner-scoped, process-safe invocation lease with durable receipts.

    PostgreSQL session advisory locks survive receipt commits and are released
    when a crashed worker's connection closes. Ordinary competing calls fail
    promptly; recovery may wait within the saved request deadline. No timer
    grants ownership or forcibly releases another connection's lock.
    """

    def __init__(
        self, engine, *, account_id: str, tenant_id: str, runtime_id: str, lease_engine=None
    ):
        if not all(isinstance(v, str) and v.strip() for v in (account_id, tenant_id, runtime_id)):
            raise ValueError("A2A_CHECKPOINT_OWNER_REQUIRED")
        self._engine = engine
        self._lease_engine = lease_engine or engine
        self._owner = (account_id, tenant_id, runtime_id)

    async def initialize(self):
        async with self._engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS ksadk_a2a_invocation_checkpoints ("
                    "owner_scope_key TEXT PRIMARY KEY, checkpoint_json TEXT NOT NULL)"
                )
            )
            await connection.execute(
                text(
                    "ALTER TABLE ksadk_a2a_invocation_checkpoints ADD COLUMN IF NOT EXISTS "
                    "cancel_requested BOOLEAN NOT NULL DEFAULT FALSE"
                )
            )

    def _key(self, scope_id):
        return hashlib.sha256(
            json.dumps([*self._owner, scope_id], separators=(",", ":")).encode()
        ).hexdigest()

    async def read(self, scope_id):
        """Read one owner-scoped locator without acquiring its mutation lease."""
        async with self._engine.connect() as connection:
            payload = await connection.scalar(
                text("SELECT checkpoint_json FROM ksadk_a2a_invocation_checkpoints "
                     "WHERE owner_scope_key=:key"), {"key": self._key(scope_id)},
            )
        if payload is None:
            return None
        saved = A2AInvocationCheckpoint.model_validate_json(payload)
        if saved.handle.scope_id != scope_id:
            raise ValueError("A2A_CHECKPOINT_IDENTITY_CONFLICT")
        return saved

    async def request_cancel(self, handle):
        """Record intent without stealing the active invocation's lease."""
        key = self._key(handle.scope_id)
        async with self._engine.begin() as connection:
            payload = await connection.scalar(
                text(
                    "SELECT checkpoint_json FROM ksadk_a2a_invocation_checkpoints "
                    "WHERE owner_scope_key=:key FOR UPDATE"
                ),
                {"key": key},
            )
            if payload is None:
                raise KeyError("A2A_INVOCATION_NOT_FOUND")
            saved = A2AInvocationCheckpoint.model_validate_json(payload)
            excluded = {"platform_task_id", "remote_task_id", "remote_context_id"}
            if saved.handle.model_dump(exclude=excluded) != handle.model_dump(exclude=excluded):
                raise ValueError("A2A_HANDLE_CONFLICT")
            for locator in excluded:
                supplied = getattr(handle, locator)
                if supplied is not None and supplied != getattr(saved.handle, locator):
                    raise ValueError("A2A_HANDLE_CONFLICT")
            await connection.execute(
                text(
                    "UPDATE ksadk_a2a_invocation_checkpoints SET cancel_requested=TRUE "
                    "WHERE owner_scope_key=:key"
                ),
                {"key": key},
            )
            return saved

    @asynccontextmanager
    async def lease(self, scope_id: str, *, wait_for_recovery: bool = False):
        if not isinstance(scope_id, str) or not scope_id.strip():
            raise ValueError("A2A_CHECKPOINT_SCOPE_REQUIRED")
        key = self._key(scope_id)
        lock_id = int.from_bytes(bytes.fromhex(key)[:8], "big", signed=True)
        async with self._lease_engine.connect() as connection:
            wait_deadline = None
            while True:
                if (wait_deadline is not None
                        and asyncio.get_running_loop().time() >= wait_deadline):
                    raise TimeoutError("A2A_CHECKPOINT_RECOVERY_TIMEOUT")
                try:
                    acquired = await connection.scalar(
                        text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_id}
                    )
                    await connection.commit()
                except BaseException:
                    # Acquisition may have succeeded on the server even when its
                    # response was lost. Never return that session to the pool.
                    await connection.invalidate()
                    raise
                if acquired:
                    break
                if not wait_for_recovery:
                    raise InvocationCheckpointBusy("A2A_INVOCATION_BUSY")
                if wait_deadline is None:
                    # This unlocked read supplies only a waiting budget. No
                    # recovery state or permission to send is taken from it.
                    payload = await connection.scalar(
                        text("SELECT checkpoint_json FROM ksadk_a2a_invocation_checkpoints "
                             "WHERE owner_scope_key=:key"), {"key": key},
                    )
                    await connection.commit()
                    if payload is None:
                        raise InvocationCheckpointBusy("A2A_INVOCATION_BUSY")
                    saved = A2AInvocationCheckpoint.model_validate_json(payload)
                    if saved.handle.scope_id != scope_id:
                        raise ValueError("A2A_CHECKPOINT_IDENTITY_CONFLICT")
                    remaining = saved.request.deadline_unix_seconds - time.time()
                    wait_deadline = asyncio.get_running_loop().time() + remaining
                remaining = wait_deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("A2A_CHECKPOINT_RECOVERY_TIMEOUT")
                await asyncio.sleep(min(0.1, remaining))
            lease = _CheckpointLease(connection, key, scope_id)
            try:
                yield lease
            finally:
                lease._active = False
                # A failed statement may leave a transaction aborted. Roll it
                # back before releasing the session lock into the pool.
                try:
                    if not connection.closed and not connection.invalidated:
                        await connection.rollback()
                        await connection.execute(
                            text("SELECT pg_advisory_unlock(:key)"), {"key": lock_id}
                        )
                        await connection.commit()
                except BaseException:
                    await connection.invalidate()
                    raise
