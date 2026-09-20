"""Durable, host-owned Teams context references beside the Kernel inbox.

Nothing in this registry grants authority by itself. Only an authenticated Host
loader may supply records, and the runtime checks the live grant on each use.
The PostgreSQL adapter shares the Kernel pool/schema/tenant; SQLite shares its
connection and writer lock. Replacing the database changes its incarnation.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ksadk.kernel.contracts import AgentControlCommand
from ksadk.kernel.execution_grants import ExecutionGrantSpec
from ksadk.kernel.store import command_digest
from ksadk.plugins.teams.cloud_contracts import Digest, Identifier, TeamsExecutionRef, digest


class ContextConflict(ValueError):
    """A durable reference was reused with different authority or payload."""


class StoreIdentityMismatch(ValueError):
    """The original durable inbox cannot be proved; never interpret as missing."""


class PreparedTeamsContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")

    context_ref: Identifier
    context_digest: Digest
    policy_ref: Identifier
    policy_digest: Digest
    owner_subject: Identifier
    ref: TeamsExecutionRef
    grant: ExecutionGrantSpec
    command: AgentControlCommand
    # JSON facts from the trusted loader, not executable tools or caller policy.
    context: dict[str, Any] = Field(default_factory=dict)
    material_manifest_ref: Identifier | None = None
    material_manifest_digest: Digest | None = None

    @model_validator(mode="after")
    def exact_scope(self):
        ref, command, grant = self.ref, self.command, self.grant
        if (self.material_manifest_ref is None) != (self.material_manifest_digest is None):
            raise ContextConflict("material manifest reference and digest are required together")
        if command.command_type != "enqueue" or any(
            (
                str(command.command_id) != ref.commandId,
                command.session_id != ref.sessionId,
                command.idempotency_key != ref.idempotencyKey,
                grant.agent_instance_id != command.agent_instance_id,
                grant.session_id != command.session_id,
                grant.tenant_id != command.tenant_id,
                grant.attempt_epoch != ref.attemptEpoch,
                command.payload.get("execution_grant_id") != grant.grant_id,
                command.payload.get("execution_grant_attempt_epoch") != ref.attemptEpoch,
                command.payload.get("execution_policy_ref") != self.policy_ref,
                command.payload.get("teams_context_ref") != self.context_ref,
            )
        ):
            raise ContextConflict("prepared command/context/grant scope mismatch")
        if ref.target.kind == "cloud_agent" and (
            ref.target.agentInstanceId != command.agent_instance_id
        ):
            raise ContextConflict("prepared target instance mismatch")
        if self.context_digest != digest(self.context):
            raise ContextConflict("prepared context digest mismatch")
        return self

    @property
    def payload_digest(self) -> str:
        return "sha256:" + command_digest(self.command)

    @property
    def session_owner_digest(self) -> str:
        return digest(
            {
                "tenant": self.grant.tenant_id,
                "owner": self.owner_subject,
                "authority": self.ref.authorityId,
                "group": self.ref.groupId,
                "teamRun": self.ref.teamRunId,
                "member": self.ref.runMemberId,
                "binding": self.ref.bindingRef,
                "instance": self.grant.agent_instance_id,
            }
        )


class TeamsExecutionContextRegistry(Protocol):
    """Shared adapters must persist in the *same* store as the original inbox."""

    durable: bool
    shared_across_hosts: bool
    kernel_store: Any

    async def initialize(self) -> str: ...
    async def incarnation(self) -> str: ...
    async def owns_session(self, session_id: str) -> bool: ...
    async def put(self, context: PreparedTeamsContext, *, expected_incarnation: str) -> None: ...
    async def get(
        self, context_ref: str, *, expected_incarnation: str
    ) -> PreparedTeamsContext | None: ...
    async def put_callback(self, context_ref, permit, *, expected_incarnation: str) -> None: ...
    async def get_callback(self, context_ref, revision, *, expected_incarnation: str): ...


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS teams_host_extensions (namespace TEXT NOT NULL, "
    "name TEXT NOT NULL, identity TEXT NOT NULL, PRIMARY KEY(namespace,name))",
    "CREATE TABLE IF NOT EXISTS teams_host_callback_permits ("
    "namespace TEXT NOT NULL, context_ref TEXT NOT NULL, grant_revision INTEGER NOT NULL, "
    "body TEXT NOT NULL, PRIMARY KEY(namespace, context_ref, grant_revision))",
    "CREATE TABLE IF NOT EXISTS teams_host_store_identity ("
    "namespace TEXT PRIMARY KEY, incarnation TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS teams_host_session_owners ("
    "namespace TEXT NOT NULL, session_id TEXT NOT NULL, owner_digest TEXT NOT NULL, "
    "PRIMARY KEY(namespace, session_id))",
    "CREATE TABLE IF NOT EXISTS teams_host_native_nonces ("
    "namespace TEXT NOT NULL, nonce TEXT NOT NULL, command_id TEXT NOT NULL, "
    "idempotency_key TEXT NOT NULL, PRIMARY KEY(namespace, nonce))",
    "CREATE TABLE IF NOT EXISTS teams_host_contexts ("
    "namespace TEXT NOT NULL, context_ref TEXT NOT NULL, session_id TEXT NOT NULL, "
    "command_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, policy_ref TEXT NOT NULL, "
    "body TEXT NOT NULL, PRIMARY KEY(namespace, context_ref), "
    "UNIQUE(namespace, session_id, command_id), "
    "UNIQUE(namespace, session_id, idempotency_key), UNIQUE(namespace, policy_ref))",
)


class _Registry:
    durable = True
    shared_across_hosts = False

    async def initialize(self) -> str:
        async with self._transaction() as db:
            # Fail if pointed at a standalone/new uninitialized metadata DB.
            await db.execute("SELECT 1 FROM kernel_inbox LIMIT 1")
            for statement in _SCHEMA:
                await db.execute(statement)
            await db.execute(
                "INSERT INTO teams_host_store_identity(namespace, incarnation) VALUES (?, ?) "
                "ON CONFLICT(namespace) DO NOTHING",
                (self.namespace, str(uuid4())),
            )
            return await self._identity(db)

    async def _identity(self, db, expected=None):
        row = await db.one(
            "SELECT incarnation FROM teams_host_store_identity WHERE namespace=?",
            (self.namespace,),
        )
        if row is None or (expected is not None and row["incarnation"] != expected):
            raise StoreIdentityMismatch("original Kernel store identity mismatch")
        return str(row["incarnation"])

    async def incarnation(self) -> str:
        async with self._transaction() as db:
            return await self._identity(db)

    async def extension_identity(self, name, *, expected_incarnation):
        async with self._transaction() as db:
            await self._identity(db, expected_incarnation)
            row = await db.one(
                "SELECT identity FROM teams_host_extensions WHERE namespace=? AND name=?",
                (self.namespace, name),
            )
            return row["identity"] if row else None

    async def bind_extension_identity(self, name, identity, *, expected_incarnation):
        async with self._transaction() as db:
            await self._identity(db, expected_incarnation)
            await db.execute(
                "INSERT INTO teams_host_extensions(namespace,name,identity) VALUES(?,?,?) "
                "ON CONFLICT DO NOTHING",
                (self.namespace, name, identity),
            )
            row = await db.one(
                "SELECT identity FROM teams_host_extensions WHERE namespace=? AND name=?",
                (self.namespace, name),
            )
            if row is None or row["identity"] != identity:
                raise StoreIdentityMismatch("original Host extension identity changed")

    async def register(self, nonce: str, command_id: str, idempotency_key: str) -> bool:
        """Durable native-permit nonce storage in the original Kernel database."""
        async with self._transaction() as db:
            await self._identity(db)
            await db.execute(
                "INSERT INTO teams_host_native_nonces(namespace,nonce,command_id,idempotency_key) "
                "VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
                (self.namespace, nonce, command_id, idempotency_key),
            )
            row = await db.one(
                "SELECT command_id,idempotency_key FROM teams_host_native_nonces "
                "WHERE namespace=? AND nonce=?",
                (self.namespace, nonce),
            )
            return row is not None and (row["command_id"], row["idempotency_key"]) == (
                command_id,
                idempotency_key,
            )

    async def owns_session(self, session_id: str) -> bool:
        """Ingress fence: reserved Teams sessions cannot bypass the Teams gate."""
        async with self._transaction() as db:
            await self._identity(db)
            row = await db.one(
                "SELECT 1 FROM teams_host_session_owners WHERE namespace=? AND session_id=?",
                (self.namespace, session_id),
            )
            return row is not None

    async def put(self, context: PreparedTeamsContext, *, expected_incarnation: str) -> None:
        # Revalidate and serialize immediately: nested dicts in a frozen model
        # are not immutable and must not be retained as an authority cache.
        context = PreparedTeamsContext.model_validate_json(context.model_dump_json())
        body = json.dumps(context.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        async with self._transaction() as db:
            await self._identity(db, expected_incarnation)
            await db.execute(
                "INSERT INTO teams_host_session_owners(namespace,session_id,owner_digest) "
                "VALUES (?,?,?) ON CONFLICT(namespace,session_id) DO NOTHING",
                (self.namespace, context.ref.sessionId, context.session_owner_digest),
            )
            owner = await db.one(
                "SELECT owner_digest FROM teams_host_session_owners "
                "WHERE namespace=? AND session_id=?",
                (self.namespace, context.ref.sessionId),
            )
            if owner["owner_digest"] != context.session_owner_digest:
                raise ContextConflict("session is reserved for another trusted scope")
            await db.execute(
                "INSERT INTO teams_host_contexts(namespace,context_ref,session_id,command_id,"
                "idempotency_key,policy_ref,body) VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (
                    self.namespace,
                    context.context_ref,
                    context.ref.sessionId,
                    context.ref.commandId,
                    context.ref.idempotencyKey,
                    context.policy_ref,
                    body,
                ),
            )
            row = await db.one(
                "SELECT body FROM teams_host_contexts WHERE namespace=? AND context_ref=?",
                (self.namespace, context.context_ref),
            )
            if row is None or row["body"] != body:
                raise ContextConflict("prepared execution reference conflicts with durable context")

    async def get(
        self, context_ref: str, *, expected_incarnation: str
    ) -> PreparedTeamsContext | None:
        async with self._transaction() as db:
            await self._identity(db, expected_incarnation)
            row = await db.one(
                "SELECT body FROM teams_host_contexts WHERE namespace=? AND context_ref=?",
                (self.namespace, context_ref),
            )
            return PreparedTeamsContext.model_validate_json(row["body"]) if row else None

    async def put_callback(self, context_ref, permit, *, expected_incarnation):
        """Persist an independently verified, short callback transport credential.

        This never changes the immutable context or establishes a grant clock.
        The composition root verifies all claims before writing and every use.
        Separate revision slots prevent a delayed old response replacing the
        active revision's callback authority.
        """
        from ksadk.plugins.teams.cloud_permits import TeamsCallbackPermit, _time

        permit = TeamsCallbackPermit.model_validate(permit)
        async with self._transaction() as db:
            await self._identity(db, expected_incarnation)
            context = await db.one(
                "SELECT 1 FROM teams_host_contexts WHERE namespace=? AND context_ref=?",
                (self.namespace, context_ref),
            )
            if context is None:
                raise ContextConflict("callback requires an original prepared context")
            args = (self.namespace, context_ref, permit.grantRevision)
            old = await db.one(
                "SELECT body FROM teams_host_callback_permits "
                "WHERE namespace=? AND context_ref=? AND grant_revision=?",
                args,
            )
            if old and _time(
                TeamsCallbackPermit.model_validate_json(old["body"]).expiresAt
            ) >= _time(permit.expiresAt):
                return
            await db.execute(
                "INSERT INTO teams_host_callback_permits"
                "(namespace,context_ref,grant_revision,body) "
                "VALUES(?,?,?,?) ON CONFLICT(namespace,context_ref,grant_revision) "
                "DO UPDATE SET body=excluded.body",
                args + (permit.model_dump_json(),),
            )

    async def get_callback(self, context_ref, revision, *, expected_incarnation):
        from ksadk.plugins.teams.cloud_permits import TeamsCallbackPermit

        async with self._transaction() as db:
            await self._identity(db, expected_incarnation)
            row = await db.one(
                "SELECT body FROM teams_host_callback_permits "
                "WHERE namespace=? AND context_ref=? AND grant_revision=?",
                (self.namespace, context_ref, revision),
            )
            return TeamsCallbackPermit.model_validate_json(row["body"]) if row else None


class _SQLiteConnection:
    def __init__(self, connection):
        self.connection = connection

    async def execute(self, sql, args=()):
        cursor = await self.connection.execute(sql, args)
        await cursor.close()

    async def one(self, sql, args=()):
        cursor = await self.connection.execute(sql, args)
        try:
            return await cursor.fetchone()
        finally:
            await cursor.close()


class SQLiteTeamsExecutionContextRegistry(_Registry):
    """Single-host adapter; shares the Kernel connection and write transaction."""

    def __init__(self, kernel_store):
        from ksadk.kernel.sqlite_store import SQLiteAgentKernelStore

        if not isinstance(kernel_store, SQLiteAgentKernelStore):
            raise TypeError("a SQLite Kernel store is required")
        self.kernel_store = kernel_store
        self.namespace = "default"
        self._inode = None

    @asynccontextmanager
    async def _transaction(self):
        store = self.kernel_store
        async with store._write_lock:
            connection = await store._connect()
            info = store.db_path.stat()
            identity = (info.st_dev, info.st_ino)
            if self._inode is not None and self._inode != identity:
                raise StoreIdentityMismatch("Kernel database file was replaced")
            self._inode = identity
            await connection.execute("BEGIN IMMEDIATE")
            try:
                yield _SQLiteConnection(connection)
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise


class _PostgresConnection:
    def __init__(self, connection):
        self.connection = connection

    @staticmethod
    def sql(sql):
        parts = sql.split("?")
        return "".join(
            part + (f"${i + 1}" if i < len(parts) - 1 else "") for i, part in enumerate(parts)
        )

    async def execute(self, sql, args=()):
        return await self.connection.execute(self.sql(sql), *args)

    async def one(self, sql, args=()):
        return await self.connection.fetchrow(self.sql(sql), *args)


class PostgresTeamsExecutionContextRegistry(_Registry):
    """Shared-host adapter using the actual Kernel asyncpg pool and tenant."""

    shared_across_hosts = True

    def __init__(self, kernel_store):
        from ksadk.kernel.postgres_store import PostgresAgentKernelStore

        if not isinstance(kernel_store, PostgresAgentKernelStore):
            raise TypeError("a PostgreSQL Kernel store is required")
        self.kernel_store = kernel_store
        self.namespace = kernel_store.tenant_id

    @asynccontextmanager
    async def _transaction(self):
        async with self.kernel_store._connection() as connection:
            async with connection.transaction():
                # Serialize initial DDL and owner/context insert across pods.
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    "teams-host-registry:" + self.namespace,
                )
                yield _PostgresConnection(connection)
