from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ksadk.harness import tools as harness_tools
from ksadk.harness.config import McpToolSpec


@pytest.mark.asyncio
async def test_cancelled_mcp_discovery_closes_the_partial_toolset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery_started = asyncio.Event()
    closed = asyncio.Event()

    class Toolset:
        async def get_tools_with_prefix(self) -> list[Any]:
            discovery_started.set()
            await asyncio.Event().wait()
            return []

        async def close(self) -> None:
            closed.set()

    monkeypatch.setattr(harness_tools, "build_mcp_toolset", lambda _config: Toolset())
    loading = asyncio.create_task(
        harness_tools.load_mcp_tools(
            McpToolSpec(name="fixture", url="http://127.0.0.1:9/mcp")
        )
    )
    await discovery_started.wait()
    loading.cancel()

    with pytest.raises(asyncio.CancelledError):
        await loading
    assert closed.is_set()
