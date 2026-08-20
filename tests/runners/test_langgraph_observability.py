from __future__ import annotations

from types import SimpleNamespace

import pytest

from ksadk.runners.langgraph_runner import LangGraphRunner


class _Chunk:
    content = "first token"
    additional_kwargs: dict = {}


class _ModelLifecycleAgent:
    async def astream_events(self, state, version="v2", config=None):
        yield {
            "event": "on_chat_model_start",
            "name": "demo-model",
            "run_id": "model-1",
            "data": {"input": state},
        }
        yield {
            "event": "on_chat_model_stream",
            "name": "demo-model",
            "run_id": "model-1",
            "data": {"chunk": _Chunk()},
        }
        yield {
            "event": "on_chat_model_end",
            "name": "demo-model",
            "run_id": "model-1",
            "data": {"output": SimpleNamespace(content="first token")},
        }

    def get_state(self, config):
        return SimpleNamespace(config=None)


@pytest.mark.asyncio
async def test_langgraph_stream_exposes_real_model_boundaries() -> None:
    detection = SimpleNamespace(entry_point="src/agent.py", agent_variable="root_agent")
    runner = LangGraphRunner(detection, ".")
    runner._agent = _ModelLifecycleAgent()

    chunks = [chunk async for chunk in runner.stream({"input": "hello", "session_id": "s"})]
    boundary_chunks = [
        chunk
        for chunk in chunks
        if chunk.get("type")
        in {
            "step_start",
            "model_call_begin",
            "model_call_first_token",
            "model_call_end",
            "step_end",
        }
    ]

    assert [chunk["type"] for chunk in boundary_chunks] == [
        "step_start",
        "model_call_begin",
        "model_call_first_token",
        "model_call_end",
        "step_end",
    ]
    assert {chunk["step_id"] for chunk in boundary_chunks} == {"step_model-1"}
    assert boundary_chunks[1]["model"] == "demo-model"
    assert boundary_chunks[2]["ttft_ms"] >= 0
    assert runner.get_runtime_capabilities()["model_call_boundaries"] is True
