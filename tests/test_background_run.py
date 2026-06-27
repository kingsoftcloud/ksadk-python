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


async def test_detached_stream_writes_completed_status_when_session_id_set(monkeypatch, tmp_path):
    """_DetachedSSEStream 持有 session_id 时，_consume 结束后写 run_status=completed 终态。"""
    monkeypatch.setenv("KSADK_SESSION_BACKEND", "memory")
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / "ui"))
    from ksadk.server.app import _DetachedSSEStream
    from ksadk.sessions import resolve_session_service

    async def source():
        yield "data: chunk1\n\n"
        yield "data: chunk2\n\n"

    invocation_id = "inv_test_completed"
    session_id = "sess_test_completed"
    service = resolve_session_service()
    await service.create_session(agent_id="a", user_id="u", session_id=session_id)
    detached = _DetachedSSEStream(source(), invocation_id=invocation_id, session_id=session_id)
    # 等后台 _consume 跑完
    await detached._task
    # 查 session 里的 run_status 事件
    events = await service.get_events(session_id)
    statuses = [e for e in events if e.event_type == "run_status"]
    assert any((e.content or {}).get("status") == "completed" for e in statuses), (
        f"期望 run_status=completed，实际 events: {[(e.event_type, e.content) for e in events]}"
    )

