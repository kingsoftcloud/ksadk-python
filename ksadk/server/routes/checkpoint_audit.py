"""Checkpoint resume audit extraction shared by REST projections."""

from __future__ import annotations

import math
from typing import Any

from ksadk.events.canonical import (
    ContinuationResumed,
    UnknownCanonicalEvent,
    parse_runtime_event_lenient,
)
from ksadk.events.canonical_store import canonical_storage_id
from ksadk.sessions import SessionEvent

_CANONICAL_RUNTIME_MARKER = "ksadk_canonical_runtime_event"
_SESSION_EVENT_ENVELOPE_MARKER = "ksadk_session_event_envelope"


def _finite_audit_timestamp(value: Any) -> float:
    """Normalize audit timestamps without allowing NaN/Infinity to poison max."""

    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return 0.0
    return timestamp if math.isfinite(timestamp) else 0.0


def _normalize_audit_payload_timestamp(
    payload: dict[str, Any], event: SessionEvent
) -> dict[str, Any]:
    """Coerce timestamp text before strict canonical parsing.

    Canonical producers should emit a JSON number.  During migration, a few
    carriers contain numeric strings or malformed values; normalize finite
    numeric strings and fall back to the carrier timestamp for the rest so a
    single bad timestamp does not discard the whole audit row.
    """

    if "timestamp" not in payload:
        return payload
    raw_timestamp = payload.get("timestamp")
    try:
        candidate = float(raw_timestamp)
    except (TypeError, ValueError):
        candidate = _finite_audit_timestamp(event.timestamp)
    if isinstance(raw_timestamp, bool) or not math.isfinite(candidate):
        candidate = _finite_audit_timestamp(event.timestamp)
    normalized = dict(payload)
    normalized["timestamp"] = candidate
    return normalized


def _parse_canonical_payload(
    event: SessionEvent, payload: dict[str, Any]
) -> Any:
    """Parse and validate one canonical payload against its SessionEvent carrier."""

    canonical = parse_runtime_event_lenient(
        _normalize_audit_payload_timestamp(payload, event)
    )
    if isinstance(canonical, UnknownCanonicalEvent):
        return canonical
    if canonical.event_type != event.event_type:
        raise ValueError("canonical SessionEvent event type does not match content")
    if int(canonical.seq) != int(event.seq_id or 0):
        raise ValueError("canonical SessionEvent sequence does not match content")
    if event.invocation_id and canonical.run_id != event.invocation_id:
        raise ValueError("canonical SessionEvent invocation does not match content")
    metadata = event.metadata or {}
    canonical_event_id = str(metadata.get("canonical_event_id") or "")
    # Legacy RuntimeEvent carriers use the producer event id as both their
    # metadata identity and physical storage key.  The typed SessionEvent
    # envelope has a distinct UUID identity, however: its nested
    # ``session_event.event_id`` is the value hashed into ``event.id`` while
    # the runtime payload keeps the producer's free-form ``event_id``.
    # Validate the two identities independently instead of conflating them.
    is_typed_runtime_envelope = bool(
        metadata.get(_SESSION_EVENT_ENVELOPE_MARKER)
        and metadata.get("family") == "runtime"
    )
    if is_typed_runtime_envelope:
        envelope_content = (event.content or {}).get("session_event")
        if not isinstance(envelope_content, dict):
            raise ValueError("canonical SessionEvent is missing session_event content")
        envelope_event_id = str(envelope_content.get("event_id") or "")
        if not envelope_event_id:
            raise ValueError("canonical SessionEvent envelope is missing event id")
        if canonical_event_id != envelope_event_id:
            raise ValueError(
                "canonical SessionEvent event id metadata does not match envelope content"
            )
        storage_event_id = envelope_event_id
    else:
        if canonical_event_id and canonical_event_id != canonical.event_id:
            raise ValueError("canonical SessionEvent event id metadata does not match content")
        storage_event_id = canonical.event_id
    expected_storage_id = canonical_storage_id(event.session_id, storage_event_id)
    if event.id != expected_storage_id:
        raise ValueError("canonical SessionEvent storage id does not match content")
    return canonical


def _record_resume_audit(
    audit_by_session: dict[str, dict[tuple[str, str], dict[str, Any]]],
    event: SessionEvent,
) -> None:
    if event.event_type == "run_resume":
        metadata = event.metadata or {}
        run_id = str(metadata.get("run_id") or "").strip()
        checkpoint_id = str(metadata.get("checkpoint_id") or "").strip()
        timestamp = _finite_audit_timestamp(event.timestamp)
    else:
        # Canonical v2 resume facts supersede the legacy ``run_resume`` carrier.
        # Keep both shapes in one audit map so checkpoint listing and resume
        # resolution apply the same replay policy during the migration window.
        payload = (event.content or {}).get("runtime_event")
        if not isinstance(payload, dict):
            envelope = (event.content or {}).get("session_event")
            payload = envelope.get("payload") if isinstance(envelope, dict) else None
        if not isinstance(payload, dict):
            return
        canonical = _parse_canonical_payload(event, payload)
        if isinstance(canonical, UnknownCanonicalEvent):
            return
        if not isinstance(canonical, ContinuationResumed):
            return
        if canonical.continuation_kind != "graph_checkpoint":
            return
        run_id = canonical.run_id.strip()
        checkpoint_id = canonical.continuation_id.strip()
        timestamp = _finite_audit_timestamp(canonical.timestamp)
    if not run_id or not checkpoint_id:
        return
    session_audit = audit_by_session.setdefault(event.session_id, {})
    item = session_audit.setdefault(
        (run_id, checkpoint_id),
        {"resume_count": 0, "last_resumed_at": None},
    )
    item["resume_count"] = int(item["resume_count"]) + 1
    item["last_resumed_at"] = max(item["last_resumed_at"] or timestamp, timestamp)


def _resume_audit_by_checkpoint(
    events: list[SessionEvent],
) -> dict[tuple[str, str], dict[str, Any]]:
    by_session: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    for event in events:
        _record_resume_audit(by_session, event)
    audit: dict[tuple[str, str], dict[str, Any]] = {}
    for session_audit in by_session.values():
        for key, item in session_audit.items():
            merged = audit.setdefault(key, {"resume_count": 0, "last_resumed_at": None})
            merged["resume_count"] = int(merged["resume_count"]) + int(item["resume_count"])
            timestamp = item.get("last_resumed_at")
            if timestamp is not None:
                merged["last_resumed_at"] = max(
                    merged["last_resumed_at"] or timestamp,
                    timestamp,
                )
    return audit
