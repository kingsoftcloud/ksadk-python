"""Context-engine baseline recording kept outside the stream hot path."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any


def record_baseline_turn(
    *,
    prepared: Any,
    model: str | None,
    usage: Any,
    ptl: bool,
    attempts: int,
    turn_start_monotonic: float | None,
) -> None:
    """Record one env-gated baseline turn without affecting decisions."""

    from ksadk.context_engine.baseline import record_baseline_turn as record

    latency_ms = None
    if turn_start_monotonic is not None:
        latency_ms = int((time.monotonic() - turn_start_monotonic) * 1000)
    record(
        getattr(prepared, "shadow_context_plan", None),
        session_id=getattr(prepared, "session_id", ""),
        invocation_id=getattr(prepared, "invocation_id", ""),
        model=str(model or ""),
        usage=usage if isinstance(usage, Mapping) else None,
        compaction_triggered=bool(getattr(prepared, "compaction_triggered", False)),
        compaction_trigger=str(getattr(prepared, "compaction_trigger", "") or ""),
        prompt_too_long=ptl,
        retry_attempts=attempts,
        turn_latency_ms=latency_ms,
    )
