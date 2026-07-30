from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from ksadk.a2a import A2ARuntimeTaskAdapter, add_a2a_protocol_routes
from ksadk.a2a.executor import A2ARuntimeExecutor
from ksadk.a2a.langgraph import stream_a2a_agent_to_writer
from ksadk.a2a.routes import A2AConfig
from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter


class _RecordingUpdater:
    def __init__(self) -> None:
        self.artifacts: list[dict[str, Any]] = []

    async def add_artifact(self, **kwargs: Any) -> None:
        self.artifacts.append(kwargs)


class _ReplacingRunner:
    async def stream(self, _runner_input: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "text", "delta": "旧答"}
        yield {"type": "text", "delta": "新答", "replace": True}
        yield {"type": "final", "output": "新答"}


@pytest.mark.asyncio
async def test_runner_text_replacement_is_marked_as_authoritative_snapshot() -> None:
    runner = _ReplacingRunner()
    executor = A2ARuntimeExecutor(runner=runner)
    updater = _RecordingUpdater()

    output = await executor._run_streaming(  # noqa: SLF001
        SimpleNamespace(task_id="task-1"),
        updater,  # type: ignore[arg-type]
        runner.stream,
        {},
    )

    assert output == "新答"
    assert [item["parts"][0].text for item in updater.artifacts] == ["旧答", "新答"]
    assert updater.artifacts[-1]["append"] is False
    assert updater.artifacts[-1]["parts"][0].metadata["ksadk_output_snapshot"] is True


@pytest.mark.asyncio
async def test_a2a_server_round_trip_preserves_text_replacement(tmp_path: Any) -> None:
    runner = _ReplacingRunner()
    app = FastAPI()
    add_a2a_protocol_routes(
        app,
        runner,
        A2AConfig(
            enabled=True,
            base_url="http://testserver",
            agent_name="replacing-agent",
            task_store_dsn=f"sqlite+aiosqlite:///{tmp_path}/tasks.db",
        ),
        task_adapter=A2ARuntimeTaskAdapter(
            RunnerRuntimeAdapter(runner, runtime_type="test"),  # type: ignore[arg-type]
            runtime_type="test",
        ),
    )
    written: list[dict[str, Any]] = []
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        output = await stream_a2a_agent_to_writer(
            "http://testserver",
            "问题",
            writer=written.append,
            httpx_client=client,
        )

    assert output == "新答"
    assert written == [
        {"type": "text", "delta": "旧答", "replace": False},
        {"type": "text", "delta": "新答", "replace": True},
    ]
