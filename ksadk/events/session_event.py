"""Generic single-log SessionEvent store port（Phase 1 Task 2）。

把 control/runtime/workflow 等 family 的 ``SessionEventEnvelope/v1`` 收敛进
同一个 session event log，复用 Session backend 的原子 per-session seq。
写入权限是 typed guard（``AdmissionWriteGuard | ActivationWriteGuard``），
禁止无 guard append；发布（订阅可见性）只发生在 backend 事务 commit 之后，
订阅先 replay ``seq > after_seq`` 再切 live，用同一 cursor 去重。

物理 ``SessionEvent.id`` 是 ``(session_id, event_id)`` 的确定性编码，
与 ``ksadk.events.canonical_store.canonical_storage_id`` 算法一致，
让 durable 主键在分配 session cursor 之前先约束幂等域。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, Protocol

from ksadk.events.canonical import parse_runtime_event
from ksadk.kernel.contracts import (
    ActivationWriteGuard,
    AdmissionWriteGuard,
    SessionEventEnvelope,
    SessionEventWriteGuard,
)
from ksadk.sessions.base import BaseSessionService, SessionEvent

_ENVELOPE_MARKER = "ksadk_session_event_envelope"
_ENVELOPE_CONTENT_KEY = "session_event"
_RUNTIME_CONTENT_KEY = "runtime_event"
_RUNTIME_FAMILY = "runtime"
_RUNTIME_FAMILY_VERSION = 2
_ADMISSION_CONTROL_EVENT_TYPES = frozenset(
    {"control.command_accepted", "control.command_rejected"}
)


def session_event_storage_id(session_id: str, event_id: str) -> str:
    """Deterministic physical id for one envelope fact (same digest as canonical)."""

    if not session_id.strip() or not event_id.strip():
        raise ValueError("session_id and event_id must be nonempty")
    encoded = json.dumps([session_id, event_id], ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return f"cev_{hashlib.sha256(encoded).hexdigest()[:40]}"


def _timestamp_to_float(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


class SessionEventStore(Protocol):
    """Generic envelope port. Append without a typed guard is forbidden."""

    async def append(
        self, envelope: SessionEventEnvelope, *, guard: SessionEventWriteGuard
    ) -> SessionEventEnvelope: ...

    async def read(
        self, session_id: str, after_seq: int, limit: int
    ) -> list[SessionEventEnvelope]: ...

    def subscribe(
        self, session_id: str, after_seq: int
    ) -> AsyncIterator[SessionEventEnvelope]: ...


def validate_write_guard(
    envelope: SessionEventEnvelope, guard: SessionEventWriteGuard
) -> SessionEventWriteGuard:
    """Typed write permission: no bare booleans, no nullable fences.

    AdmissionWriteGuard 只允许 admission 产生的 ``control.command_accepted`` /
    ``control.command_rejected``。ActivationWriteGuard 在 Phase 1 允许
    worker/control/runtime facts；activation_id/fencing_token 与 lease 的
    事务内比较由 Task 3 的 AgentKernelStore 承接。
    """

    if isinstance(guard, bool) or not isinstance(guard, (AdmissionWriteGuard, ActivationWriteGuard)):
        raise TypeError(
            "append requires a typed SessionEventWriteGuard "
            "(AdmissionWriteGuard | ActivationWriteGuard)"
        )
    if isinstance(guard, AdmissionWriteGuard):
        if envelope.family != "control" or envelope.event_type not in _ADMISSION_CONTROL_EVENT_TYPES:
            raise PermissionError(
                "AdmissionWriteGuard may only append control.command_accepted or "
                "control.command_rejected facts"
            )
    return guard


def envelope_to_session_event(envelope: SessionEventEnvelope) -> SessionEvent:
    """Pack one envelope into the existing SessionEvent carrier."""

    content: dict[str, Any] = {_ENVELOPE_CONTENT_KEY: envelope.model_dump(mode="json")}
    binding = "session_event.seq"
    if envelope.family == _RUNTIME_FAMILY and envelope.family_version == _RUNTIME_FAMILY_VERSION:
        binding = "runtime_event.seq"
        content[_RUNTIME_CONTENT_KEY] = dict(envelope.payload)
    metadata: dict[str, Any] = {
        _ENVELOPE_MARKER: True,
        "schema_version": 1,
        "family": envelope.family,
        "family_version": envelope.family_version,
        "canonical_event_id": str(envelope.event_id),
    }
    if envelope.run_id is not None:
        metadata["run_id"] = envelope.run_id
    return SessionEvent(
        id=session_event_storage_id(envelope.session_id, str(envelope.event_id)),
        session_id=envelope.session_id,
        author=envelope.actor_ref or envelope.family,
        event_type=envelope.event_type,
        content=content,
        timestamp=_timestamp_to_float(envelope.timestamp),
        invocation_id=envelope.run_id,
        metadata=metadata,
        seq_binding=binding,  # type: ignore[arg-type]
    )


def session_event_to_envelope(event: SessionEvent) -> SessionEventEnvelope | None:
    """Restore an envelope using the physical session cursor as ``seq``."""

    metadata = event.metadata or {}
    if not metadata.get(_ENVELOPE_MARKER):
        return None
    dump = dict((event.content or {}).get(_ENVELOPE_CONTENT_KEY) or {})
    if not isinstance(dump, dict):
        raise ValueError("canonical SessionEvent is missing session_event content")
    if metadata.get("family") == _RUNTIME_FAMILY:
        runtime_payload = (event.content or {}).get(_RUNTIME_CONTENT_KEY)
        if not isinstance(runtime_payload, dict):
            raise ValueError("runtime family SessionEvent is missing runtime_event content")
        if runtime_payload.get("seq") != event.seq_id:
            raise ValueError("runtime payload seq does not match physical seq")
        dump["payload"] = dict(runtime_payload)
    if str(dump.get("event_id")) != str(metadata.get("canonical_event_id")):
        raise ValueError("canonical SessionEvent event id metadata does not match content")
    dump["seq"] = event.seq_id
    return SessionEventEnvelope.model_validate(dump)


class SessionServiceEventStore:
    """``SessionEventStore`` adapter over one ``BaseSessionService`` backend.

    ``fence_validator`` 是可选的 ActivationWriteGuard 事务内 CAS seam：
    提供时（典型为 ``AgentKernelStore.validate_write_fence``），每个
    activation 写都在持久化之前比较当前 lease 的 fencing token，被
    takeover 的旧 owner 得到 :class:`~ksadk.kernel.errors.StaleFenceError`。
    validator 只看 guard，不向 envelope/payload 写入任何 fence 字段。
    """

    def __init__(
        self,
        session_service: BaseSessionService,
        *,
        fence_validator: Callable[
            [SessionEventEnvelope, ActivationWriteGuard], Awaitable[None]
        ]
        | None = None,
    ) -> None:
        self._service = session_service
        self._fence_validator = fence_validator

    @property
    def session_service(self) -> BaseSessionService:
        return self._service

    async def append(
        self, envelope: SessionEventEnvelope, *, guard: SessionEventWriteGuard
    ) -> SessionEventEnvelope:
        validate_write_guard(envelope, guard)
        if (
            isinstance(guard, ActivationWriteGuard)
            and self._fence_validator is not None
        ):
            await self._fence_validator(envelope, guard)
        if not envelope.session_id.strip():
            raise ValueError("session_id must be nonempty")
        self._require_storage_capabilities(envelope)
        existing = await self._find_envelope(envelope)
        if existing is not None:
            self._assert_same_fact(existing, envelope)
            return existing
        packed = envelope_to_session_event(envelope)
        try:
            stored = await self._service.append_event(envelope.session_id, packed)
        except Exception:
            # Deterministic physical id turns concurrent appends into an
            # insert-winner/insert-loser race on durable backends.
            existing = await self._find_envelope(envelope)
            if existing is None:
                raise
            self._assert_same_fact(existing, envelope)
            return existing
        persisted = session_event_to_envelope(stored)
        if persisted is None:  # pragma: no cover - packed by this module
            raise RuntimeError("SessionEventEnvelope lost its storage marker")
        return persisted

    async def read(
        self, session_id: str, after_seq: int, limit: int
    ) -> list[SessionEventEnvelope]:
        if limit < 1:
            raise ValueError("limit must be positive")
        raw = await self._service.get_events(
            session_id,
            after_seq_id=int(after_seq),
            limit=limit,
        )
        rows = sorted(raw, key=lambda event: event.seq_id)
        envelopes = []
        for row in rows:
            envelope = session_event_to_envelope(row)
            if envelope is None:
                continue
            _validate_runtime_payload(envelope)
            envelopes.append(envelope)
        return envelopes[:limit]

    async def subscribe(
        self,
        session_id: str,
        after_seq: int,
        *,
        poll_interval: float = 0.25,
        timeout: float = 5 * 60,
        should_stop: Callable[[], Awaitable[bool]] | None = None,
    ) -> AsyncIterator[SessionEventEnvelope]:
        """Replay ``seq > after_seq`` first, then follow live with the same cursor.

        只读取已 commit 的事实行（backend append 返回值），因此 publish 天然
        发生在 transaction commit 之后；replay→live 切换窗口由同一 cursor 去重。
        """

        cursor = int(after_seq or 0)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            rows = await self._service.get_events(session_id, after_seq_id=cursor)
            rows.sort(key=lambda event: event.seq_id)
            for row in rows:
                cursor = row.seq_id
                envelope = session_event_to_envelope(row)
                if envelope is not None:
                    _validate_runtime_payload(envelope)
                    yield envelope
            if asyncio.get_running_loop().time() >= deadline:
                return
            if should_stop is not None and await should_stop():
                # 客户端断开：及时收口，而不是继续轮询到 timeout。
                return
            await asyncio.sleep(poll_interval)

    async def _find_envelope(
        self, envelope: SessionEventEnvelope
    ) -> SessionEventEnvelope | None:
        storage_id = session_event_storage_id(envelope.session_id, str(envelope.event_id))
        stored = await self._service.get_event_by_id(envelope.session_id, storage_id)
        return session_event_to_envelope(stored) if stored is not None else None

    @staticmethod
    def _assert_same_fact(
        existing: SessionEventEnvelope, candidate: SessionEventEnvelope
    ) -> None:
        # ``seq`` is the store-assigned delivery cursor, not producer identity;
        # the runtime payload's placeholder ``seq`` participates in the same rule.
        def _comparable(envelope: SessionEventEnvelope) -> dict[str, Any]:
            dump = envelope.model_dump(mode="json", exclude={"seq"})
            payload = dict(dump.get("payload") or {})
            payload.pop("seq", None)
            dump["payload"] = payload
            return dump

        if _comparable(existing) != _comparable(candidate):
            raise ValueError(f"SessionEvent id collision for {candidate.event_id!r}")

    def _require_storage_capabilities(self, envelope: SessionEventEnvelope) -> None:
        capabilities = self._service.storage_capabilities
        required_binding = (
            "runtime_event.seq"
            if envelope.family == _RUNTIME_FAMILY
            and envelope.family_version == _RUNTIME_FAMILY_VERSION
            else "session_event.seq"
        )
        if required_binding not in capabilities.atomic_seq_bindings or (
            not capabilities.indexed_event_lookup
        ):
            raise RuntimeError(
                "session backend must support atomic "
                f"{required_binding} binding and indexed physical event lookup"
            )


def _validate_runtime_payload(envelope: SessionEventEnvelope) -> None:
    """family=runtime/v2 时 payload 必须通过现有 RuntimeEvent/v2 校验。"""

    if envelope.family != _RUNTIME_FAMILY or envelope.family_version != _RUNTIME_FAMILY_VERSION:
        return
    try:
        parse_runtime_event(dict(envelope.payload))
    except Exception as error:  # noqa: BLE001 - surface as contract violation
        raise ValueError(
            f"runtime family payload failed RuntimeEvent/v2 validation: {error}"
        ) from error


__all__ = [
    "SessionEventStore",
    "SessionServiceEventStore",
    "validate_write_guard",
    "envelope_to_session_event",
    "session_event_to_envelope",
    "session_event_storage_id",
]
