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
import uuid
from collections.abc import AsyncIterator, Iterable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ksadk.events.canonical import RuntimeEvent, dump_runtime_event, parse_runtime_event
from ksadk.kernel.contracts import ActivationWriteGuard, SessionEventEnvelope
from ksadk.sessions.base import SessionEvent, SessionEventSeqBinding

if TYPE_CHECKING:
    from ksadk.events.session_event import SessionEventStore

_CANONICAL_RUNTIME_MARKER = "ksadk_canonical_runtime_event"
_CANONICAL_CONTENT_KEY = "runtime_event"
_ENVELOPE_MARKER = "ksadk_session_event_envelope"
_TERMINAL_EVENT_TYPES = frozenset({"run.completed", "run.failed", "run.canceled"})

_REQUIRED_SEQ_BINDING: SessionEventSeqBinding = "runtime_event.seq"

# Stable namespace for mapping free-form RuntimeEvent event ids onto the
# UUID-typed ``SessionEventEnvelope.event_id`` contract.
_RUNTIME_EVENT_UUID_NAMESPACE = uuid.UUID("6e9f0c5a-2f4d-4d8a-9b31-1c2a5f7e9b41")


def runtime_event_envelope_id(session_id: str, event_id: str) -> uuid.UUID:
    """UUID contract id for one runtime fact (deterministic per session)."""

    try:
        return uuid.UUID(str(event_id))
    except (ValueError, AttributeError, TypeError):
        return uuid.uuid5(_RUNTIME_EVENT_UUID_NAMESPACE, f"{session_id}|{event_id}")


def runtime_event_envelope(session_id: str, event: RuntimeEvent) -> SessionEventEnvelope:
    """Lift one canonical RuntimeEvent fact into a family=runtime/v2 envelope."""

    if getattr(event, "schema_version", None) != 2:
        raise ValueError("canonical RuntimeEventStore accepts schema_version=2 only")
    timestamp = datetime.fromtimestamp(event.timestamp, tz=timezone.utc).isoformat()
    return SessionEventEnvelope(
        event_id=runtime_event_envelope_id(session_id, event.event_id),
        session_id=session_id,
        seq=0,  # overwritten with the store-allocated cursor on persistence
        timestamp=timestamp,
        family="runtime",
        family_version=2,
        event_type=event.event_type,
        payload=dump_runtime_event(event),
        run_id=event.run_id,
        actor_ref=event.source.framework,
    )


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
    if metadata.get(_ENVELOPE_MARKER) and metadata.get("family") == "runtime":
        # Task 2 typed view rows: family=runtime/v2 written through the
        # generic SessionEventStore envelope carrier.
        payload = (event.content or {}).get(_CANONICAL_CONTENT_KEY)
        if not isinstance(payload, dict):
            raise ValueError("runtime family SessionEvent is missing runtime_event content")
        if payload.get("seq") != event.seq_id:
            raise ValueError("canonical RuntimeEvent seq does not match physical seq")
        return parse_runtime_event(payload)
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


def _is_session_event_store(candidate: Any) -> bool:
    """Duck-type the generic SessionEventStore port without an import cycle."""

    return all(
        callable(getattr(candidate, name, None)) for name in ("append", "read", "subscribe")
    )


class RuntimeEventStore:
    """Schema-v2-only canonical store with durable session-scoped idempotency.

    Task 2 起 ``RuntimeEventStore`` 是单一 SessionEvent Store 的 typed view：
    传入 ``SessionEventStore`` 时走 envelope 写路径（只接受
    ``ActivationWriteGuard``），传 session service 时保持旧 carrier 兼容路径。
    """

    def __init__(self, store: Any, *, session_id: str | None = None) -> None:
        if _is_session_event_store(store):
            self._event_store: SessionEventStore | None = store
            self._service = getattr(store, "session_service", None)
            self._typed_session_id = session_id
        else:
            self._event_store = None
            self._service = store
            self._typed_session_id = session_id

    @property
    def session_service(self) -> Any:
        return self._service

    @property
    def session_id(self) -> str | None:
        """Session bound at construction for the typed (envelope) write path."""

        return self._typed_session_id

    @property
    def event_store(self) -> "SessionEventStore | None":
        return self._event_store

    async def append(
        self,
        session_id_or_event: Any,
        events: Iterable[RuntimeEvent] | None = None,
        *,
        guard: ActivationWriteGuard | None = None,
    ) -> Any:
        """Typed view append: ``append(event, *, guard=ActivationWriteGuard)``.

        旧签名 ``append(session_id, events)`` 保持兼容（carrier 路径）。
        """

        if not isinstance(session_id_or_event, str):
            if events is not None:
                raise TypeError("typed append takes a single RuntimeEvent")
            if not isinstance(guard, ActivationWriteGuard):
                raise TypeError(
                    "RuntimeEventStore typed append requires an ActivationWriteGuard"
                )
            return await self.append_typed(session_id_or_event, guard=guard)
        if guard is not None:
            raise TypeError("legacy append(session_id, events) does not take a guard")
        return [await self.append_one(session_id_or_event, event) for event in events or ()]

    async def append_typed(
        self, event: RuntimeEvent, *, guard: ActivationWriteGuard
    ) -> RuntimeEvent:
        """Persist one fact through the generic SessionEventStore envelope."""

        if self._event_store is None:
            raise RuntimeError("typed append requires a SessionEventStore-backed runtime view")
        if self._typed_session_id is None or not self._typed_session_id.strip():
            raise ValueError("typed append requires a session_id bound at construction")
        if not isinstance(guard, ActivationWriteGuard):
            raise TypeError("RuntimeEventStore only accepts ActivationWriteGuard")
        envelope = runtime_event_envelope(self._typed_session_id, event)
        persisted = await self._event_store.append(envelope, guard=guard)
        return parse_runtime_event(dict(persisted.payload) | {"seq": persisted.seq})

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

    async def _read_rows(
        self,
        session_id: str,
        after_seq: int,
        before_seq: int | None,
        *,
        limit: int | None = None,
    ):
        """Typed envelope 路径的读取兜底。

        hosted PG 的 ``PostgresFencedSessionEventStore`` 只包 kernel store，
        没有 ``session_service``（``_service is None``）。冷恢复
        (scan_open_runs -> list) 在此之前会 AttributeError，导致 takeover
        recovery 双路径失败 -> runtime degraded。改走 event store 自己的
        ``read``（envelope 语义）再转 SessionEvent 行。
        """

        if self._service is not None:
            return await self._service.get_events(
                session_id,
                limit=limit,
                after_seq_id=after_seq,
                before_seq_id=before_seq,
            )
        if self._event_store is None:
            raise RuntimeError(
                "RuntimeEventStore has neither a session service nor an event store"
            )
        rows = []
        from ksadk.events.session_event import envelope_to_session_event

        for envelope in await self._event_store.read(
            session_id, int(after_seq), int(limit or 100_000)
        ):
            if before_seq is not None and int(envelope.seq) >= int(before_seq):
                break
            row = envelope_to_session_event(envelope)
            if int(row.seq_id or 0) != int(envelope.seq):
                row.seq_id = int(envelope.seq)
            rows.append(row)
        return rows

    async def page(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        before_seq: int | None = None,
        limit: int = 500,
    ) -> list[RuntimeEvent]:
        """Read the next canonical page in ascending physical cursor order.

        ``list(..., limit=...)`` is a compatibility tail projection.  Durable
        export and replay callers that need bounded forward pagination must use
        this explicit method, otherwise a large session can be read wholesale
        before Python applies its limit.
        """

        if limit < 1:
            raise ValueError("limit must be positive")
        raw = await self._read_rows(
            session_id,
            int(after_seq),
            before_seq,
            limit=limit,
        )
        events = [
            canonical
            for canonical in (session_event_to_runtime_event(item) for item in raw)
            if canonical is not None
        ]
        events.sort(key=lambda event: event.seq)
        return events[:limit]

    async def event_by_id(self, session_id: str, event_id: str) -> RuntimeEvent | None:
        if self._service is not None:
            self._require_storage_capabilities()
            storage_id = canonical_storage_id(session_id, event_id)
            stored = await self._service.get_event_by_id(session_id, storage_id)
            return session_event_to_runtime_event(stored) if stored is not None else None
        for event in await self.list(session_id):
            if event.event_id == event_id:
                return event
        return None

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
        if run_id is None or self._service is None:
            # run 过滤在 typed envelope 兜底路径上退化为全量读取后按
            # run_id 过滤（fenced store 没有按 invocation 的索引查询）。
            raw = await self._read_rows(session_id, after_seq, before_seq)
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

    async def list_run_ids(self, session_id: str) -> list[str]:
        """Distinct run ids in session order of first appearance."""

        seen: dict[str, None] = {}
        for event in await self.list(session_id):
            seen.setdefault(event.run_id, None)
        return list(seen)

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
    "runtime_event_envelope",
    "runtime_event_envelope_id",
]
