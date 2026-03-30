from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from click.testing import CliRunner

from ksadk.runners.base_runner import BaseRunner
from ksadk.sessions.in_memory import InMemorySessionService


class _UiRunner(BaseRunner):
    def __init__(self):
        super().__init__(
            detection_result=SimpleNamespace(
                name="demo-agent",
                description="demo agent",
                type=SimpleNamespace(value="langgraph"),
            ),
            project_dir=".",
        )
        self.invocations: list[dict] = []
        self.run_server_calls: list[int] = []

    def load_agent(self) -> None:
        return None

    async def invoke(self, input_data: dict) -> dict:
        self.invocations.append(input_data)
        return {"output": "assistant says hi"}

    async def stream(self, input_data: dict):
        self.invocations.append(input_data)
        yield {"type": "thinking", "delta": "plan"}
        yield {"type": "text", "delta": "hello"}
        yield {"type": "final", "output": "hello world"}

    def run_server(self, port: int = 8000) -> None:
        self.run_server_calls.append(port)


def _build_transport(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _UiRunner()
    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)
    transport = httpx.ASGITransport(app=server_app_module.app)
    return server_app_module, runner, service, transport


@pytest.mark.asyncio
async def test_get_agent_ui_bootstrap_returns_modules_and_capabilities(monkeypatch):
    _, _, _, transport = _build_transport(monkeypatch)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/GetAgentUiBootstrap",
            json={"AgentId": "demo-agent"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["Code"] == 0
    assert payload["Data"]["Agent"]["AgentId"] == "demo-agent"
    assert payload["Data"]["Modules"] == ["Chat", "Build", "Deploy"]
    assert payload["Data"]["Capabilities"]["Thinking"] is True
    assert payload["Data"]["Capabilities"]["Attachments"] is True


@pytest.mark.asyncio
async def test_run_agent_action_returns_responses_payload_and_persists_session(monkeypatch):
    _, runner, service, transport = _build_transport(monkeypatch)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "demo-agent",
                "Messages": [{"role": "user", "content": "hello"}],
                "ApiFormat": "responses",
                "Stream": False,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["Code"] == 0
    assert payload["Data"]["object"] == "response"
    assert payload["Data"]["status"] == "completed"
    assert payload["Data"]["output_text"] == "assistant says hi"

    session_id = payload["Data"]["session_id"]
    session = await service.get_session(session_id)
    assert session is not None
    events = await service.get_events(session_id)
    assert [event.author for event in events] == ["user", "demo-agent"]
    assert runner.invocations[-1]["history"] == [{"role": "user", "content": "hello"}]


@pytest.mark.asyncio
async def test_session_kop_actions_crud_and_event_listing(monkeypatch):
    _, _, _, transport = _build_transport(monkeypatch)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        created = await client.post(
            "/agentengine/api/v1/CreateSession",
            json={"AgentId": "demo-agent", "UserId": "user-1"},
        )
        session_id = created.json()["Data"]["Session"]["SessionId"]

        await client.post(
            "/agentengine/api/v1/RunAgent",
            json={
                "AgentId": "demo-agent",
                "Messages": [{"role": "user", "content": "hello"}],
                "SessionId": session_id,
                "ApiFormat": "responses",
            },
        )

        listed = await client.post(
            "/agentengine/api/v1/ListSessions",
            json={"AgentId": "demo-agent", "UserId": "user-1"},
        )
        fetched = await client.post(
            "/agentengine/api/v1/GetSession",
            json={"SessionId": session_id},
        )
        events = await client.post(
            "/agentengine/api/v1/ListSessionEvents",
            json={"SessionId": session_id},
        )
        deleted = await client.post(
            "/agentengine/api/v1/DeleteSession",
            json={"SessionId": session_id},
        )

    assert created.status_code == 200
    assert listed.status_code == 200
    assert fetched.status_code == 200
    assert events.status_code == 200
    assert deleted.status_code == 200
    assert [item["SessionId"] for item in listed.json()["Data"]["Sessions"]] == [session_id]
    assert fetched.json()["Data"]["Session"]["SessionId"] == session_id
    assert [item["Author"] for item in events.json()["Data"]["Events"]] == ["user", "demo-agent"]
    assert deleted.json()["Data"]["Deleted"] is True


@pytest.mark.asyncio
async def test_responses_endpoint_streams_thinking_and_text_events(monkeypatch):
    _, _, _, transport = _build_transport(monkeypatch)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/v1/responses",
            json={
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
                "stream": True,
            },
        )

    assert response.status_code == 200
    lines = [line for line in response.text.splitlines() if line.startswith("event: ")]
    assert "event: response.reasoning.delta" in lines
    assert "event: response.output_text.delta" in lines
    assert "event: response.completed" in lines


def test_cmd_web_launches_unified_local_server(monkeypatch, tmp_path):
    runner = CliRunner()
    fake_runner = _UiRunner()
    project_dir = tmp_path / "demo-agent"
    project_dir.mkdir()

    class _Detector:
        def __init__(self, path: str):
            self.path = path

        def detect(self):
            return SimpleNamespace(
                type=SimpleNamespace(value="langgraph"),
                name="demo-agent",
                entry_point="agent.py",
            )

    import ksadk.cli.cmd_web as cmd_web_module

    monkeypatch.setattr(cmd_web_module, "FrameworkDetector", _Detector, raising=False)
    monkeypatch.setattr(cmd_web_module, "setup_environment", lambda path: None, raising=False)
    monkeypatch.setattr(
        "ksadk.runners.unified_runner.UnifiedRunner.create",
        lambda result, project_dir: fake_runner,
        raising=False,
    )
    monkeypatch.chdir(project_dir)

    result = runner.invoke(cmd_web_module.web, [str(project_dir), "--port", "8899"])

    assert result.exit_code == 0, result.output
    assert fake_runner.run_server_calls == [8899]
    assert "Chainlit" not in result.output


@pytest.mark.asyncio
async def test_static_routes_serve_unified_agent_ui_shell(monkeypatch):
    _, _, _, transport = _build_transport(monkeypatch)

    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        root_response = await client.get("/")
        chat_response = await client.get("/chat")

    assert root_response.status_code == 200
    assert chat_response.status_code == 200
    assert "Agent Workbench" in root_response.text
    assert "window.__AGENTENGINE_UI__" in root_response.text
    assert "window.__AGENTENGINE_UI__" in chat_response.text
