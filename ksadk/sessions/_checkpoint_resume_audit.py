"""Count logical resume attempts across legacy and canonical carriers."""

from __future__ import annotations

from typing import Any

from ksadk.sessions.base import SessionEvent, checkpoint_resume_identity


class CheckpointResumeAudit(dict[str, Any]):
    """Public audit fields with private, non-serialized deduplication state."""

    def __init__(self) -> None:
        super().__init__(resume_count=0, last_resumed_at=None)
        self._attempts: set[tuple[str, str, str, str, str]] = set()

    def record(self, event: SessionEvent, identity: tuple[str, str, float] | None = None) -> None:
        identity = identity or checkpoint_resume_identity(event)
        if identity is None:
            return
        if event.event_type in {"run_resume", "runtime_resume"}:
            attempt = (event.metadata or {}).get("resume_attempt_id")
        else:
            payload = (event.content or {}).get("runtime_event")
            if not isinstance(payload, dict):
                envelope = (event.content or {}).get("session_event")
                payload = envelope.get("payload") if isinstance(envelope, dict) else {}
            attempt = (payload or {}).get("resume_attempt_id")
        attempt_id = str(attempt or "").strip()
        # Historical rows without an attempt id are independent facts. Do not
        # collapse them all into one empty-key attempt during migration.
        key = (
            event.session_id,
            identity[0],
            identity[1],
            "attempt" if attempt_id else "event",
            attempt_id or event.id,
        )
        if key not in self._attempts:
            self._attempts.add(key)
            self["resume_count"] += 1
        timestamp = identity[2]
        current = self["last_resumed_at"]
        self["last_resumed_at"] = timestamp if current is None else max(current, timestamp)
