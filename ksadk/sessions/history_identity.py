"""Source epoch for runtime history and Kernel SSE subscriptions."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from ksadk.sessions.base import Session

_BOOT_ID = uuid.uuid4().hex


def history_identity(service: Any, session: Session) -> tuple[str, bool]:
    """Return a durable creation epoch or process-local live epoch.

    This is a cursor continuity signal, never an authorization decision.
    """
    degraded = bool(getattr(service, "persistence_degraded", lambda _id: False)(session.id))
    primary = getattr(service, "primary", service)
    if not hasattr(primary, "namespace"):
        degraded = True
    if degraded:
        identity = f"live:{_BOOT_ID}:{session.id}:{session.created_at:.9f}"
    else:
        identity = f"durable:{primary.namespace}:{session.id}:{session.created_at:.9f}"
    return hashlib.sha256(identity.encode()).hexdigest()[:32], degraded
