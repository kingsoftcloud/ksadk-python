from __future__ import annotations

import logging
import os
from collections.abc import Iterator

from ksadk.sessions.errors import SessionBackendUnavailable

logger = logging.getLogger(__name__)


def session_backend_timeout_seconds() -> float:
    raw = (
        os.getenv("KSADK_SESSION_CONNECT_TIMEOUT")
        or os.getenv("KSADK_SESSION_PG_CONNECT_TIMEOUT")
        or ""
    ).strip()
    if not raw:
        return 5.0
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid KSADK_SESSION_CONNECT_TIMEOUT=%r; using 5 seconds", raw)
        return 5.0
    return max(0.1, value)


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        original = getattr(current, "orig", None)
        if isinstance(original, BaseException) and id(original) not in seen:
            current = original
            continue
        current = current.__cause__ or current.__context__


def is_session_backend_failure(exc: BaseException) -> bool:
    """Return whether an exception represents an unavailable database backend."""
    for current in _exception_chain(exc):
        if isinstance(
            current,
            (SessionBackendUnavailable, TimeoutError, OSError, ConnectionError),
        ):
            return True
        module = type(current).__module__
        if module.startswith("asyncpg.") or module.startswith("sqlalchemy."):
            return True
    return False
