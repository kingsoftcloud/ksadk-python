from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

import httpx
import pytest

from ksadk.conversations import runtime as conversation_runtime
from ksadk.runners.base_runner import BaseRunner
from ksadk.sessions.in_memory import InMemorySessionService


class _UnsupportedCancelRunner(BaseRunner):
    def __init__(self):
        super().__init__(
            detection_result=SimpleNamespace(name="demo-agent", type=SimpleNamespace(value="mock")),
            project_dir=".",
        )

    def load_agent(self) -> None:
        return None

    async def invoke(self, input_data: dict):
        return {"output": "unused"}

    async def stream(self, input_data: dict):
        yield {"type": "text", "delta": "unused"}


class _CancellableStreamingRunner(_UnsupportedCancelRunner):
    def __init__(self):
        super().__init__()
        self.cancel_requests: list[str] = []

    async def stream(self, input_data: dict):
        yield {"type": "text", "delta": "started"}
        await asyncio.Event().wait()

    def request_cancel(self, invocation_id: str) -> str:
        self.cancel_requests.append(invocation_id)
        return "accepted"


@pytest.mark.asyncio
async def test_cancel_run_reports_unsupported_for_runner_without_cancel_hook(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _UnsupportedCancelRunner()

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/CancelRun",
            json={"AgentId": "demo-agent", "InvocationId": "inv-unsupported"},
        )

    assert response.status_code == 200
    data = response.json()["Data"]
    assert data["Cancelled"] is False
    assert data["Found"] is False
    assert data["Status"] == "unsupported"
    assert data["RunnerCancelStatus"] == "unsupported"


@pytest.mark.asyncio
async def test_cancel_run_stops_detached_stream_and_writes_cancelled_terminal(monkeypatch):
    server_app_module = importlib.import_module("ksadk.server.app")
    service = InMemorySessionService()
    runner = _CancellableStreamingRunner()
    invocation_id = "inv-cancel-long-task"

    monkeypatch.setattr(server_app_module, "resolve_session_service", lambda: service)
    server_app_module.set_runner(runner)
    server_app_module._detached_streaming_response(
        conversation_runtime.stream_responses_conversation_turn(
            runner=runner,
            agent_id="demo-agent",
            user_id="user-1",
            messages=[{"role": "user", "content": "start"}],
            session_id="sess-cancel-long-task",
            model=None,
            prepare_runner=lambda _runner, _model: None,
            invocation_id=invocation_id,
            session_service_provider=lambda: service,
        ),
        invocation_id=invocation_id,
    )

    for _ in range(20):
        events = await service.get_events("sess-cancel-long-task")
        statuses = [
            event.content.get("status")
            for event in events
            if event.event_type == "run_status"
        ]
        if statuses == ["in_progress"]:
            break
        await asyncio.sleep(0.02)

    transport = httpx.ASGITransport(app=server_app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/CancelRun",
            json={"AgentId": "demo-agent", "InvocationId": invocation_id},
        )

    assert response.status_code == 200
    data = response.json()["Data"]
    assert data["Cancelled"] is True
    assert data["Found"] is True
    assert data["Status"] == "cancelling"
    assert data["RunnerCancelStatus"] == "accepted"
    assert runner.cancel_requests == [invocation_id]

    for _ in range(30):
        events = await service.get_events("sess-cancel-long-task")
        if events and events[-1].event_type == "run_status" and events[-1].content.get("status") == "cancelled":
            break
        await asyncio.sleep(0.02)

    events = await service.get_events("sess-cancel-long-task")
    statuses = [
        event.content.get("status")
        for event in events
        if event.event_type == "run_status"
    ]
    event_types = [event.event_type for event in events]
    assert statuses == ["in_progress", "cancelled"]
    assert "assistant_message" not in event_types
    assert "run_checkpoint" not in event_types
