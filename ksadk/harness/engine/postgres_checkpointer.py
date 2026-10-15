"""Postgres 异步 Checkpointer 工厂（plan §6 / 分布式后端）。

按 ``KSADK_CHECKPOINT_DSN`` 选择 sqlite/postgres。langgraph Postgres import
不越出本模块（架构守卫守卫 engine/ 包外不引 langgraph）。

依赖：``ksadk[postgres]`` extra（langgraph-checkpoint-postgres + psycopg）。
缺依赖时显式报错（不静默降级）。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from ksadk.sessions.db_budget import (
    DatabaseBudget,
    DatabaseOperationDeadline,
    session_db_budget,
)

logger = logging.getLogger(__name__)


class CheckpointCommitUnknown(RuntimeError):
    """A checkpoint write may have committed; never replay graph/tool work."""

    def __init__(self, thread_id: str, checkpoint_id: str, operation: str) -> None:
        self.thread_id = thread_id
        self.checkpoint_id = checkpoint_id
        self.operation = operation
        super().__init__(
            f"{operation} commit outcome unknown for thread={thread_id} checkpoint={checkpoint_id}"
        )


def _commit_outcome_uncertain(exc: Exception) -> bool:
    """Only transport/timeout failures can hide a successful COMMIT."""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        sqlstate = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if sqlstate and not str(sqlstate).startswith(("08", "57P", "58")):
            return False
        current = current.__cause__ or current.__context__
    return isinstance(exc, (TimeoutError, ConnectionError, OSError)) or type(exc).__name__ in {
        "OperationalError",
        "InterfaceError",
    }


def bounded_postgres_saver_class() -> Any:
    """Build a saver that bounds storage phases without timing graph execution."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    class BoundedAsyncPostgresSaver(AsyncPostgresSaver):
        def __init__(self, conn: AsyncConnectionPool, *, budget: DatabaseBudget | None = None):
            super().__init__(conn)
            self.db_budget = budget or session_db_budget()
            self._write_phase: ContextVar[bool] = ContextVar(
                "checkpoint_write_phase", default=False
            )
            self._active_connections: dict[asyncio.Task[Any], Any] = {}
            self._cleanup_deadlines: dict[asyncio.Task[Any], float] = {}

        async def _run_write(
            self, operation: Any, deadline: DatabaseOperationDeadline | None = None
        ) -> Any:
            seconds = (
                deadline.remaining_seconds()
                if deadline is not None
                else self.db_budget.write_operation_seconds
            )
            if seconds <= 0:
                if hasattr(operation, "close"):
                    operation.close()
                raise TimeoutError("checkpoint write operation deadline exceeded")
            token = self._write_phase.set(True)
            try:
                task = asyncio.create_task(operation)
            finally:
                self._write_phase.reset(token)
            try:
                return await asyncio.wait_for(asyncio.shield(task), seconds)
            except (asyncio.CancelledError, TimeoutError):
                cleanup_deadline = (
                    asyncio.get_running_loop().time() + self.db_budget.cleanup_seconds
                )
                self._cleanup_deadlines[task] = cleanup_deadline
                task.cancel()
                connection = self._active_connections.get(task)
                if connection is not None:
                    try:
                        remaining = max(0.0, cleanup_deadline - asyncio.get_running_loop().time())
                        if remaining:
                            await asyncio.wait_for(connection.close(), remaining)
                        else:
                            connection.pgconn.finish()
                    except Exception:
                        logger.exception("Could not close cancelled checkpoint connection")
                        connection.pgconn.finish()
                remaining = max(0.0, cleanup_deadline - asyncio.get_running_loop().time())
                done, _ = await asyncio.wait(
                    (task,), timeout=min(remaining, self.db_budget.cleanup_seconds / 2)
                )
                if not done:
                    task.cancel()
                    remaining = max(0.0, cleanup_deadline - asyncio.get_running_loop().time())
                    done, _ = await asyncio.wait((task,), timeout=remaining)
                if done:
                    await asyncio.gather(task, return_exceptions=True)
                else:
                    task.add_done_callback(
                        lambda finished: finished.exception() if not finished.cancelled() else None
                    )
                raise
            finally:
                self._cleanup_deadlines.pop(task, None)

        @asynccontextmanager
        async def _cursor(self, *, pipeline: bool = False):
            budget = self.db_budget
            write = pipeline or self._write_phase.get()
            acquire = budget.write_acquire_seconds if write else budget.read_acquire_seconds
            sql = budget.write_sql_seconds if write else budget.read_sql_seconds
            await asyncio.wait_for(self.lock.acquire(), budget.lock_seconds)
            try:
                async with self.conn.connection(timeout=acquire) as conn:
                    current = asyncio.current_task()
                    if current is not None:
                        self._active_connections[current] = conn
                    # Pool connections use autocommit. Restore session GUCs
                    # before returning them so reads never inherit write limits.
                    original_error: BaseException | None = None
                    configured = False
                    try:
                        await conn.execute(
                            f"SET statement_timeout TO '{max(1, int(sql * 1000))}ms'"
                        )
                        await conn.execute(
                            f"SET lock_timeout TO '{max(1, int(budget.lock_seconds * 1000))}ms'"
                        )
                        configured = True
                        if pipeline:
                            if self.supports_pipeline:
                                async with (
                                    conn.pipeline(),
                                    conn.cursor(binary=True, row_factory=dict_row) as cur,
                                ):
                                    yield cur
                            else:
                                async with (
                                    conn.transaction(),
                                    conn.cursor(binary=True, row_factory=dict_row) as cur,
                                ):
                                    yield cur
                        else:
                            async with conn.cursor(binary=True, row_factory=dict_row) as cur:
                                yield cur
                    except BaseException as exc:
                        original_error = exc
                        raise
                    finally:
                        try:
                            if not configured:
                                await conn.close()
                            elif not conn.closed:
                                deadline = self._cleanup_deadlines.get(current)
                                remaining = (
                                    max(0.001, deadline - asyncio.get_running_loop().time())
                                    if deadline is not None
                                    else budget.cleanup_seconds
                                )
                                async with asyncio.timeout(remaining):
                                    await conn.execute("RESET statement_timeout")
                                    await conn.execute("RESET lock_timeout")
                        except BaseException:
                            try:
                                await conn.close()
                            except Exception:
                                logger.exception(
                                    "Could not close checkpoint connection after reset failure"
                                )
                            if original_error is None:
                                raise
                            logger.warning(
                                "Checkpoint connection reset failed after operation error"
                            )
            finally:
                if current := asyncio.current_task():
                    self._active_connections.pop(current, None)
                self.lock.release()

        async def setup(self) -> None:
            await self._run_write(super().setup())

        async def aput(self, *args: Any, **kwargs: Any) -> Any:
            deadline = DatabaseOperationDeadline(self.db_budget)
            try:
                return await self._run_write(super().aput(*args, **kwargs), deadline)
            except Exception as exc:
                if not _commit_outcome_uncertain(exc):
                    raise
                config = args[0] if args else kwargs["config"]
                checkpoint = args[1] if len(args) > 1 else kwargs["checkpoint"]
                configurable = config["configurable"]
                receipt = {
                    "configurable": {
                        "thread_id": configurable["thread_id"],
                        "checkpoint_ns": configurable.get("checkpoint_ns", ""),
                        "checkpoint_id": checkpoint["id"],
                    }
                }
                try:
                    outcome = await deadline.run(self.reconcile_checkpoint_commit(receipt))
                except Exception:
                    outcome = "unknown"
                if outcome == "committed":
                    return receipt
                raise CheckpointCommitUnknown(
                    configurable["thread_id"], checkpoint["id"], "aput"
                ) from exc

        async def aput_writes(self, *args: Any, **kwargs: Any) -> Any:
            deadline = DatabaseOperationDeadline(self.db_budget)
            try:
                return await self._run_write(super().aput_writes(*args, **kwargs), deadline)
            except Exception as exc:
                if not _commit_outcome_uncertain(exc):
                    raise
                config = args[0] if args else kwargs["config"]
                writes = args[1] if len(args) > 1 else kwargs["writes"]
                task_id = args[2] if len(args) > 2 else kwargs["task_id"]
                try:
                    stored = await deadline.run(self.aget_tuple(config))
                except Exception:
                    stored = None
                if stored is not None and all(
                    (task_id, channel, value) in stored.pending_writes for channel, value in writes
                ):
                    return None
                address = config["configurable"]
                raise CheckpointCommitUnknown(
                    address["thread_id"], address.get("checkpoint_id", ""), "aput_writes"
                ) from exc

        async def adelete_thread(self, *args: Any, **kwargs: Any) -> Any:
            try:
                return await self._run_write(super().adelete_thread(*args, **kwargs))
            except Exception as exc:
                if not _commit_outcome_uncertain(exc):
                    raise
                thread_id = args[0] if args else kwargs["thread_id"]
                raise CheckpointCommitUnknown(thread_id, "", "adelete_thread") from exc

        async def aget_tuple(self, *args: Any, **kwargs: Any) -> Any:
            budget = self.db_budget
            async with asyncio.timeout(budget.read_acquire_seconds + budget.read_sql_seconds):
                return await super().aget_tuple(*args, **kwargs)

        async def alist(self, *args: Any, **kwargs: Any):
            # Upstream fetches all rows before yielding, but keeps its cursor
            # open while the caller consumes them. Materialize and close first
            # so consumer/Agent time is never part of a DB deadline.
            async with asyncio.timeout(
                self.db_budget.read_acquire_seconds + self.db_budget.read_sql_seconds
            ):
                items = [item async for item in super().alist(*args, **kwargs)]
            for item in items:
                yield item

        async def reconcile_checkpoint_commit(self, config: Any) -> str:
            """Confirm a stable checkpoint ID without replaying graph work."""
            checkpoint_id = str(config.get("configurable", {}).get("checkpoint_id") or "")
            if not checkpoint_id:
                return "unknown"
            try:
                stored = await self.aget_tuple(config)
            except Exception:
                return "unknown"
            if stored is not None and stored.checkpoint.get("id") == checkpoint_id:
                return "committed"
            return "unknown"

    return BoundedAsyncPostgresSaver


async def create_bounded_postgres_saver(
    dsn: str, *, budget: DatabaseBudget | None = None
) -> tuple[Any, Any]:
    """Open a bounded psycopg pool and return its saver and owned pool."""
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    budget = budget or session_db_budget()
    pool = AsyncConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=10,
        open=False,
        timeout=budget.write_acquire_seconds,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "connect_timeout": max(1, int(budget.connect_seconds + 0.999)),
            "options": f"-c statement_timeout={max(1, int(budget.write_sql_seconds * 1000))} "
            f"-c lock_timeout={max(1, int(budget.lock_seconds * 1000))}",
        },
    )
    try:
        await pool.open(wait=True, timeout=budget.connect_seconds)
        saver = bounded_postgres_saver_class()(pool, budget=budget)
        await saver.setup()
        return saver, pool
    except BaseException:
        await pool.close(timeout=budget.cleanup_seconds)
        raise


def postgres_checkpointer(dsn: str) -> Any:
    """Postgres 异步 Checkpointer 的上下文管理器（调用方负责 __aenter__）。

    经由本工厂获取，避免 langgraph import 越出引擎模块
    （tests/architecture/test_harness_contract.py 守卫）。

    Args:
        dsn: Postgres 连接串，如 ``postgresql://user:pass@host:5432/db``。
    """
    try:
        import langgraph.checkpoint.postgres.aio  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "Postgres Checkpointer 需要 ksadk[postgres] extra"
            "（langgraph-checkpoint-postgres + psycopg）；"
            "缺依赖时用 KSADK_CHECKPOINT_DSN 留空走 SQLite 默认。"
        ) from exc

    return _postgres_checkpointer_context(dsn)


@asynccontextmanager
async def _postgres_checkpointer_context(dsn: str) -> Any:
    saver, pool = await create_bounded_postgres_saver(dsn)
    try:
        yield saver
    finally:
        await pool.close(timeout=session_db_budget().cleanup_seconds)


def is_postgres_configured() -> bool:
    """检查是否配置了 Postgres Checkpoint（KSADK_CHECKPOINT_DSN 非空）。"""
    import os

    return bool(os.getenv("KSADK_CHECKPOINT_DSN", "").strip())


def resolve_checkpointer(checkpoint_path: str | None = None) -> Any:
    """按配置解析 Checkpointer：有 DSN → Postgres，否则 SQLite。

    Args:
        checkpoint_path: SQLite 路径（无 DSN 时使用）。
    """
    import os

    dsn = os.getenv("KSADK_CHECKPOINT_DSN", "").strip()
    if dsn:
        return postgres_checkpointer(dsn)
    from ksadk.harness.engine.langgraph import sqlite_checkpointer

    return sqlite_checkpointer(str(checkpoint_path or ":memory:"))


__all__ = [
    "is_postgres_configured",
    "postgres_checkpointer",
    "bounded_postgres_saver_class",
    "create_bounded_postgres_saver",
    "CheckpointCommitUnknown",
    "resolve_checkpointer",
]
