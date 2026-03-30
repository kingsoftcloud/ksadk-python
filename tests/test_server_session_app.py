from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import httpx
import pytest

from ksadk.runners.base_runner import BaseRunner
from ksadk.server.api_models import AgentRunRequest
from ksadk.sessions.in_memory import InMemorySessionService


class _DummyRunner(BaseRunner):
    def __init__(self):
        super().__init__(
            detection_result=SimpleNamespace(
                name="demo-agent",
                type=SimpleNamespace(value="mock"),
            ),
            project_dir=".",
        )
        self.calls: list[dict] = []

    def load_agent(self) -> None:
        return None

    async def invoke(self, input_data: dict) -> dict:
        self.calls.append(input_data)
        return {"output": "assistant says hi"}

    async def stream(self, input_data: dict):
        yield {"type": "final", "output": "assistant says hi"}


class _OverrideStreamingRunner(BaseRunner):
    def __init__(self):
        super().__init__(
            detection_result=SimpleNamespace(
                name="demo-agent",
                type=SimpleNamespace(value="mock"),
            ),
            project_dir=".",
        )

    def load_agent(self) -> None:
        return None

    async def invoke(self, input_data: dict) -> dict:
        return {"output": "goodbye"}

    async def stream(self, input_data: dict):
        yield {"type": "text", "delta": "hel"}
        yield {"type": "text", "delta": "lo"}
        yield {"type": "final", "output": "goodbye"}


def _sse_payloads(response_text: str) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response_text.splitlines()
        if line.startswith("data: ")
    ]


@pytest.mark.asyncio
async def test_run_sse_uses_new_session_service(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _DummyRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/run_sse",
            json=AgentRunRequest(
                appName="demo-agent",
                userId="user-1",
                sessionId=None,
                newMessage={"role": "user", "parts": [{"text": "hello"}]},
                streaming=False,
                stateDelta={"topic": "billing"},
            ).model_dump(),
        )

    assert response.status_code == 200
    first_line = next(line for line in response.text.splitlines() if line.startswith("data: "))
    payload = json.loads(first_line.removeprefix("data: "))
    session_id = payload["sessionId"]

    session = await service.get_session(session_id)
    assert session is not None
    assert session.state == {"topic": "billing"}

    events = await service.get_events(session_id)
    assert [event.author for event in events] == ["user", "demo-agent"]
    assert events[0].content["parts"][0]["text"] == "hello"
    assert events[1].content["parts"][0]["text"] == "assistant says hi"

    assert runner.calls == [
        {
            "input": "hello",
            "history": [{"role": "user", "content": "hello"}],
        }
    ]


@pytest.mark.asyncio
async def test_create_session_rejects_explicit_session_owned_by_other_agent_or_user(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    await service.create_session(
        agent_id="other-agent",
        user_id="other-user",
        session_id="shared-session",
    )

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/apps/demo-agent/users/user-1/sessions",
            json={"sessionId": "shared-session"},
        )

    assert response.status_code == 409
    assert "different agent or user" in response.json()["detail"]


@pytest.mark.asyncio
async def test_run_sse_rejects_explicit_session_owned_by_other_agent_or_user(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _DummyRunner()
    await service.create_session(
        agent_id="other-agent",
        user_id="other-user",
        session_id="shared-session",
    )

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/run_sse",
            json=AgentRunRequest(
                appName="demo-agent",
                userId="user-1",
                sessionId="shared-session",
                newMessage={"role": "user", "parts": [{"text": "hello"}]},
                streaming=False,
            ).model_dump(),
        )

    assert response.status_code == 409
    assert "different agent or user" in response.json()["detail"]
    assert runner.calls == []


@pytest.mark.asyncio
async def test_run_sse_stream_emits_authoritative_final_event_when_output_overrides_partials(
    monkeypatch,
):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _OverrideStreamingRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/run_sse",
            json=AgentRunRequest(
                appName="demo-agent",
                userId="user-1",
                sessionId=None,
                newMessage={"role": "user", "parts": [{"text": "hello"}]},
                streaming=True,
            ).model_dump(),
        )

    assert response.status_code == 200
    payloads = _sse_payloads(response.text)
    assert [payload["content"]["parts"][0]["text"] for payload in payloads] == [
        "hel",
        "lo",
        "goodbye",
    ]
    assert payloads[0]["partial"] is True
    assert payloads[1]["partial"] is True
    assert "partial" not in payloads[2]

    session_id = payloads[0]["sessionId"]
    events = await service.get_events(session_id)
    assert [event.author for event in events] == ["user", "demo-agent"]
    assert events[-1].content["parts"][0]["text"] == "goodbye"
