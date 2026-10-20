"""Optional host call limits, without non-finite numbers in durable contracts.

None means no call-count fence. Zero remains an exhausted finite allowance.
Usage and child reservations are accounted independently of these limits.
"""

from __future__ import annotations


def call_room(limit: int | None, claimed: int, additional: int = 1) -> bool:
    return limit is None or claimed + additional <= limit


def remaining_calls(limit: int | None, claimed: int, reserve: int = 0) -> int | None:
    return None if limit is None else max(0, limit - claimed - reserve)


def cap_calls(*limits: int | None) -> int | None:
    finite = [limit for limit in limits if limit is not None]
    return min(finite) if finite else None


def share_calls(remaining: int | None, count: int, index: int) -> int | None:
    return None if remaining is None else remaining // count + int(index < remaining % count)
