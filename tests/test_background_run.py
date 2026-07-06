from __future__ import annotations

import asyncio
import importlib
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


class _FailingBackgroundRunner(_SlowBackgroundRunner):
    async def stream(self, input_data: dict):
        self.stream_started.set()
        raise RuntimeError("background boom")
        yield  # pragma: no cover


@pytest.fixture
def bg_client(monkeypatch, tmp_path):
    monkeypatch.setenv("KSADK_SESSION_BACKEND", "memory")
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / "ui"))
    from ksadk.server import app, set_runner

    runner = _SlowBackgroundRunner()
    set_runner(runner)
    yield app, runner


@pytest.fixture
def failing_bg_client(monkeypatch, tmp_path):
    monkeypatch.setenv("KSADK_SESSION_BACKEND", "memory")
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / "ui"))
    from ksadk.server import app, set_runner

    runner = _FailingBackgroundRunner()
    set_runner(runner)
    yield app, runner


async def _run_statuses(session_id: str, invocation_id: str) -> list[str]:
    from ksadk.sessions import resolve_session_service

    events = await resolve_session_service().get_events(session_id)
    return [
        (event.content or {}).get("status")
        for event in events
        if event.event_type == "run_status" and event.invocation_id == invocation_id
    ]


async def _wait_for_terminal_statuses(session_id: str, invocation_id: str) -> list[str]:
    for _ in range(40):
        statuses = await _run_statuses(session_id, invocation_id)
        if _terminal_statuses(statuses):
            return statuses
        await asyncio.sleep(0.05)
    return await _run_statuses(session_id, invocation_id)


def _terminal_statuses(statuses: list[str]) -> list[str]:
    return [status for status in statuses if status in {"completed", "cancelled", "failed"}]


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
        # SessionId 顶层字段（与 SubscribeUrl 一致，避免前端从 URL 反解）
        assert "SessionId" in data and data["SessionId"]
        # 关键：响应返回时后台慢流（0.3s）还没跑完，证明是立即返回而非阻塞
        assert not runner.stream_finished.is_set(), "background 应立即返回，不该等 stream 完成"
        # InvocationId 落入 _DETACHED_STREAMS_BY_INVOCATION，CancelRun 能查到
        from ksadk.server.app import _DETACHED_STREAMS_BY_INVOCATION

        assert data["InvocationId"] in _DETACHED_STREAMS_BY_INVOCATION


@pytest.mark.asyncio
async def test_run_agent_background_primes_session_title_before_detached_stream_consumes(bg_client, monkeypatch):
    """Background=true 返回 job 句柄前先写入首轮 prompt/title，刷新列表不显示空标题。"""
    server_app_module = importlib.import_module("ksadk.server.app")
    from ksadk.sessions import resolve_session_service

    class _IdleDetachedStream:
        def __init__(self, source, *, invocation_id=None, session_id=None):
            self.source = source
            self.invocation_id = invocation_id
            self.session_id = session_id
            self._task = asyncio.Future()

    monkeypatch.setattr(server_app_module, "_DetachedSSEStream", _IdleDetachedStream)

    app, _runner = bg_client
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        resp = await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "a",
                "SessionId": "sess-bg-title",
                "ResponsesInput": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "调研 2026 企业 AI Agent 平台趋势"}],
                    }
                ],
                "ApiFormat": "responses",
                "Background": True,
                "Stream": False,
            },
        )
        assert resp.status_code == 200, resp.text
        invocation_id = resp.json()["Data"]["InvocationId"]
        listed = await client.post(
            "/agentengine/api/v1/ListSessions",
            json={"AgentId": "a"},
        )
        assert listed.status_code == 200, listed.text

    session = await resolve_session_service().get_session("sess-bg-title")
    assert session is not None
    assert session.first_prompt == "调研 2026 企业 AI Agent 平台趋势"
    assert session.last_prompt == "调研 2026 企业 AI Agent 平台趋势"
    assert session.title
    assert session.title != "sess-bg-title"
    assert session.title_source == "fallback_first_prompt"
    listed_session = listed.json()["Data"]["Sessions"][0]
    assert listed_session["FirstPrompt"] == "调研 2026 企业 AI Agent 平台趋势"
    assert listed_session["ActiveInvocationId"] == invocation_id
    assert listed_session["ActiveRunStatus"] == "in_progress"


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
    assert _terminal_statuses(await _run_statuses("sess-bg-2", invocation_id)) == ["completed"]


@pytest.mark.asyncio
async def test_run_agent_background_writes_single_in_progress_status(bg_client):
    """Background 起始态只由 conversation runtime 写一次，避免刷新/订阅看到重复 running。"""
    app, runner = bg_client
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        resp = await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "a",
                "SessionId": "sess-bg-single-start",
                "Messages": [{"role": "user", "content": "研究 X"}],
                "Background": True,
                "Stream": False,
            },
        )
        assert resp.status_code == 200, resp.text
        invocation_id = resp.json()["Data"]["InvocationId"]
        await runner.stream_finished.wait()

    statuses = await _run_statuses("sess-bg-single-start", invocation_id)
    assert statuses.count("in_progress") == 1, (
        f"期望同一 InvocationId 只有一个 in_progress，实际 statuses: {statuses}"
    )
    assert _terminal_statuses(statuses) == ["completed"]


async def test_detached_stream_does_not_write_duplicate_completed_status(monkeypatch, tmp_path):
    """_DetachedSSEStream 正常结束时不补写 completed，终态由 conversation stream 主写入。"""
    monkeypatch.setenv("KSADK_SESSION_BACKEND", "memory")
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / "ui"))
    from ksadk.server.app import _DetachedSSEStream
    from ksadk.conversations import append_run_status_event
    from ksadk.sessions import resolve_session_service

    async def source():
        await append_run_status_event(
            session_id=session_id,
            author="runner",
            status="in_progress",
            invocation_id=invocation_id,
        )
        yield "data: chunk1\n\n"
        await append_run_status_event(
            session_id=session_id,
            author="runner",
            status="completed",
            invocation_id=invocation_id,
        )
        yield "data: chunk2\n\n"

    invocation_id = "inv_test_completed"
    session_id = "sess_test_completed"
    service = resolve_session_service()
    await service.create_session(agent_id="a", user_id="u", session_id=session_id)
    detached = _DetachedSSEStream(source(), invocation_id=invocation_id, session_id=session_id)
    # 等后台 _consume 跑完
    await detached._task
    # 查 session 里的 run_status 事件
    statuses = await _run_statuses(session_id, invocation_id)
    assert _terminal_statuses(statuses) == ["completed"], (
        f"期望只有 conversation stream 写入一个 completed，实际 statuses: {statuses}"
    )


async def test_detached_stream_writes_failed_fallback_only_when_source_raises(monkeypatch, tmp_path):
    """_DetachedSSEStream 只在源流异常且没有已有终态时兜底写 failed。"""
    monkeypatch.setenv("KSADK_SESSION_BACKEND", "memory")
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / "ui"))
    from ksadk.server.app import _DetachedSSEStream
    from ksadk.sessions import resolve_session_service

    async def source():
        yield "data: chunk1\n\n"
        raise RuntimeError("raw stream failed")

    invocation_id = "inv_test_raw_failed"
    session_id = "sess_test_raw_failed"
    service = resolve_session_service()
    await service.create_session(agent_id="a", user_id="u", session_id=session_id)
    detached = _DetachedSSEStream(source(), invocation_id=invocation_id, session_id=session_id)

    with pytest.raises(RuntimeError, match="raw stream failed"):
        await detached._task

    statuses = await _run_statuses(session_id, invocation_id)
    assert _terminal_statuses(statuses) == ["failed"], (
        f"期望 detached 异常兜底只写一个 failed，实际 statuses: {statuses}"
    )


@pytest.mark.asyncio
async def test_run_agent_background_lifecycle_does_not_create_checkpoints(bg_client):
    """background lifecycle run_status 不应被 ListSessionCheckpoints 当成恢复点。"""
    app, runner = bg_client
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        resp = await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "a",
                "SessionId": "sess-bg-no-checkpoints",
                "Messages": [{"role": "user", "content": "研究 X"}],
                "Background": True,
                "Stream": False,
            },
        )
        assert resp.status_code == 200, resp.text
        await runner.stream_finished.wait()
        checkpoints_resp = await client.post(
            "/agentengine/api/v1/ListSessionCheckpoints",
            json={"AgentId": "a", "SessionId": "sess-bg-no-checkpoints"},
        )

    assert checkpoints_resp.status_code == 200, checkpoints_resp.text
    checkpoints_data = checkpoints_resp.json()["Data"]
    assert checkpoints_data["Checkpoints"] == []
    assert checkpoints_data["Total"] == 0


@pytest.mark.asyncio
async def test_run_agent_background_cancel_writes_cancelled_status(bg_client):
    """CancelRun 对 background 任务生效，写 run_status=cancelled 终态。"""
    from ksadk.server.app import _DETACHED_STREAMS_BY_INVOCATION
    from ksadk.sessions import resolve_session_service

    app, runner = bg_client
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 起 background 任务（慢流 0.3s，cancel 前后台还在 sleep）
        resp = await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "a",
                "SessionId": "sess-bg-cancel",
                "Messages": [{"role": "user", "content": "研究 X"}],
                "Background": True,
                "Stream": False,
            },
        )
        assert resp.status_code == 200, resp.text
        invocation_id = resp.json()["Data"]["InvocationId"]
        assert invocation_id in _DETACHED_STREAMS_BY_INVOCATION
        # CancelRun：detached 已注册进 dict，开箱即用
        cancel_resp = await client.post(
            "/agentengine/api/v1/CancelRun",
            json={"InvocationId": invocation_id},
        )
        cancel_data = cancel_resp.json()["Data"]
        assert cancel_data["Found"] is True
        # 等 detached task 处理取消 + finally 写终态
        detached = _DETACHED_STREAMS_BY_INVOCATION.get(invocation_id)
        if detached is not None:
            try:
                await detached._task
            except Exception:
                pass
    # 查 session 里的 run_status 事件
    service = resolve_session_service()
    events = await service.get_events("sess-bg-cancel")
    statuses = [
        (e.content or {}).get("status")
        for e in events
        if e.event_type == "run_status" and e.invocation_id == invocation_id
    ]
    assert _terminal_statuses(statuses) == ["cancelled"], (
        f"期望同一 InvocationId 只有一个 cancelled 终态，实际 statuses: {statuses}"
    )


@pytest.mark.asyncio
async def test_run_agent_background_failure_writes_single_terminal_status(failing_bg_client):
    """background stream 失败时，同一 InvocationId 只有一个 failed 终态。"""
    app, _runner = failing_bg_client
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "a",
                "SessionId": "sess-bg-failed",
                "Messages": [{"role": "user", "content": "研究 X"}],
                "Background": True,
                "Stream": False,
            },
        )
        assert resp.status_code == 200, resp.text
        invocation_id = resp.json()["Data"]["InvocationId"]

    statuses = await _wait_for_terminal_statuses("sess-bg-failed", invocation_id)
    assert _terminal_statuses(statuses) == ["failed"], (
        f"期望同一 InvocationId 只有一个 failed 终态，实际 statuses: {statuses}"
    )


@pytest.mark.asyncio
async def test_run_agent_background_false_preserves_existing_behavior(bg_client):
    """Background 不传（默认 false）+ Stream=false 走现有同步 invoke 路径，行为不变。"""
    app, runner = bg_client
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "a",
                "SessionId": "sess-bg-compat",
                "Messages": [{"role": "user", "content": "普通问题"}],
                # 不传 Background（默认 False），Stream=False
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["Data"]
        # 同步 invoke 路径返回普通 payload（含 output），不含 background 句柄字段
        assert "output" in data, f"同步路径应返回 output，实际 Data: {data}"
        assert data.get("Background") is not True, "非 background 路径不该返回 Background:true"
        assert data.get("Status") != "running", "非 background 路径不该返回 Status:running"
