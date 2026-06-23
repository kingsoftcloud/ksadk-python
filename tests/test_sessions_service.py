from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

import pytest

from ksadk.sessions import (
    close_session_service,
    create_session_service,
    get_session_service,
    reset_session_service,
    resolve_session_service,
)
from ksadk.sessions.base import SessionEvent
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
    assert session.title == ""
    assert session.title_source == ""
    assert session.summary == ""
    assert session.first_prompt == ""
    assert session.last_prompt == ""
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
    assert fetched.title == ""
    assert fetched.title_source == ""
    assert fetched.summary == ""
    assert fetched.first_prompt == ""
    assert fetched.last_prompt == ""
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
async def test_in_memory_and_local_session_services_sort_by_updated_at_desc_and_preserve_metadata(tmp_path):
    memory_service = InMemorySessionService()
    local_service = LocalSessionService(db_path=tmp_path / "sessions.sqlite")

    for service in (memory_service, local_service):
        await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-old")
        await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-new")
        await service.update_session_metadata(
            "sess-old",
            title="老会话",
            title_source="fallback_first_prompt",
            summary="旧摘要",
            first_prompt="最早问题",
            last_prompt="最近更新",
        )
        await service.append_event(
            "sess-old",
            SessionEvent(
                id="evt-refresh",
                author="user",
                event_type="user_message",
                content={"role": "user", "parts": [{"text": "刷新排序"}]},
            ),
        )

        listed = await service.list_sessions("demo-agent", "user-1")
        assert [item.id for item in listed] == ["sess-old", "sess-new"]
        assert listed[0].title == "老会话"
        assert listed[0].title_source == "fallback_first_prompt"
        assert listed[0].summary == "旧摘要"
        assert listed[0].first_prompt == "最早问题"
        assert listed[0].last_prompt == "最近更新"

    await local_service.aclose()


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
    monkeypatch.delenv("AGENTENGINE_SESSION_BACKEND", raising=False)
    monkeypatch.setattr("ksadk.sessions._cached_session_service", None)

    service = resolve_session_service()
    assert isinstance(service, LocalSessionService)

    monkeypatch.setenv("AGENTENGINE_SESSION_BACKEND", "memory")
    monkeypatch.setattr("ksadk.sessions._cached_session_service", None)

    service = resolve_session_service()
    assert isinstance(service, InMemorySessionService)


def test_create_session_service_hides_backend_selection(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / ".agentengine" / "ui"))

    local_service = create_session_service()
    assert isinstance(local_service, LocalSessionService)

    memory_service = create_session_service(backend="memory")
    assert isinstance(memory_service, InMemorySessionService)


@pytest.mark.asyncio
async def test_legacy_session_service_aliases_remain_compatible(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / ".agentengine" / "ui"))
    monkeypatch.setattr("ksadk.sessions._cached_session_service", None)

    service = get_session_service()
    assert isinstance(service, LocalSessionService)
    assert get_session_service() is resolve_session_service()

    await close_session_service()


def test_legacy_sqlite_service_import_path_remains_available():
    module = importlib.import_module("ksadk.sessions.sqlite_service")
    assert module.LocalSessionService is LocalSessionService


@pytest.mark.asyncio
async def test_local_session_service_closes_sqlite_connections(monkeypatch, tmp_path):
    connections: list[sqlite3.Connection] = []

    class TrackingConnection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.closed = False

        def close(self):
            self.closed = True
            return super().close()

    original_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        kwargs["factory"] = TrackingConnection
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)

    service = LocalSessionService(db_path=tmp_path / "sessions.sqlite")
    session = await service.create_session("demo-agent", "user-1", "sess-1")
    await service.append_event(
        session.id,
        SessionEvent(
            id="evt-1",
            author="user",
            event_type="user_message",
            content={"role": "user", "parts": [{"text": "hello"}]},
        ),
    )
    await service.get_session(session.id)
    await service.list_sessions("demo-agent")
    await service.get_events(session.id)
    await service.delete_session(session.id)

    assert connections
    assert all(getattr(connection, "closed", False) for connection in connections)
