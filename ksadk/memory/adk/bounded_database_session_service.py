"""PostgreSQL phase budgets around ADK's native transactional session service."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from sqlalchemy import event, text

from ksadk.compat.adk_compat import DatabaseSessionService
from ksadk.sessions.db_budget import DatabaseBudget, session_db_budget

logger = logging.getLogger(__name__)


class ADKSessionLockTimeout(RuntimeError):
    """Local session serialization is busy; PostgreSQL availability is unknown."""

    sqlstate = "55P03"


class BoundedADKDatabaseSessionService(DatabaseSessionService):
    """Keep ADK's transaction logic while bounding its database phases.

    A cancelled call is never replayed.  In particular, a timed-out commit may
    have succeeded; callers must reconcile its stable session/event ID.
    """

    def __init__(self, *, db_url: str, budget: DatabaseBudget | None = None) -> None:
        self.db_budget = budget or session_db_budget()
        self._active_connections: dict[asyncio.Task[Any], Any] = {}
        self._checked_out_drivers: dict[asyncio.Task[Any], set[Any]] = {}
        self._driver_owners: dict[int, tuple[asyncio.Task[Any], Any]] = {}
        phase = self.db_budget
        super().__init__(
            db_url=db_url,
            pool_timeout=phase.write_acquire_seconds,
            connect_args={
                "timeout": phase.connect_seconds,
                "command_timeout": phase.write_sql_seconds,
                "server_settings": {
                    "statement_timeout": str(max(1, int(phase.write_sql_seconds * 1000))),
                    "lock_timeout": str(max(1, int(phase.lock_seconds * 1000))),
                },
            },
        )
        event.listen(self.db_engine.sync_engine, "checkout", self._on_checkout)
        event.listen(self.db_engine.sync_engine, "checkin", self._on_checkin)

    def _on_checkout(self, dbapi_connection: Any, _record: Any, _proxy: Any) -> None:
        task = asyncio.current_task()
        driver = getattr(dbapi_connection, "driver_connection", None)
        if task is not None and driver is not None:
            self._driver_owners[id(_record)] = (task, driver)
            self._checked_out_drivers.setdefault(task, set()).add(driver)

    def _on_checkin(self, dbapi_connection: Any, _record: Any) -> None:
        owned = self._driver_owners.pop(id(_record), None)
        if owned is not None:
            task, driver = owned
            drivers = self._checked_out_drivers.get(task)
            if drivers is not None:
                drivers.discard(driver)
                if not drivers:
                    self._checked_out_drivers.pop(task, None)

    async def _bounded(self, operation: Any, seconds: float) -> Any:
        task = asyncio.create_task(operation)
        try:
            return await asyncio.wait_for(asyncio.shield(task), seconds)
        except (asyncio.CancelledError, TimeoutError):
            deadline = asyncio.get_running_loop().time() + self.db_budget.cleanup_seconds
            task.cancel()
            connection = self._active_connections.get(task)
            # asyncpg terminate is synchronous and closes the server-side
            # transaction, including prepare_tables' direct engine checkout.
            drivers = tuple(self._checked_out_drivers.get(task, ()))
            if not drivers and connection is not None:
                try:
                    drivers = (connection.sync_connection.connection.driver_connection,)
                except Exception:
                    pass
            terminated = False
            for driver in drivers:
                try:
                    driver.terminate()
                    terminated = True
                except Exception:
                    logger.exception("Could not terminate cancelled ADK DB connection")
            if connection is not None and not terminated:
                try:
                    remaining = max(0.0, deadline - asyncio.get_running_loop().time())
                    if remaining:
                        await asyncio.wait_for(connection.invalidate(), remaining)
                except Exception:
                    logger.exception("Could not invalidate cancelled ADK DB connection")
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            done, _ = await asyncio.wait(
                (task,), timeout=min(remaining, self.db_budget.cleanup_seconds / 2)
            )
            if not done:
                task.cancel()
                remaining = max(0.0, deadline - asyncio.get_running_loop().time())
                done, _ = await asyncio.wait((task,), timeout=remaining)
            if done:
                await asyncio.gather(task, return_exceptions=True)
            else:
                if connection is not None:
                    try:
                        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
                        if remaining:
                            await asyncio.wait_for(connection.invalidate(), remaining)
                    except Exception:
                        logger.exception("Could not invalidate cancelled ADK DB connection")
                # A pending connection attempt has no checked-out socket to
                # terminate; retire the pool and consume the late task result.
                if connection is None:
                    remaining = max(0.0, deadline - asyncio.get_running_loop().time())
                    if remaining:
                        try:
                            await asyncio.wait_for(self.db_engine.dispose(close=False), remaining)
                        except Exception:
                            logger.exception("Could not retire cancelled ADK DB pool")
                task.add_done_callback(
                    lambda finished: finished.exception() if not finished.cancelled() else None
                )
            raise

    @asynccontextmanager
    async def _rollback_on_exception_session(
        self, *, read_only: bool = False
    ) -> AsyncIterator[Any]:
        factory = self._get_database_session_factory(read_only=read_only)
        async with factory() as sql_session:
            try:
                acquire = (
                    self.db_budget.read_acquire_seconds
                    if read_only
                    else self.db_budget.write_acquire_seconds
                )
                async with asyncio.timeout(acquire):
                    connection = await sql_session.connection()
                current = asyncio.current_task()
                if current is not None:
                    self._active_connections[current] = connection
                sql_seconds = (
                    self.db_budget.read_sql_seconds
                    if read_only
                    else self.db_budget.write_sql_seconds
                )
                # SET LOCAL shares ADK's transaction, so it cannot split an
                # event/state update or leak limits to the next pool borrower.
                await sql_session.execute(
                    text("SELECT set_config('statement_timeout', :milliseconds, true)"),
                    {"milliseconds": str(max(1, int(sql_seconds * 1000)))},
                )
                await sql_session.execute(
                    text("SELECT set_config('lock_timeout', :milliseconds, true)"),
                    {"milliseconds": str(max(1, int(self.db_budget.lock_seconds * 1000)))},
                )
                yield sql_session
            except BaseException:
                await sql_session.rollback()
                raise
            finally:
                if current := asyncio.current_task():
                    self._active_connections.pop(current, None)

    @asynccontextmanager
    async def _with_session_lock(self, **kwargs: Any) -> AsyncIterator[None]:
        context = super()._with_session_lock(**kwargs)
        try:
            await asyncio.wait_for(context.__aenter__(), self.db_budget.lock_seconds)
        except TimeoutError as exc:
            raise ADKSessionLockTimeout("ADK session append lock timed out") from exc
        try:
            yield
        except BaseException as exc:
            await context.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            await context.__aexit__(None, None, None)

    async def create_session(self, **kwargs: Any) -> Any:
        return await self._bounded(
            super().create_session(**kwargs), self.db_budget.write_operation_seconds
        )

    async def get_session(self, **kwargs: Any) -> Any:
        budget = (
            self.db_budget.write_operation_seconds
            if not self._tables_created
            else self.db_budget.read_acquire_seconds + self.db_budget.read_sql_seconds
        )
        return await self._bounded(
            super().get_session(**kwargs), budget,
        )

    async def list_sessions(self, **kwargs: Any) -> Any:
        budget = (
            self.db_budget.write_operation_seconds
            if not self._tables_created
            else self.db_budget.read_acquire_seconds + self.db_budget.read_sql_seconds
        )
        return await self._bounded(
            super().list_sessions(**kwargs), budget,
        )

    async def get_user_state(self, **kwargs: Any) -> Any:
        budget = (
            self.db_budget.write_operation_seconds
            if not self._tables_created
            else self.db_budget.read_acquire_seconds + self.db_budget.read_sql_seconds
        )
        return await self._bounded(
            super().get_user_state(**kwargs), budget,
        )

    async def delete_session(self, *args: Any, **kwargs: Any) -> None:
        await self._bounded(
            super().delete_session(*args, **kwargs), self.db_budget.write_operation_seconds
        )

    async def append_event(self, *args: Any, **kwargs: Any) -> Any:
        return await self._bounded(
            super().append_event(*args, **kwargs), self.db_budget.write_operation_seconds
        )

    async def close(self) -> None:
        await self.db_engine.dispose()
