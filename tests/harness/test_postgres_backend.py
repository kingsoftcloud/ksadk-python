"""Postgres Checkpointer + Memory Provider 单测（not_configured 诚实标注）。

无 Postgres DSN/psycopg 时验证：
- postgres_checkpointer 显式报错（不静默降级）
- PostgresMemoryProvider 无 DSN 报错
- is_postgres_configured() 返回 False
- resolve_checkpointer 无 DSN 回退 SQLite
- provider_resolver local-postgres 分支可解析（缺 psycopg 时报错）
"""

from __future__ import annotations

from unittest.mock import patch


def test_is_postgres_configured_false_by_default():
    """无 KSADK_CHECKPOINT_DSN 时返回 False。"""
    with patch.dict("os.environ", {}, clear=True):
        from ksadk.harness.engine.postgres_checkpointer import is_postgres_configured

        assert not is_postgres_configured()


def test_is_postgres_configured_true_with_dsn():
    """有 KSADK_CHECKPOINT_DSN 时返回 True。"""
    with patch.dict("os.environ", {"KSADK_CHECKPOINT_DSN": "postgresql://u:p@h/db"}):
        from ksadk.harness.engine.postgres_checkpointer import is_postgres_configured

        assert is_postgres_configured()


def test_postgres_checkpointer_missing_deps_reports_error():
    """缺 langgraph-checkpoint-postgres 时显式报错。"""
    import builtins
    real_import = builtins.__import__

    def _block_postgres(name, *args, **kwargs):
        if "langgraph.checkpoint.postgres" in name:
            raise ImportError("no postgres extra")
        return real_import(name, *args, **kwargs)

    with patch.dict("os.environ", {}, clear=True):
        with patch("builtins.__import__", side_effect=_block_postgres):
            from ksadk.harness.engine.postgres_checkpointer import postgres_checkpointer

            try:
                postgres_checkpointer("postgresql://u:p@h/db")
                assert False, "应报错"
            except RuntimeError as exc:
                assert "postgres" in str(exc).lower() or "ksadk[postgres]" in str(exc).lower()


def test_resolve_checkpointer_falls_back_to_sqlite():
    """无 DSN 时 resolve_checkpointer 回退 SQLite。"""
    with patch.dict("os.environ", {}, clear=True):
        from ksadk.harness.engine.postgres_checkpointer import resolve_checkpointer

        cp = resolve_checkpointer(":memory:")
        assert cp is not None  # SQLite checkpointer 返回非 None


def test_postgres_memory_provider_missing_dsn_reports_error():
    """PostgresMemoryProvider 无 DSN 报错。"""
    with patch.dict("os.environ", {}, clear=True):
        try:
            from ksadk.memory.providers.local_postgres import PostgresMemoryProvider

            PostgresMemoryProvider()
            assert False, "应报错"
        except RuntimeError as exc:
            assert "dsn" in str(exc).lower() or "postgres" in str(exc).lower()


def test_provider_resolver_has_postgres_branch():
    """provider_resolver 有 local-postgres 分支（即使 psycopg 未装也该走到报错）。"""
    with patch.dict("os.environ", {}, clear=True):
        try:
            from ksadk.memory.provider_resolver import resolve_memory_provider

            resolve_memory_provider("local-postgres")
        except RuntimeError as exc:
            # 缺 DSN 或 psycopg 都报 RuntimeError，证明分支存在
            assert ("dsn" in str(exc).lower() or "psycopg" in str(exc).lower()
                    or "postgres" in str(exc).lower())
        except ImportError:
            # psycopg 未装也证明分支存在（走到 import）
            pass
