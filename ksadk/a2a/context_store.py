"""Durable owner-scoped mapping from A2A context IDs to Runtime sessions."""

from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class A2AContextIdentity:
    """Verified caller identity used as the context-mapping ownership boundary."""

    account_id: str
    tenant_id: str
    caller_principal_type: str
    caller_principal_id: str

    def canonical_owner(self) -> str:
        values = (
            self.account_id.strip(),
            self.tenant_id.strip(),
            self.caller_principal_type.strip(),
            self.caller_principal_id.strip(),
        )
        if not values[0] or not values[2] or not values[3]:
            raise ValueError(
                "A2A context identity requires verified account, type, and principal"
            )
        return "\x1f".join(values)


class A2AContextStore(ABC):
    """Maps a verified external context to an internal Runtime session ID."""

    @abstractmethod
    async def initialize(self) -> None:
        """Verify durable storage is ready before inbound A2A starts serving."""
        raise NotImplementedError

    @abstractmethod
    async def resolve_or_create(
        self,
        identity: A2AContextIdentity,
        external_context_id: str,
        *,
        isolation_scope: str | None = None,
    ) -> str:
        raise NotImplementedError

    @abstractmethod
    async def get(
        self,
        identity: A2AContextIdentity,
        external_context_id: str,
        *,
        isolation_scope: str | None = None,
    ) -> str | None:
        raise NotImplementedError


class SQLiteA2AContextStore(A2AContextStore):
    """Crash-safe local adapter for tests and single-replica Runtime deployments.

    Product deployments may supply a shared durable implementation of
    :class:`A2AContextStore`; callers do not depend on this SQLite schema.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._initialized = False
        self._init_lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True

    async def resolve_or_create(
        self,
        identity: A2AContextIdentity,
        external_context_id: str,
        *,
        isolation_scope: str | None = None,
    ) -> str:
        await self.initialize()
        key = self._mapping_key(identity, external_context_id, isolation_scope)
        return await asyncio.to_thread(self._resolve_or_create_sync, key)

    async def get(
        self,
        identity: A2AContextIdentity,
        external_context_id: str,
        *,
        isolation_scope: str | None = None,
    ) -> str | None:
        await self.initialize()
        key = self._mapping_key(identity, external_context_id, isolation_scope)
        return await asyncio.to_thread(self._get_sync, key)

    def _initialize_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path, timeout=5)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ksadk_a2a_context_mappings (
                    schema_version INTEGER NOT NULL,
                    owner_key_sha256 TEXT NOT NULL,
                    external_context_sha256 TEXT NOT NULL,
                    isolation_scope TEXT NOT NULL,
                    internal_session_id TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(owner_key_sha256, external_context_sha256, isolation_scope)
                )
                """
            )
            connection.commit()
        finally:
            connection.close()
        os.chmod(self._path, 0o600)

    @staticmethod
    def _mapping_key(
        identity: A2AContextIdentity,
        external_context_id: str,
        isolation_scope: str | None,
    ) -> tuple[str, str, str]:
        context = external_context_id.strip()
        if not context:
            raise ValueError("external_context_id must be non-empty")
        owner_hash = hashlib.sha256(identity.canonical_owner().encode("utf-8")).hexdigest()
        context_hash = hashlib.sha256(context.encode("utf-8")).hexdigest()
        return owner_hash, context_hash, str(isolation_scope or "")

    def _resolve_or_create_sync(self, key: tuple[str, str, str]) -> str:
        owner_hash, context_hash, scope = key
        connection = sqlite3.connect(self._path, timeout=5, isolation_level=None)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT internal_session_id
                FROM ksadk_a2a_context_mappings
                WHERE owner_key_sha256 = ? AND external_context_sha256 = ? AND isolation_scope = ?
                """,
                key,
            ).fetchone()
            if row is not None:
                connection.execute("COMMIT")
                return str(row[0])
            session_id = f"a2a-session-{uuid.uuid4().hex}"
            connection.execute(
                """
                INSERT INTO ksadk_a2a_context_mappings(
                    schema_version, owner_key_sha256, external_context_sha256,
                    isolation_scope, internal_session_id
                ) VALUES (1, ?, ?, ?, ?)
                """,
                (owner_hash, context_hash, scope, session_id),
            )
            connection.execute("COMMIT")
            return session_id
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _get_sync(self, key: tuple[str, str, str]) -> str | None:
        connection = sqlite3.connect(self._path, timeout=5)
        try:
            row = connection.execute(
                """
                SELECT internal_session_id
                FROM ksadk_a2a_context_mappings
                WHERE owner_key_sha256 = ? AND external_context_sha256 = ? AND isolation_scope = ?
                """,
                key,
            ).fetchone()
            return str(row[0]) if row is not None else None
        finally:
            connection.close()


__all__ = ["A2AContextIdentity", "A2AContextStore", "SQLiteA2AContextStore"]
