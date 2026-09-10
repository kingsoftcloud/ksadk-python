"""共享 Checkpointer/状态装配 helper 的分档行为契约。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ksadk.harness.runtime_server import assemble_checkpoint_stack


@pytest.mark.asyncio
async def test_state_dir_assembles_durable_sqlite_stack(tmp_path: Path) -> None:
    stack = await assemble_checkpoint_stack(state_dir=tmp_path)

    try:
        assert stack.durable is True
        assert (tmp_path / "checkpoints.sqlite").exists()
        assert stack.run_store is not None
        assert stack.receipt_store is not None
        assert stack.receipt_store.path == tmp_path / "tool_receipts.sqlite"
    finally:
        await stack.aclose()


@pytest.mark.asyncio
async def test_no_state_dir_falls_back_to_memory() -> None:
    stack = await assemble_checkpoint_stack(state_dir=None)

    try:
        assert stack.durable is False
        assert stack.run_store is None
        assert stack.receipt_store is None
        from langgraph.checkpoint.memory import MemorySaver

        assert isinstance(stack.checkpointer, MemorySaver)
    finally:
        await stack.aclose()


@pytest.mark.asyncio
async def test_dsn_takes_precedence_and_uses_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    class _FakePostgresContext:
        async def __aenter__(self):
            captured["entered"] = True
            return object()

        async def __aexit__(self, *args):
            captured["exited"] = True
            return False

    def _fake_postgres(dsn: str):
        captured["dsn"] = dsn
        return _FakePostgresContext()

    monkeypatch.setattr(
        "ksadk.harness.engine.postgres_checkpointer.postgres_checkpointer",
        _fake_postgres,
    )
    monkeypatch.setenv("KSADK_CHECKPOINT_DSN", "postgresql://example/checkpoints")

    stack = await assemble_checkpoint_stack(state_dir=None)

    try:
        assert captured["dsn"] == "postgresql://example/checkpoints"
        assert captured["entered"] is True
        assert stack.durable is True
        assert stack.run_store is None  # 无 state_dir 时不建本地索引
    finally:
        await stack.aclose()
        assert captured["exited"] is True


@pytest.mark.asyncio
async def test_explicit_dsn_argument_overrides_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    class _FakePostgresContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(
        "ksadk.harness.engine.postgres_checkpointer.postgres_checkpointer",
        lambda dsn: captured.update(dsn=dsn) or _FakePostgresContext(),
    )
    monkeypatch.setenv("KSADK_CHECKPOINT_DSN", "postgresql://env/backend")

    stack = await assemble_checkpoint_stack(
        state_dir=tmp_path, dsn="postgresql://explicit/backend"
    )

    try:
        assert captured["dsn"] == "postgresql://explicit/backend"
        assert stack.run_store is not None  # state_dir 仍在时保留本地索引
    finally:
        await stack.aclose()
