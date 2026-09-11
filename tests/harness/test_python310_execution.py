"""Exercise runtime branches with the Python 3.10 asyncio API surface."""

from __future__ import annotations

import asyncio

import pytest

from ksadk.harness.engine.run_control_bridge import invoke_graph_with_control
from ksadk.harness.run_control import RunController, RunControlSpec, RunControlStop, RunLimits
from ksadk.plugins.providers.harness_tools import assemble_python_tools


@pytest.mark.asyncio
async def test_builtin_executor_runs_without_asyncio_timeout(monkeypatch, tmp_path):
    monkeypatch.delattr(asyncio, "timeout", raising=False)
    workspace = tmp_path / "member"
    tools, approvals = assemble_python_tools(
        tmp_path,
        {
            "capabilities": {
                "tools": [{"name": "write_workspace_file", "executor": "builtin"}]
            }
        },
        workspace_root=workspace,
    )
    assert approvals == {"write_workspace_file"}
    await tools["write_workspace_file"].handler(
        {"path": "compat-proof.txt", "content": "real subprocess write"}, "native-call"
    )
    files = list(workspace.rglob("compat-proof.txt"))
    assert len(files) == 1
    assert files[0].read_text() == "real subprocess write"


@pytest.mark.asyncio
@pytest.mark.parametrize("exhausted", [False, True])
async def test_wall_budget_works_without_asyncio_timeout(monkeypatch, exhausted):
    monkeypatch.delattr(asyncio, "timeout", raising=False)
    canceled = asyncio.Event()

    class Graph:
        async def ainvoke(self, value, *, config):
            assert value == "input" and config == {"scope": "same-run"}
            if exhausted:
                try:
                    await asyncio.Event().wait()
                finally:
                    canceled.set()
            return "completed"

    controller = RunController(
        RunControlSpec(
            objective="bounded invocation", limits=RunLimits(max_wall_seconds=0.05)
        )
    )
    invocation = invoke_graph_with_control(
        Graph(), "input", {"scope": "same-run"}, controller
    )
    if exhausted:
        with pytest.raises(RunControlStop, match="wall_time_hard_limit"):
            await invocation
        assert controller.budget_exhausted
        assert canceled.is_set(), "timeout must finish cancelling the actual graph"
    else:
        assert await invocation == "completed"
        assert not controller.budget_exhausted
