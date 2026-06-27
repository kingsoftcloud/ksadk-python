from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport

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
    yield app, runner


def test_background_field_is_parsed(bg_client):
    """RunAgentActionRequest 能解析 Background 字段（默认 False）。"""
    app, _ = bg_client
    client = TestClient(app)
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


@pytest.mark.asyncio
async def test_run_agent_background_returns_immediately_with_job_handle(bg_client):
    """Background=true 立即返回 job 句柄，不等后台 stream 跑完。"""
    app, runner = bg_client
    # 用 ASGITransport + AsyncClient 而非同步 TestClient：同步 TestClient 在
    # 请求返回后即拆除事件循环，asyncio.create_task 的 detached 后台任务无法存活
    # （会在 sleep 中被取消）。async client 让事件循环持续驱动 detached _consume。
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        resp = await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "a",
                "SessionId": "sess-bg-1",
                "Messages": [{"role": "user", "content": "研究 AI Agent 趋势"}],
                "Background": True,
                "Stream": False,
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["Data"]
        # 立即返回 running 状态（后台 stream 还在跑，stream_finished 未 set）
        assert data["Status"] == "running"
        assert data["Background"] is True
        assert "InvocationId" in data and data["InvocationId"]
        # 关键：响应返回时后台慢流（0.3s）还没跑完，证明是立即返回而非阻塞
        assert not runner.stream_finished.is_set(), "background 应立即返回，不该等 stream 完成"
        # InvocationId 落入 _DETACHED_STREAMS_BY_INVOCATION，CancelRun 能查到
        from ksadk.server.app import _DETACHED_STREAMS_BY_INVOCATION

        assert data["InvocationId"] in _DETACHED_STREAMS_BY_INVOCATION


@pytest.mark.asyncio
async def test_run_agent_background_subscribe_gets_terminal_status(bg_client):
    """background 起任务后，SubscribeRunEvents 拉到 run_status 终态 + [DONE]。"""
    app, runner = bg_client
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        resp = await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "a",
                "SessionId": "sess-bg-2",
                "Messages": [{"role": "user", "content": "研究 X"}],
                "Background": True,
                "Stream": False,
            },
        )
        invocation_id = resp.json()["Data"]["InvocationId"]
        # 等后台慢流（0.3s）跑完写终态，再 subscribe（SubscribeRunEvents 也支持
        # 后到也能拉到历史 run_status 事件，但先等终态写入更稳）
        await runner.stream_finished.wait()
        # 用流式 GET 拉 SubscribeRunEvents，读到 [DONE] 终止符
        chunks: list[str] = []
        async with client.stream(
            "GET",
            f"/agentengine/api/v1/SubscribeRunEvents?SessionId=sess-bg-2&InvocationId={invocation_id}",
        ) as response:
            async for line in response.aiter_lines():
                chunks.append(line)
                if "[DONE]" in line:
                    break
    body = "\n".join(chunks)
    assert "completed" in body or "run_status" in body, f"期望 run_status 终态，实际: {body[:500]}"
    assert "[DONE]" in body, "期望 [DONE] 终止符"


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
