"""Durable schema-v2 RuntimeEvent storage on the existing session event log.

The canonical event envelope intentionally has no ``session_id``.  Session
scope is therefore an explicit store argument and never hidden in source
metadata.  The physical ``SessionEvent.id`` is a deterministic encoding of
``(session_id, event_id)`` so the existing durable primary-key constraint can
enforce the canonical idempotency domain before a session cursor is allocated.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Iterable
from typing import Any

from ksadk.events.canonical import RuntimeEvent, dump_runtime_event, parse_runtime_event
from ksadk.sessions.base import SessionEvent, SessionEventSeqBinding

_CANONICAL_RUNTIME_MARKER = "ksadk_canonical_runtime_event"
_CANONICAL_CONTENT_KEY = "runtime_event"
_TERMINAL_EVENT_TYPES = frozenset({"run.completed", "run.failed", "run.canceled"})

_REQUIRED_SEQ_BINDING: SessionEventSeqBinding = "runtime_event.seq"


def canonical_storage_id(session_id: str, event_id: str) -> str:
    """Return a stable physical id distinct from the producer event id."""

    if not session_id.strip() or not event_id.strip():
        raise ValueError("session_id and event_id must be nonempty")
    encoded = json.dumps([session_id, event_id], ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return f"cev_{hashlib.sha256(encoded).hexdigest()[:40]}"


def runtime_event_to_session_event(session_id: str, event: RuntimeEvent) -> SessionEvent:
    """Pack one canonical fact into the existing free-form SessionEvent carrier."""

    if getattr(event, "schema_version", None) != 2:
        raise ValueError("canonical RuntimeEventStore accepts schema_version=2 only")
    payload = dump_runtime_event(event)
    return SessionEvent(
        id=canonical_storage_id(session_id, event.event_id),
        session_id=session_id,
        author=event.source.framework,
        event_type=event.event_type,
        content={_CANONICAL_CONTENT_KEY: payload},
        timestamp=event.timestamp,
        invocation_id=event.run_id,
        metadata={
            _CANONICAL_RUNTIME_MARKER: True,
            "schema_version": 2,
            "canonical_event_id": event.event_id,
        },
        seq_binding=_REQUIRED_SEQ_BINDING,
        seq_id=int(event.seq),
    )


def session_event_to_runtime_event(event: SessionEvent) -> RuntimeEvent | None:
    """Restore a canonical fact, using the physical session cursor as ``seq``."""

    metadata = event.metadata or {}
    if not metadata.get(_CANONICAL_RUNTIME_MARKER):
        return None
    if metadata.get("schema_version") != 2:
        raise ValueError("canonical SessionEvent marker requires schema_version=2")
    stored_payload = (event.content or {}).get(_CANONICAL_CONTENT_KEY)
    if not isinstance(stored_payload, dict):
        raise ValueError("canonical SessionEvent is missing runtime_event content")
    if stored_payload.get("seq") != event.seq_id:
        raise ValueError("canonical RuntimeEvent seq does not match physical seq")
    payload = dict(stored_payload)
    restored = parse_runtime_event(payload)
    canonical_event_id = str(metadata.get("canonical_event_id") or "")
    if canonical_event_id != restored.event_id:
        raise ValueError("canonical SessionEvent event id metadata does not match content")
    expected_storage_id = canonical_storage_id(event.session_id, restored.event_id)
    if event.id != expected_storage_id:
        raise ValueError("canonical SessionEvent storage id does not match session event identity")
    if event.invocation_id != restored.run_id or event.event_type != restored.event_type:
        raise ValueError("canonical SessionEvent envelope does not match runtime event content")
    return restored


class RuntimeEventStore:
    """Schema-v2-only canonical store with durable session-scoped idempotency."""

    def __init__(self, session_service: Any) -> None:
        self._service = session_service

    @property
    def session_service(self) -> Any:
        return self._service

    async def append(self, session_id: str, events: Iterable[RuntimeEvent]) -> list[RuntimeEvent]:
        return [await self.append_one(session_id, event) for event in events]

    async def append_one(self, session_id: str, event: RuntimeEvent) -> RuntimeEvent:
        persisted, _created = await self.persist_one(session_id, event)
        return persisted

    async def persist_one(self, session_id: str, event: RuntimeEvent) -> tuple[RuntimeEvent, bool]:
        """Persist before publication and return whether this call created the fact."""

        if getattr(event, "schema_version", None) != 2:
            raise ValueError("canonical RuntimeEventStore accepts schema_version=2 only")
        if not session_id.strip():
            raise ValueError("session_id must be nonempty")
        self._require_storage_capabilities()
        existing = await self.event_by_id(session_id, event.event_id)
        if existing is not None:
            self._assert_same_fact(existing, event)
            return existing, False
        packed = runtime_event_to_session_event(session_id, event)
        try:
            stored = await self._service.append_event(session_id, packed)
        except Exception:
            # The deterministic physical id turns concurrent appends into an
            # insert-winner/insert-loser race on durable backends.  Re-read the
            # winner and only absorb the error when it is the same fact.
            existing = await self.event_by_id(session_id, event.event_id)
            if existing is None:
                raise
            self._assert_same_fact(existing, event)
            return existing, False
        persisted = session_event_to_runtime_event(stored)
        if persisted is None:  # pragma: no cover - packed by this module
            raise RuntimeError("canonical RuntimeEvent lost its storage marker")
        self._assert_same_fact(persisted, event)
        return persisted, True

    async def event_by_id(self, session_id: str, event_id: str) -> RuntimeEvent | None:
        self._require_storage_capabilities()
        storage_id = canonical_storage_id(session_id, event_id)
        stored = await self._service.get_event_by_id(session_id, storage_id)
        return session_event_to_runtime_event(stored) if stored is not None else None

    async def resolve_existing(
        self, session_id: str, candidate: RuntimeEvent
    ) -> RuntimeEvent | None:
        """Return an identical durable fact or raise for an id collision."""

        existing = await self.event_by_id(session_id, candidate.event_id)
        if existing is not None:
            self._assert_same_fact(existing, candidate)
        return existing

    async def list(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        before_seq: int | None = None,
        run_id: str | None = None,
        limit: int | None = None,
    ) -> list[RuntimeEvent]:
        # Run replay uses the backend's invocation index; session replay still
        # reads the shared physical cursor log and filters legacy rows here.
        if run_id is None:
            raw = await self._service.get_events(
                session_id,
                after_seq_id=after_seq,
                before_seq_id=before_seq,
            )
        else:
            self._require_storage_capabilities()
            raw = await self._service.get_events_by_invocation_id(
                session_id,
                run_id,
                after_seq_id=after_seq,
                before_seq_id=before_seq,
            )
        events = [
            canonical
            for canonical in (session_event_to_runtime_event(item) for item in raw)
            if canonical is not None and (run_id is None or canonical.run_id == run_id)
        ]
        events.sort(key=lambda event: event.seq)
        if limit is not None:
            if limit < 1:
                raise ValueError("limit must be positive")
            events = events[-limit:]
        return events

    async def subscribe_session(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        poll_interval: float = 0.25,
        timeout: float = 5 * 60,
    ) -> AsyncIterator[RuntimeEvent]:
        cursor = int(after_seq or 0)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            rows = await self._service.get_events(session_id, after_seq_id=cursor)
            rows.sort(key=lambda event: event.seq_id)
            for row in rows:
                event = session_event_to_runtime_event(row)
                cursor = row.seq_id
                if event is not None:
                    yield event
            if asyncio.get_running_loop().time() >= deadline:
                return
            await asyncio.sleep(poll_interval)

    async def subscribe_run(
        self,
        session_id: str,
        run_id: str,
        *,
        after_seq: int = 0,
        poll_interval: float = 0.25,
        timeout: float = 5 * 60,
    ) -> AsyncIterator[RuntimeEvent]:
        cursor = int(after_seq or 0)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            rows = await self._service.get_events(session_id, after_seq_id=cursor)
            rows.sort(key=lambda event: event.seq_id)
            for row in rows:
                event = session_event_to_runtime_event(row)
                cursor = row.seq_id
                if event is not None and event.run_id == run_id:
                    yield event
                    if event.event_type in _TERMINAL_EVENT_TYPES:
                        return
            if asyncio.get_running_loop().time() >= deadline:
                return
            await asyncio.sleep(poll_interval)

    @staticmethod
    def _assert_same_fact(existing: RuntimeEvent, candidate: RuntimeEvent) -> None:
        existing_payload = dump_runtime_event(existing)
        candidate_payload = dump_runtime_event(candidate)
        # ``seq`` is the store-assigned delivery cursor, not producer fact
        # identity.  Every other canonical field participates in collision
        # validation, including timestamp, source, run_seq and typed content.
        existing_payload.pop("seq", None)
        candidate_payload.pop("seq", None)
        if existing_payload != candidate_payload:
            raise ValueError(f"RuntimeEvent id collision for {candidate.event_id!r}")

    def _require_storage_capabilities(self) -> None:
        capabilities = self._service.storage_capabilities
        if (
            _REQUIRED_SEQ_BINDING not in capabilities.atomic_seq_bindings
            or not capabilities.indexed_event_lookup
            or not capabilities.indexed_invocation_lookup
        ):
            raise RuntimeError(
                "session backend must support atomic runtime_event.seq binding "
                "and indexed physical event lookup and indexed invocation lookup"
            )


__all__ = [
    "RuntimeEventStore",
    "canonical_storage_id",
    "runtime_event_to_session_event",
    "session_event_to_runtime_event",
]
