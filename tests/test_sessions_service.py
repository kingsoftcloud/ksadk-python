from __future__ import annotations

import importlib
import json
from pathlib import Path

import httpx
import pytest

from ksadk.sessions import (
    close_session_service,
    create_session_service,
    get_session_service,
    reset_session_service,
    resolve_session_service,
)
from ksadk.sessions.base import SessionEvent
from ksadk.sessions.engine_service import EngineSessionService
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.sessions.local_service import LocalSessionService


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


@pytest.mark.asyncio
async def test_in_memory_session_service_get_events_supports_offset_and_limit():
    service = InMemorySessionService()
    await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
    )

    for index in range(4):
        await service.append_event(
            "sess-1",
            SessionEvent(
                id=f"evt-{index + 1}",
                author="user",
                event_type="text",
                content={"index": index},
            ),
        )

    events = await service.get_events("sess-1", offset=1, limit=2)

    assert [event.seq_id for event in events] == [2, 3]


@pytest.mark.asyncio
async def test_in_memory_session_service_create_session_is_idempotent_for_existing_explicit_id():
    service = InMemorySessionService()
    created = await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
    )
    await service.append_event(
        "sess-1",
        SessionEvent(
            id="evt-1",
            author="user",
            event_type="text",
            content={"role": "user", "parts": [{"text": "hello"}]},
            state_delta={"turns": 1},
        ),
    )

    fetched_before = await service.get_session("sess-1")
    recreated = await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
    )
    fetched_after = await service.get_session("sess-1")

    assert fetched_before is not None
    assert fetched_after is not None
    assert recreated.id == "sess-1"
    assert recreated.created_at == created.created_at
    assert recreated.state == {"turns": 1}
    assert [event.id for event in recreated.events] == ["evt-1"]
    assert fetched_after.created_at == fetched_before.created_at
    assert fetched_after.state == {"turns": 1}
    assert [event.id for event in await service.get_events("sess-1")] == ["evt-1"]


@pytest.mark.asyncio
async def test_engine_session_service_reuses_client_until_closed(monkeypatch):
    created_clients = []

    def build_response(method: str, path: str, payload: dict) -> httpx.Response:
        request = httpx.Request(method, f"https://engine.example.test{path}")
        return httpx.Response(200, json=payload, request=request)

    class RecordingAsyncClient:
        def __init__(self, *, base_url, headers, transport=None, timeout=None):
            self.base_url = str(base_url)
            self.headers = headers
            self.transport = transport
            self.timeout = timeout
            self.closed = False
            self.requests = []
            created_clients.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            await self.aclose()

        async def request(self, method, path, params=None, json=None):
            self.requests.append((method, path, params, json))
            if method == "POST" and path == "/conversations/sessions":
                return build_response(
                    method,
                    path,
                    {
                        "id": "sess-1",
                        "agent_id": "demo-agent",
                        "user_id": "user-1",
                        "state": {},
                        "events": [],
                        "created_at": 1.0,
                        "updated_at": 1.0,
                        "version": 0,
                    },
                )
            if method == "GET" and path == "/conversations/sessions/sess-1":
                return build_response(
                    method,
                    path,
                    {
                        "id": "sess-1",
                        "agent_id": "demo-agent",
                        "user_id": "user-1",
                        "state": {},
                        "events": [],
                        "created_at": 1.0,
                        "updated_at": 1.0,
                        "version": 0,
                    },
                )
            raise AssertionError(f"unexpected request: {method} {path}")

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr("ksadk.sessions.engine_service.httpx.AsyncClient", RecordingAsyncClient)

    service = EngineSessionService(
        endpoint="https://engine.example.test",
        token="secret-token",
    )

    created = await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
    )
    fetched = await service.get_session("sess-1")

    assert created is not None
    assert fetched is not None
    assert len(created_clients) == 1
    assert created_clients[0].headers["Authorization"] == "Bearer secret-token"
    assert created_clients[0].closed is False

    await service.aclose()
    assert created_clients[0].closed is True

    await service.aclose()
    assert len(created_clients) == 1


@pytest.mark.asyncio
async def test_engine_session_service_delete_session_accepts_no_content_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/conversations/sessions/sess-1"
        return httpx.Response(204, request=request)

    service = EngineSessionService(
        endpoint="https://engine.example.test",
        transport=httpx.MockTransport(handler),
    )

    assert await service.delete_session("sess-1") is True


@pytest.mark.asyncio
async def test_engine_session_service_get_events_supports_offset_and_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params == httpx.QueryParams({"offset": "1", "limit": "2"})
        return httpx.Response(
            200,
            json=[
                {
                    "id": "evt-2",
                    "session_id": "sess-1",
                    "author": "user",
                    "event_type": "text",
                    "content": {"index": 1},
                    "seq_id": 2,
                    "timestamp": 2.0,
                },
                {
                    "id": "evt-3",
                    "session_id": "sess-1",
                    "author": "user",
                    "event_type": "text",
                    "content": {"index": 2},
                    "seq_id": 3,
                    "timestamp": 3.0,
                },
            ],
            request=request,
        )

    service = EngineSessionService(
        endpoint="https://engine.example.test",
        transport=httpx.MockTransport(handler),
    )

    events = await service.get_events("sess-1", offset=1, limit=2)

    assert [event.seq_id for event in events] == [2, 3]


@pytest.mark.asyncio
async def test_sqlite_session_service_persists_sessions_events_and_state(tmp_path):
    db_path = tmp_path / "sessions.sqlite"
    service = LocalSessionService(db_path=db_path)

    session = await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
    )
    await service.append_event(
        "sess-1",
        SessionEvent(
            id="evt-1",
            author="user",
            event_type="text",
            content={"role": "user", "parts": [{"text": "hello"}]},
            state_delta={"turns": 1},
        ),
    )
    await service.update_state(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
        scope="session",
        state_delta={"topic": "billing"},
    )
    await service.aclose()

    reopened = LocalSessionService(db_path=db_path)
    fetched = await reopened.get_session("sess-1")
    assert fetched is not None
    assert fetched.id == session.id
    assert fetched.state == {"turns": 1, "topic": "billing"}

    events = await reopened.get_events("sess-1")
    assert [event.id for event in events] == ["evt-1"]
    assert events[0].content["parts"][0]["text"] == "hello"

    state = await reopened.get_state(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
        scope="session",
    )
    assert state is not None
    assert state.state == {"turns": 1, "topic": "billing"}

    await reopened.aclose()


@pytest.mark.asyncio
async def test_resolve_session_service_defaults_to_local_backend(monkeypatch, tmp_path):
    module = importlib.import_module("ksadk.sessions")
    monkeypatch.delenv("AGENTENGINE_SESSION_ENDPOINT", raising=False)
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / ".agentengine" / "ui"))

    await reset_session_service()
    module._cached_session_service = None

    service = resolve_session_service()

    assert isinstance(service, LocalSessionService)
    assert Path(service.db_path).parent == tmp_path / ".agentengine" / "ui"

    await reset_session_service()


def test_resolve_session_service_auto_selects_implementation(monkeypatch):
    monkeypatch.delenv("AGENTENGINE_SESSION_ENDPOINT", raising=False)
    monkeypatch.delenv("AGENTENGINE_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AGENTENGINE_SESSION_BACKEND", raising=False)
    monkeypatch.setattr("ksadk.sessions._cached_session_service", None)

    service = resolve_session_service()
    assert isinstance(service, LocalSessionService)

    monkeypatch.setenv("AGENTENGINE_SESSION_BACKEND", "memory")
    monkeypatch.setattr("ksadk.sessions._cached_session_service", None)

    service = resolve_session_service()
    assert isinstance(service, InMemorySessionService)

    monkeypatch.setenv("AGENTENGINE_SESSION_ENDPOINT", "https://engine.example.test")
    monkeypatch.setenv("AGENTENGINE_SESSION_TOKEN", "secret-token")
    monkeypatch.setattr("ksadk.sessions._cached_session_service", None)

    service = resolve_session_service()
    assert isinstance(service, EngineSessionService)
    assert service.endpoint == "https://engine.example.test"


def test_create_session_service_hides_backend_selection(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTENGINE_SESSION_ENDPOINT", raising=False)
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / ".agentengine" / "ui"))

    local_service = create_session_service()
    assert isinstance(local_service, LocalSessionService)

    memory_service = create_session_service(backend="memory")
    assert isinstance(memory_service, InMemorySessionService)

    engine_service = create_session_service(
        endpoint="https://engine.example.test",
        backend="engine",
    )
    assert isinstance(engine_service, EngineSessionService)


@pytest.mark.asyncio
async def test_legacy_session_service_aliases_remain_compatible(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTENGINE_SESSION_ENDPOINT", raising=False)
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / ".agentengine" / "ui"))
    monkeypatch.setattr("ksadk.sessions._cached_session_service", None)

    service = get_session_service()
    assert isinstance(service, LocalSessionService)
    assert get_session_service() is resolve_session_service()

    await close_session_service()


def test_legacy_sqlite_service_import_path_remains_available():
    module = importlib.import_module("ksadk.sessions.sqlite_service")
    assert module.LocalSessionService is LocalSessionService
