from __future__ import annotations

import json

import httpx
import pytest

from ksadk.sessions import get_session_service
from ksadk.sessions.base import SessionEvent
from ksadk.sessions.engine_service import EngineSessionService
from ksadk.sessions.in_memory import InMemorySessionService


@pytest.mark.asyncio
async def test_in_memory_session_service_crud_append_event_and_state_updates():
    service = InMemorySessionService()

    session = await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
    )

    assert session.id == "sess-1"
    assert session.agent_id == "demo-agent"
    assert session.user_id == "user-1"
    assert session.state == {}

    appended = await service.append_event(
        "sess-1",
        SessionEvent(
            id="evt-1",
            author="user",
            event_type="text",
            content={"role": "user", "parts": [{"text": "hello"}]},
            state_delta={"turns": 1},
        ),
    )

    assert appended.id == "evt-1"
    assert appended.content["parts"][0]["text"] == "hello"

    fetched = await service.get_session("sess-1")
    assert fetched is not None
    assert fetched.state == {"turns": 1}

    updated = await service.update_state(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
        scope="session",
        state_delta={"topic": "billing"},
    )
    assert updated.scope == "session"
    assert updated.state == {"turns": 1, "topic": "billing"}
    assert updated.version == 2

    state = await service.get_state(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
        scope="session",
    )
    assert state is not None
    assert state.state == {"turns": 1, "topic": "billing"}

    listed = await service.list_sessions(agent_id="demo-agent", user_id="user-1")
    assert [item.id for item in listed] == ["sess-1"]

    events = await service.get_events("sess-1")
    assert [event.id for event in events] == ["evt-1"]

    assert await service.delete_session("sess-1") is True
    assert await service.get_session("sess-1") is None


@pytest.mark.asyncio
async def test_engine_session_service_uses_conversation_http_api():
    requests: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, payload))
        headers = {"x-request-auth": request.headers.get("authorization", "")}

        if request.method == "POST" and request.url.path == "/conversations/sessions":
            assert payload == {
                "agent_id": "demo-agent",
                "user_id": "user-1",
                "session_id": "sess-1",
            }
            return httpx.Response(
                200,
                json={
                    "id": "sess-1",
                    "agent_id": "demo-agent",
                    "user_id": "user-1",
                    "state": {},
                    "events": [],
                    "created_at": 1.0,
                    "updated_at": 1.0,
                    "version": 0,
                },
                headers=headers,
            )

        if request.method == "POST" and request.url.path == "/conversations/sessions/sess-1/events":
            assert payload["author"] == "user"
            assert payload["event_type"] == "text"
            return httpx.Response(
                200,
                json={
                    "id": "evt-1",
                    "session_id": "sess-1",
                    "author": "user",
                    "event_type": "text",
                    "content": payload["content"],
                    "state_delta": payload["state_delta"],
                    "seq_id": 1,
                    "timestamp": 2.0,
                },
                headers=headers,
            )

        if request.method == "GET" and request.url.path == "/conversations/sessions/sess-1/events":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "evt-1",
                        "session_id": "sess-1",
                        "author": "user",
                        "event_type": "text",
                        "content": {"role": "user", "parts": [{"text": "hello"}]},
                        "state_delta": {"turns": 1},
                        "seq_id": 1,
                        "timestamp": 2.0,
                    }
                ],
                headers=headers,
            )

        if request.method == "PUT" and request.url.path == "/conversations/states/session":
            assert payload == {
                "agent_id": "demo-agent",
                "user_id": "user-1",
                "session_id": "sess-1",
                "state_delta": {"topic": "billing"},
            }
            return httpx.Response(
                200,
                json={
                    "scope": "session",
                    "agent_id": "demo-agent",
                    "user_id": "user-1",
                    "session_id": "sess-1",
                    "state": {"topic": "billing"},
                    "version": 1,
                    "updated_at": 3.0,
                },
                headers=headers,
            )

        if request.method == "DELETE" and request.url.path == "/conversations/sessions/sess-1":
            return httpx.Response(200, json={"deleted": True}, headers=headers)

        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    service = EngineSessionService(
        endpoint="https://engine.example.test",
        token="secret-token",
        transport=httpx.MockTransport(handler),
    )

    session = await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
    )
    assert session.id == "sess-1"

    event = await service.append_event(
        "sess-1",
        SessionEvent(
            id="evt-local",
            author="user",
            event_type="text",
            content={"role": "user", "parts": [{"text": "hello"}]},
            state_delta={"turns": 1},
        ),
    )
    assert event.id == "evt-1"

    events = await service.get_events("sess-1")
    assert [item.seq_id for item in events] == [1]

    state = await service.update_state(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
        scope="session",
        state_delta={"topic": "billing"},
    )
    assert state.version == 1

    assert await service.delete_session("sess-1") is True
    assert [item[:2] for item in requests] == [
        ("POST", "/conversations/sessions"),
        ("POST", "/conversations/sessions/sess-1/events"),
        ("GET", "/conversations/sessions/sess-1/events"),
        ("PUT", "/conversations/states/session"),
        ("DELETE", "/conversations/sessions/sess-1"),
    ]


def test_get_session_service_auto_selects_implementation(monkeypatch):
    monkeypatch.delenv("AGENTENGINE_SESSION_ENDPOINT", raising=False)
    monkeypatch.delenv("AGENTENGINE_SESSION_TOKEN", raising=False)
    monkeypatch.setattr("ksadk.sessions._service_instance", None)

    service = get_session_service()
    assert isinstance(service, InMemorySessionService)

    monkeypatch.setenv("AGENTENGINE_SESSION_ENDPOINT", "https://engine.example.test")
    monkeypatch.setenv("AGENTENGINE_SESSION_TOKEN", "secret-token")
    monkeypatch.setattr("ksadk.sessions._service_instance", None)

    service = get_session_service()
    assert isinstance(service, EngineSessionService)
    assert service.endpoint == "https://engine.example.test"
