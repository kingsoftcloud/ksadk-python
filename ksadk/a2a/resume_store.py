"""Runtime-local storage for opaque A2A input-required recovery state."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

from ksadk.runtime.adapter import ResumePayload, ResumeTarget, RunHandle

A2AResumePayloadKind: TypeAlias = Literal[
    "tool_result",
    "approval_decision",
    "hitl_answer",
    "free_text",
]


@dataclass(frozen=True)
class A2AResumeState:
    """Private Runtime state needed to resume one protocol Task."""

    handle: RunHandle
    target: ResumeTarget
    payload_kind: A2AResumePayloadKind
    call_id: str | None = None


class A2AResumeStateStore(ABC):
    """Persists recovery state outside public A2A Task metadata."""

    @abstractmethod
    async def initialize(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def put(self, key: str, state: A2AResumeState) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get(self, key: str) -> A2AResumeState | None:
        raise NotImplementedError

    @abstractmethod
    async def delete(self, key: str) -> None:
        raise NotImplementedError


class InMemoryA2AResumeStateStore(A2AResumeStateStore):
    """Local-development store; it intentionally does not survive a restart."""

    def __init__(self) -> None:
        self._states: dict[str, A2AResumeState] = {}

    async def initialize(self) -> None:
        return None

    async def put(self, key: str, state: A2AResumeState) -> None:
        self._states[key] = state

    async def get(self, key: str) -> A2AResumeState | None:
        return self._states.get(key)

    async def delete(self, key: str) -> None:
        self._states.pop(key, None)


class SQLiteA2AResumeStateStore(A2AResumeStateStore):
    """Crash-safe Runtime-local store for single-replica/persistent-volume deployments."""

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

    async def put(self, key: str, state: A2AResumeState) -> None:
        await self.initialize()
        payload = json.dumps(
            {
                "handle": state.handle.model_dump(mode="json"),
                "target": state.target.model_dump(mode="json"),
                "payload_kind": state.payload_kind,
                "call_id": state.call_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        await asyncio.to_thread(self._put_sync, key, payload)

    async def get(self, key: str) -> A2AResumeState | None:
        await self.initialize()
        payload = await asyncio.to_thread(self._get_sync, key)
        if payload is None:
            return None
        try:
            raw = json.loads(payload)
            return A2AResumeState(
                handle=RunHandle.model_validate(raw["handle"]),
                target=ResumeTarget.model_validate(raw["target"]),
                payload_kind=ResumePayload.model_validate(
                    {"kind": raw["payload_kind"]}
                ).kind,
                call_id=str(raw["call_id"]) if raw.get("call_id") is not None else None,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("A2A resume state is invalid") from exc

    async def delete(self, key: str) -> None:
        await self.initialize()
        await asyncio.to_thread(self._delete_sync, key)

    def _initialize_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path, timeout=5)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ksadk_a2a_resume_states (
                    resume_key_sha256 TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.commit()
        finally:
            connection.close()
        os.chmod(self._path, 0o600)

    @staticmethod
    def _hash(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def _put_sync(self, key: str, payload: str) -> None:
        connection = sqlite3.connect(self._path, timeout=5)
        try:
            connection.execute(
                """
                INSERT INTO ksadk_a2a_resume_states(resume_key_sha256, state_json)
                VALUES (?, ?)
                ON CONFLICT(resume_key_sha256) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (self._hash(key), payload),
            )
            connection.commit()
        finally:
            connection.close()

    def _get_sync(self, key: str) -> str | None:
        connection = sqlite3.connect(self._path, timeout=5)
        try:
            row = connection.execute(
                "SELECT state_json FROM ksadk_a2a_resume_states WHERE resume_key_sha256 = ?",
                (self._hash(key),),
            ).fetchone()
            return str(row[0]) if row is not None else None
        finally:
            connection.close()

    def _delete_sync(self, key: str) -> None:
        connection = sqlite3.connect(self._path, timeout=5)
        try:
            connection.execute(
                "DELETE FROM ksadk_a2a_resume_states WHERE resume_key_sha256 = ?",
                (self._hash(key),),
            )
            connection.commit()
        finally:
            connection.close()


__all__ = [
    "A2AResumeState",
    "A2AResumePayloadKind",
    "A2AResumeStateStore",
    "InMemoryA2AResumeStateStore",
    "SQLiteA2AResumeStateStore",
]
