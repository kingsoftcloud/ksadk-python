"""Postgres 异步 Checkpointer 工厂（plan §6 / 分布式后端）。

按 ``KSADK_CHECKPOINT_DSN`` 选择 sqlite/postgres。langgraph Postgres import
不越出本模块（架构守卫守卫 engine/ 包外不引 langgraph）。

依赖：``ksadk[postgres]`` extra（langgraph-checkpoint-postgres + psycopg）。
缺依赖时显式报错（不静默降级）。
"""

from __future__ import annotations

from typing import Any


def postgres_checkpointer(dsn: str) -> Any:
    """Postgres 异步 Checkpointer 的上下文管理器（调用方负责 __aenter__）。

    经由本工厂获取，避免 langgraph import 越出引擎模块
    （tests/architecture/test_harness_contract.py 守卫）。

    Args:
        dsn: Postgres 连接串，如 ``postgresql://user:pass@host:5432/db``。
    """
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError as exc:
        raise RuntimeError(
            "Postgres Checkpointer 需要 ksadk[postgres] extra"
            "（langgraph-checkpoint-postgres + psycopg）；"
            "缺依赖时用 KSADK_CHECKPOINT_DSN 留空走 SQLite 默认。"
        ) from exc

    return AsyncPostgresSaver.from_conn_string(dsn)


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
    "resolve_checkpointer",
]
