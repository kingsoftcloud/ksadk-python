"""Shared database phase budgets for session, Kernel, ADK and LangGraph storage.

Each positive finite ``KSADK_DB_<PHASE>_SECONDS`` value overrides its default:
CONNECT=2, READ_ACQUIRE=3, READ_SQL=8, WRITE_ACQUIRE=3,
WRITE_SQL=15, WRITE_OPERATION=30, REPLAY_SQL=10, REPLAY_OPERATION=60, LOCK=2, CLEANUP=2. Invalid values use the
default with a warning. CONNECT also accepts the legacy
``KSADK_SESSION_CONNECT_TIMEOUT`` / ``KSADK_SESSION_PG_CONNECT_TIMEOUT``
when the new name is absent. These are per-phase limits, not retry budgets.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)
_cleanup_deadline: ContextVar[float | None] = ContextVar("ksadk_db_cleanup_deadline", default=None)


def _terminate_and_cancel(
    task: asyncio.Task[Any], connection: Any, *, cancel_task: bool = True,
) -> None:
    # A pool proxy may already be detached when an outer HTTP deadline races
    # driver cancellation. Always reap the task even when terminate() raises;
    # cleanup failure must not replace the original timeout/cancellation.
    task.add_done_callback(
        lambda finished: finished.exception() if not finished.cancelled() else None
    )
    try:
        terminate = getattr(connection, "terminate", None)
        if callable(terminate):
            terminate()
    except Exception:
        logger.debug("Connection already unavailable during cancellation", exc_info=True)
    finally:
        if cancel_task:
            task.cancel()


async def _cancel_driver_task(task: asyncio.Task[Any], connection: Any, seconds: float) -> None:
    """Let the driver send PostgreSQL CancelRequest before discarding its socket.

    Closing first prevents asyncpg from cancelling a running query. The task
    may finish before PostgreSQL acknowledges cancellation; pool release then
    waits for that acknowledgement under the same cleanup deadline.
    """
    loop = asyncio.get_running_loop()
    deadline = _cleanup_deadline.get()
    if deadline is None:
        deadline = loop.time() + seconds
        _cleanup_deadline.set(deadline)
    task.add_done_callback(
        lambda finished: finished.exception() if not finished.cancelled() else None
    )
    task.cancel()
    try:
        done, _ = await asyncio.wait({task}, timeout=max(0.0, deadline - loop.time()))
    except asyncio.CancelledError:
        _terminate_and_cancel(task, connection)
        raise
    if not done:
        _terminate_and_cancel(task, connection)


async def bounded_driver_call(
    awaitable: Any, connection: Any, seconds: float, cleanup_seconds: float = 1.0,
) -> Any:
    """Hard-wall driver command cap, including a cancellation-resistant socket."""
    task = asyncio.create_task(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=seconds)
        if done:
            return await task
        await _cancel_driver_task(task, connection, cleanup_seconds)
        raise TimeoutError("database command deadline exceeded")
    except asyncio.CancelledError:
        await _cancel_driver_task(task, connection, cleanup_seconds)
        raise


async def bounded_cleanup(
    awaitable: Any, connection: Any, seconds: float, *, cancel_task: bool = True,
) -> Any:
    """Share one cleanup deadline across rollback and pool release.

    A driver can ignore task cancellation while a socket is stalled.  Wait on
    a separate task so the caller can terminate that connection at the
    deadline without waiting again for cancellation acknowledgement.
    """
    loop = asyncio.get_running_loop()
    deadline = _cleanup_deadline.get()
    if deadline is None:
        deadline = loop.time() + seconds
        _cleanup_deadline.set(deadline)
    task = asyncio.create_task(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=max(0.0, deadline - loop.time()))
        if done:
            return await task
        _terminate_and_cancel(task, connection, cancel_task=cancel_task)
        raise TimeoutError("database cleanup deadline exceeded")
    except asyncio.CancelledError:
        _terminate_and_cancel(task, connection, cancel_task=cancel_task)
        raise


async def release_pool_lease(pool: Any, lease: Any, connection: Any, seconds: float) -> None:
    """Give asyncpg reset its cleanup budget, independent of pool acquisition.

    asyncpg's acquire context manager silently reuses the acquire timeout for
    release/reset unless Pool.release receives an explicit timeout. Custom
    pool context managers retain their existing exit semantics.
    """
    try:
        from asyncpg import InterfaceError, Pool
    except ImportError:
        Pool = ()
    if isinstance(pool, Pool):
        deadline = _cleanup_deadline.get()
        remaining = seconds if deadline is None else max(
            0.0, deadline - asyncio.get_running_loop().time()
        )
        cleanup = pool.release(connection, timeout=remaining)
    else:
        cleanup = lease.__aexit__(None, None, None)
    try:
        # Pool.release shields a holder task. At the deadline close the
        # connection but keep the public release task observing that holder's
        # result. This needs no extra task on the normal reset path. Custom
        # context managers retain the existing cancellation behavior.
        await bounded_cleanup(cleanup, connection, seconds, cancel_task=not isinstance(pool, Pool))
    except Exception as exc:
        if not isinstance(pool, Pool):
            raise  # Custom lease managers may perform more than driver reset.
        # The statement/transaction outcome was already determined before
        # release. A failed reset must discard the transport, not turn a
        # successful read/COMMIT into a backend failure (or mask a SQL error).
        # Cancellation remains a BaseException and still propagates.
        try:
            connection.terminate()
        except InterfaceError:
            # Pool.release terminates on reset failure and can have detached
            # this proxy already. There is no usable connection to return.
            pass
        logger.warning("Postgres connection reset failed (%s); discarded connection",
                       type(exc).__name__)


@dataclass(frozen=True)
class DatabaseBudget:
    connect_seconds: float = 2.0
    read_acquire_seconds: float = 3.0
    read_sql_seconds: float = 8.0
    write_acquire_seconds: float = 3.0
    write_sql_seconds: float = 15.0
    write_operation_seconds: float = 30.0
    checkpoint_read_operation_seconds: float = 45.0
    lock_seconds: float = 2.0
    cleanup_seconds: float = 2.0
    replay_operation_seconds: float = 60.0
    replay_sql_seconds: float = 10.0
    event_observe_recovery_seconds: float = 60.0


class DatabaseOperationDeadline:
    """One write/reconciliation clock; cleanup may consume one extra phase.

    Construct at the public write entry, before acquiring a connection. Pass
    the same object to unknown-commit readback so it cannot restart a fresh
    operation budget. A nonpositive remainder means return an unknown outcome
    without retrying the write or its upstream side effect.
    """

    def __init__(self, budget: DatabaseBudget) -> None:
        loop = asyncio.get_running_loop()
        self.expires_at = loop.time() + budget.write_operation_seconds

    def remaining_seconds(self) -> float:
        return max(0.0, self.expires_at - asyncio.get_running_loop().time())

    async def run(self, awaitable: Any) -> Any:
        remaining = self.remaining_seconds()
        if remaining <= 0:
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise TimeoutError("database write operation deadline exceeded")
        async with asyncio.timeout(remaining):
            return await awaitable


def _positive_seconds(name: str, default: float, *aliases: str) -> float:
    raw = next((value for key in (name, *aliases)
                if (value := os.getenv(key, "").strip())), "")
    if not raw:
        return default
    try:
        value = float(raw)
        if math.isfinite(value) and value > 0:
            return value
    except ValueError:
        pass
    logger.warning("Invalid %s value; using %.3f seconds", name, default)
    return default


def session_db_budget() -> DatabaseBudget:
    defaults = DatabaseBudget()
    values = {}
    for field_name in defaults.__dataclass_fields__:
        name = "KSADK_DB_" + field_name.removesuffix("_seconds").upper() + "_SECONDS"
        aliases = (
            ("KSADK_SESSION_CONNECT_TIMEOUT", "KSADK_SESSION_PG_CONNECT_TIMEOUT")
            if field_name == "connect_seconds" else ()
        )
        values[field_name] = _positive_seconds(name, getattr(defaults, field_name), *aliases)
    return DatabaseBudget(**values)


class TimedDatabaseConnection:
    """Delegate driver calls under the configured SQL and lock phase limits."""

    def __init__(self, connection: Any, budget: DatabaseBudget, *, write: bool) -> None:
        self._connection = connection
        self._budget = budget
        self._write = write

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    async def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        seconds = self._budget.write_sql_seconds if self._write else self._budget.read_sql_seconds
        return await bounded_driver_call(
            getattr(self._connection, name)(*args, **kwargs), self._connection, seconds,
            self._budget.cleanup_seconds,
        )

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("execute", *args, **kwargs)

    async def fetch(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("fetch", *args, **kwargs)

    async def fetchrow(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("fetchrow", *args, **kwargs)

    async def fetchval(self, *args: Any, **kwargs: Any) -> Any:
        return await self._call("fetchval", *args, **kwargs)

    @asynccontextmanager
    async def transaction(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        transaction = self._connection.transaction(*args, **kwargs)
        seconds = self._budget.write_sql_seconds if self._write else self._budget.read_sql_seconds
        await bounded_driver_call(transaction.__aenter__(), self._connection, seconds,
                                  self._budget.cleanup_seconds)
        try:
            milliseconds = max(1, int(self._budget.lock_seconds * 1000))
            await self.fetchval("SELECT set_config('lock_timeout', $1, true)",
                                f"{milliseconds}ms")
            yield transaction
        except BaseException as exc:
            try:
                await bounded_cleanup(
                    transaction.__aexit__(type(exc), exc, exc.__traceback__),
                    self._connection, self._budget.cleanup_seconds,
                )
            except Exception as cleanup_error:
                # Preserve the operation failure while discarding an unusable
                # connection. A terminated pool proxy may already be detached.
                logger.warning("Transaction rollback failed (%s); discarding connection",
                               type(cleanup_error).__name__)
                try:
                    self._connection.terminate()
                except Exception:
                    logger.debug("Connection already unavailable during rollback", exc_info=True)
            raise
        else:
            try:
                await bounded_driver_call(transaction.__aexit__(None, None, None),
                                          self._connection, seconds, self._budget.cleanup_seconds)
            except TimeoutError:
                raise


@asynccontextmanager
async def budgeted_pool_connection(
    pool: Any, budget: DatabaseBudget, *, write: bool = False,
) -> AsyncIterator[TimedDatabaseConnection]:
    """Bound pool acquire, statements, operation, lock waits and release.

    ``pool`` may also be a test/direct connection without an acquire method;
    its statements and transaction are still bounded.
    """

    lease = None
    connection = None
    token = _cleanup_deadline.set(None)
    try:
        if write:
            async with asyncio.timeout(budget.write_operation_seconds):
                if hasattr(pool, "acquire"):
                    seconds = budget.write_acquire_seconds
                    try:
                        lease = pool.acquire(timeout=seconds)
                    except TypeError:
                        lease = pool.acquire()
                    connection = await asyncio.wait_for(lease.__aenter__(), timeout=seconds)
                else:
                    connection = pool
                yield TimedDatabaseConnection(connection, budget, write=True)
        else:
            if hasattr(pool, "acquire"):
                seconds = budget.read_acquire_seconds
                try:
                    lease = pool.acquire(timeout=seconds)
                except TypeError:
                    lease = pool.acquire()
                connection = await asyncio.wait_for(lease.__aenter__(), timeout=seconds)
            else:
                connection = pool
            yield TimedDatabaseConnection(connection, budget, write=False)
    finally:
        if lease is not None and connection is not None:
            try:
                await release_pool_lease(pool, lease, connection, budget.cleanup_seconds)
            finally:
                _cleanup_deadline.reset(token)
        else:
            _cleanup_deadline.reset(token)
