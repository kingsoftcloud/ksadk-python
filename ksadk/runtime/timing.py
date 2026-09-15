"""Response timing contract. Durations use milliseconds, never model content."""

import math
from collections.abc import Mapping
from typing import Any

PHASES = ("agent", "selection", "answer", "skill_load", "skill_execution")


def normalize_timing(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    statuses: dict[str, str] = {}
    raw_statuses = value.get("phase_status")
    raw_statuses = raw_statuses if isinstance(raw_statuses, Mapping) else {}
    for phase in PHASES:
        key = f"{phase}_duration_ms"
        if key not in value:
            continue
        duration = value[key]
        try:
            valid = (
                isinstance(duration, (int, float))
                and not isinstance(duration, bool)
                and math.isfinite(duration)
                and duration >= 0
            )
        except OverflowError:
            valid = False
        result[key] = duration if valid else None
        statuses[phase] = (
            "measured"
            if valid
            else "skipped"
            if duration is None and raw_statuses.get(phase) == "skipped"
            else "unavailable"
        )
    if result:
        result["phase_status"] = statuses
    return result


def extract_timing(result: Any) -> dict[str, Any]:
    """Only opt-in graph/runner state fields are eligible; don't inspect messages."""
    return normalize_timing(result.get("timing")) if isinstance(result, Mapping) else {}
