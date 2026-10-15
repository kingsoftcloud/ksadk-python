"""Import an authorized legacy conversation prefix into the Kernel log.

The control plane may own a v1 history before a Session is upgraded to the
AgentKernel path. A trusted enqueue can carry that frozen prefix once. The
worker writes deterministic workflow facts before starting the new Run, so
old and new history share the Runtime's physical per-Session cursor instead
of being merged from two cursor domains at read time.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping
from uuid import UUID, uuid5

from ksadk.kernel.contracts import (
    ActivationWriteGuard,
    AgentControlCommand,
    SessionEventEnvelope,
)

LEGACY_HISTORY_PAYLOAD_KEY = "legacy_history"
LEGACY_HISTORY_EVENT_KEY = "legacy_history_event"
LEGACY_HISTORY_MAX_EVENTS = 2000
_LEGACY_HISTORY_NAMESPACE = UUID("9ccedc3a-9283-5f1e-86e3-5e2242c933b8")


def _timestamp(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("legacy history Timestamp must be nonempty")
    datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return raw


def normalize_legacy_history(
    session_id: str,
    value: Any,
) -> list[dict[str, Any]]:
    """Validate and freeze the bounded legacy prefix carried by Server."""

    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("legacy_history must be a list")
    if len(value) > LEGACY_HISTORY_MAX_EVENTS:
        raise ValueError(f"legacy_history exceeds {LEGACY_HISTORY_MAX_EVENTS} events")

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    previous_seq = 0
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValueError("legacy history event must be an object")
        event_session_id = str(raw.get("SessionId") or session_id).strip()
        if event_session_id != session_id:
            raise ValueError("legacy history event belongs to another Session")
        event_id = str(raw.get("EventId") or "").strip()
        event_type = str(raw.get("EventType") or "").strip()
        if not event_id or len(event_id) > 256:
            raise ValueError("legacy history EventId must be 1..256 characters")
        if event_id in seen_ids:
            raise ValueError("legacy history EventId must be unique")
        if not event_type or len(event_type) > 128:
            raise ValueError("legacy history EventType must be 1..128 characters")
        try:
            seq = int(raw.get("SeqId"))
        except (TypeError, ValueError) as exc:
            raise ValueError("legacy history SeqId must be an integer") from exc
        if seq <= previous_seq:
            raise ValueError("legacy history SeqId must be strictly increasing")
        content = raw.get("Content") or {}
        state_delta = raw.get("StateDelta") or {}
        metadata = raw.get("Metadata") or {}
        if not all(isinstance(item, Mapping) for item in (content, state_delta, metadata)):
            raise ValueError("legacy history Content/StateDelta/Metadata must be objects")

        event: dict[str, Any] = {
            "EventId": event_id,
            "SessionId": session_id,
            "Author": str(raw.get("Author") or ""),
            "EventType": event_type,
            "Content": dict(content),
            "StateDelta": dict(state_delta),
            "SeqId": seq,
            "Metadata": dict(metadata),
            "Timestamp": _timestamp(raw.get("Timestamp")),
        }
        invocation_id = str(raw.get("InvocationId") or "").strip()
        if invocation_id:
            event["InvocationId"] = invocation_id
        # Command validation already enforces JSON values. Serializing here
        # also gives the deterministic event UUID a canonical input.
        json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        normalized.append(event)
        seen_ids.add(event_id)
        previous_seq = seq
    return normalized


def legacy_history_envelope(
    command: AgentControlCommand,
    event: Mapping[str, Any],
) -> SessionEventEnvelope:
    frozen = dict(event)
    fingerprint = json.dumps(
        frozen,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    event_id = uuid5(
        _LEGACY_HISTORY_NAMESPACE,
        f"{command.tenant_id}\n{command.agent_instance_id}\n{command.session_id}\n{fingerprint}",
    )
    return SessionEventEnvelope(
        event_id=event_id,
        session_id=command.session_id,
        seq=0,
        timestamp=str(frozen["Timestamp"]),
        family="workflow",
        family_version=1,
        # Retain the legacy type at the storage layer so EventTypes filters
        # keep working after migration.
        event_type=str(frozen["EventType"]),
        payload={LEGACY_HISTORY_EVENT_KEY: frozen},
        run_id=str(frozen.get("InvocationId") or "") or None,
        causation_id=str(command.command_id),
        actor_ref="legacy-history-import",
    )


async def import_legacy_history(
    command: AgentControlCommand,
    *,
    session_events: Any,
    guard: ActivationWriteGuard,
) -> int:
    """Idempotently append the trusted prefix under the current fence."""

    events = normalize_legacy_history(
        command.session_id,
        command.payload.get(LEGACY_HISTORY_PAYLOAD_KEY),
    )
    for event in events:
        await session_events.append(
            legacy_history_envelope(command, event),
            guard=guard,
        )
    return len(events)


def project_legacy_history_event(event: Any) -> dict[str, Any] | None:
    """Restore an imported v1 event while retaining the physical seq."""

    content = getattr(event, "content", None) or {}
    envelope = content.get("session_event") if isinstance(content, Mapping) else None
    if not isinstance(envelope, Mapping) or envelope.get("family") != "workflow":
        return None
    payload = envelope.get("payload")
    frozen = payload.get(LEGACY_HISTORY_EVENT_KEY) if isinstance(payload, Mapping) else None
    if not isinstance(frozen, Mapping):
        return None
    projected = dict(frozen)
    projected["SessionId"] = str(getattr(event, "session_id", "") or "")
    projected["SeqId"] = int(getattr(event, "seq_id", 0) or 0)
    return projected


__all__ = [
    "LEGACY_HISTORY_EVENT_KEY",
    "LEGACY_HISTORY_MAX_EVENTS",
    "LEGACY_HISTORY_PAYLOAD_KEY",
    "import_legacy_history",
    "legacy_history_envelope",
    "normalize_legacy_history",
    "project_legacy_history_event",
]
