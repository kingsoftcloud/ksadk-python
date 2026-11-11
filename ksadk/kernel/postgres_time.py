"""Timestamp normalization shared by the existing PostgreSQL store transactions."""

from datetime import datetime, timezone


def parse_ts(value: str | None):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
