from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ksadk.runners.base_runner import BaseRunner


class _SlowBackgroundRunner(BaseRunner):
    """runner 的 stream 产慢流，用于验证 background 立即返回。"""

    def __init__(self):
        super().__init__(
            detection_result=SimpleNamespace(
                name="background-test-agent",
                description="bg",
                type=SimpleNamespace(value="langgraph"),
            ),
            project_dir=".",
        )
        self.stream_started = asyncio.Event()
        self.stream_finished = asyncio.Event()

    def load_agent(self) -> None:
        return None

    async def invoke(self, input_data: dict) -> dict:
        return {"output": "ok"}

    async def stream(self, input_data: dict):
        self.stream_started.set()
        await asyncio.sleep(0.3)
        yield {"type": "text", "delta": "hello"}
        yield {"type": "final", "output": "hello world"}
        self.stream_finished.set()


@pytest.fixture
def bg_client(monkeypatch, tmp_path):
    monkeypatch.setenv("KSADK_SESSION_BACKEND", "memory")
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / "ui"))
    from ksadk.server import app, set_runner

    runner = _SlowBackgroundRunner()
    set_runner(runner)
    yield TestClient(app), runner


def test_background_field_is_parsed(bg_client):
    """RunAgentActionRequest 能解析 Background 字段（默认 False）。"""
    client, _ = bg_client
    # Background=true 应被接受（不报 422 校验错误），后续 Task 才验证行为
    # 这里只验证字段存在且可解析——发一个最小请求触发校验
    resp = client.post(
        "/agentengine/api/v1/RunAgent",
        json={"AgentId": "a", "Messages": [{"role": "user", "content": "hi"}], "Background": True},
    )
    # 非 422 即说明 Background 字段被模型接受
    assert resp.status_code != 422
    # 正向断言：字段确已落在模型上（pydantic 默认忽略 extra，需直接校验属性可读）
    from ksadk.server.app import RunAgentActionRequest

    req = RunAgentActionRequest(AgentId="a", Messages=[], Background=True)
    assert hasattr(req, "Background"), "RunAgentActionRequest 缺 Background 字段"
    assert req.Background is True
    # 默认 False（向后兼容）
    assert RunAgentActionRequest(AgentId="a").Background is False

