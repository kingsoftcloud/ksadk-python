"""Deterministic, dependency-free calendar calculations for Scheduler Lite.

The supported cron dialect intentionally stays small and explicit: five fields,
numeric values, comma lists, ranges and ``*/step``.  It is sufficient for the
Studio scheduler and avoids inheriting an accidental transitive dependency.
All matching happens after UTC -> IANA timezone conversion.  That means a
nonexistent DST local minute is skipped and the repeated fall-back minute runs
once (the first ``fold`` only).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ksadk.scheduler.contracts import ScheduleSpec, _parse_cron


def next_schedule_time(
    schedule: ScheduleSpec,
    *,
    after: datetime,
    anchor_at: datetime | None = None,
) -> datetime | None:
    """Return the first scheduled UTC timestamp strictly after ``after``."""

    if after.tzinfo is None:
        raise ValueError("after must include a timezone")
    after = after.astimezone(timezone.utc)
    if schedule.kind == "once":
        assert schedule.at is not None
        return schedule.at if schedule.at > after else None
    if schedule.kind == "interval":
        assert schedule.every_seconds is not None
        anchor = (schedule.anchor_at or anchor_at or after).astimezone(timezone.utc)
        if anchor > after:
            return anchor
        elapsed = (after - anchor).total_seconds()
        step = schedule.every_seconds
        return anchor + timedelta(seconds=(int(elapsed // step) + 1) * step)
    assert schedule.expression is not None
    return _next_cron(schedule.expression, schedule.timezone, after)


def _next_cron(expression: str, timezone_name: str, after: datetime) -> datetime:
    minute, hour, day, month, weekday = _parse_cron(expression)
    fields = (
        _expand_field(minute, 0, 59),
        _expand_field(hour, 0, 23),
        _expand_field(day, 1, 31),
        _expand_field(month, 1, 12),
        _expand_field(weekday, 0, 6),
    )
    zone = ZoneInfo(timezone_name)
    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    # Five years is a deliberate guard against impossible expressions such as
    # 31 February.  The caller receives a typed validation error rather than a
    # scheduler loop that runs forever.
    deadline = candidate + timedelta(days=366 * 5)
    while candidate <= deadline:
        local = candidate.astimezone(zone)
        # datetime.weekday uses Monday=0.  Cron uses Sunday=0.
        cron_weekday = (local.weekday() + 1) % 7
        if (
            local.fold == 0
            and local.minute in fields[0]
            and local.hour in fields[1]
            and local.day in fields[2]
            and local.month in fields[3]
            and cron_weekday in fields[4]
        ):
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError("cron expression has no matching time within five years")


def _expand_field(value: str, minimum: int, maximum: int) -> set[int]:
    result: set[int] = set()
    for item in value.split(","):
        if "/" in item:
            base, step_text = item.split("/", 1)
        else:
            base, step_text = item, None
        step = int(step_text) if step_text is not None else 1
        if step <= 0:
            raise ValueError("cron step must be positive")
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            start, end = int(start_text), int(end_text)
        else:
            start = end = int(base)
        if start < minimum or end > maximum or start > end:
            raise ValueError("cron field is outside its valid range")
        result.update(range(start, end + 1, step))
    if not result:
        raise ValueError("cron field must not be empty")
    return result


__all__ = ["next_schedule_time"]
