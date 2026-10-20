from __future__ import annotations

import logging
from collections.abc import Iterator

from ksadk.sessions.errors import SessionBackendUnavailable

logger = logging.getLogger(__name__)


def is_transient_session_backend_failure(exc: BaseException) -> bool:
    """Recoverable storage failure, excluding invalid data and authorization."""
    if isinstance(exc, (FileNotFoundError, PermissionError)):
        return False
    for current in _exception_chain(exc):
        state = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if state in {"57014", "55P03", "40001", "40P01"}:
            return True
        if state:
            return str(state).startswith(("08", "53", "57P", "58"))
        if (type(current).__module__.startswith(("psycopg", "psycopg_pool."))
                and type(current).__name__ in {"OperationalError", "InterfaceError", "PoolTimeout"}):
            return True
    return is_session_backend_failure(exc)


def is_session_connection_failure(exc: BaseException) -> bool:
    """A broken transport, not an exhausted budget or a rejected SQL command."""
    for current in _exception_chain(exc):
        state = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if state:
            return str(state).startswith("08") or str(state) in {"57P01", "57P02", "57P03"}
        if isinstance(current, OSError) and not isinstance(current, TimeoutError):
            return True
    return False


def session_backend_timeout_seconds() -> float:
    from ksadk.sessions.db_budget import session_db_budget

    return session_db_budget().connect_seconds


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
    # Driver and SQLAlchemy wrappers both expose SQLSTATE. Integrity, data,
    # syntax and serialization failures mean the backend answered; never
    # switch to an independently sequenced live store for those errors.
    for current in _exception_chain(exc):
        sqlstate = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if sqlstate and not str(sqlstate).startswith(("08", "53", "57P", "58")):
            return False
    for current in _exception_chain(exc):
        if isinstance(
            current,
            (SessionBackendUnavailable, TimeoutError, OSError, ConnectionError),
        ):
            logger.warning(
                "Session backend error; assuming backend failure: %s",
                current
            )
            return True
        module = type(current).__module__
        if module.startswith("asyncpg.") or module.startswith("sqlalchemy."):
            logger.warning(
                "SQLAlchemy error; assuming backend failure: %s",
                current
            )
            return True
    return False
