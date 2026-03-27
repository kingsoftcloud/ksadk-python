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


@pytest.mark.asyncio
async def test_run_sse_uses_new_session_service(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _DummyRunner()

    monkeypatch.setattr(server_app_module, "get_session_service", lambda: service)
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
