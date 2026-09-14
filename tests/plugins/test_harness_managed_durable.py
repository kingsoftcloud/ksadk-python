"""DSH Provider Managed 装配的持久 Checkpoint 分档契约。"""

from __future__ import annotations

from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver

from ksadk.harness.config import HarnessConfig
from ksadk.harness.reasoner import HarnessReasoningTurn
from ksadk.plugins.providers.harness_managed import build_managed_provider_adapter


class _Reasoner:
    async def complete(self, **_kwargs):
        return HarnessReasoningTurn(final_text="ok")


def _config() -> HarnessConfig:
    return HarnessConfig(model="glm-5.2", prompt="provider assistant")


@pytest.mark.asyncio
async def test_provider_adapter_assembles_durable_sqlite_stack(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    adapter = await build_managed_provider_adapter(
        _config(),
        agent_name="durable-agent",
        workspace_root=tmp_path,
        reasoner=_Reasoner(),
        state_dir=state_dir,
    )

    assert (state_dir / "checkpoints.sqlite").exists()
    assert (state_dir / "tool_receipts.sqlite").exists()
    assert (state_dir / "runs.json").exists() or state_dir.exists()
    assert not isinstance(adapter._engine._checkpointer, MemorySaver)
    matrix = adapter._engine.capabilities()
    assert matrix.durable_across_process.supported is True
    adapter_matrix = adapter.capabilities()
    assert adapter_matrix.attach.supported is True
    assert adapter_matrix.durable_restore.supported is True
    assert adapter.runtime.native_capabilities()["session_continuity"]["durable"] is True


@pytest.mark.asyncio
async def test_provider_adapter_without_state_falls_back_to_memory(tmp_path: Path) -> None:
    adapter = await build_managed_provider_adapter(
        _config(),
        agent_name="memory-agent",
        workspace_root=tmp_path,
        reasoner=_Reasoner(),
    )

    assert isinstance(adapter._engine._checkpointer, MemorySaver)
    matrix = adapter._engine.capabilities()
    assert matrix.durable_across_process.supported is False
    adapter_matrix = adapter.capabilities()
    assert adapter_matrix.attach.supported is False
    assert adapter_matrix.durable_restore.supported is False
