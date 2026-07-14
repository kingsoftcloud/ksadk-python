from __future__ import annotations

import pytest
from google.adk.events.event import Event
from google.adk.sessions import InMemorySessionService

from ksadk.memory.adk.resilient_session_service import ResilientADKSessionService


class AppendFailingSessionService(InMemorySessionService):
    async def append_event(self, session, event):
        raise ConnectionError("postgres connection lost")


@pytest.mark.asyncio
async def test_adk_session_persistence_failure_does_not_abort_live_session(caplog):
    primary = AppendFailingSessionService()
    service = ResilientADKSessionService(primary)
    session = await service.create_session(
        app_name="demo-agent",
        user_id="user-1",
        session_id="sess-1",
    )
    event = Event(author="demo-agent", invocation_id="inv-1")

    stored = await service.append_event(session, event)
    fetched = await service.get_session(
        app_name="demo-agent",
        user_id="user-1",
        session_id="sess-1",
    )

    assert stored.id == event.id
    assert fetched is not None
    assert [item.id for item in fetched.events] == [event.id]
    assert service.degraded is True
    assert "ADK session persistence degraded" in caplog.text


@pytest.mark.asyncio
async def test_adk_session_keeps_hydrated_events_after_primary_failure(caplog):
    primary = AppendFailingSessionService()
    durable = await primary.create_session(
        app_name="demo-agent",
        user_id="user-1",
        session_id="sess-1",
    )
    old_event = Event(author="user", invocation_id="inv-old")
    await InMemorySessionService.append_event(primary, durable, old_event)

    service = ResilientADKSessionService(primary)
    session = await service.get_session(
        app_name="demo-agent",
        user_id="user-1",
        session_id="sess-1",
    )
    assert session is not None
    assert [event.id for event in session.events] == [old_event.id]

    new_event = Event(author="demo-agent", invocation_id="inv-new")
    await service.append_event(session, new_event)
    fetched = await service.get_session(
        app_name="demo-agent",
        user_id="user-1",
        session_id="sess-1",
    )

    assert fetched is not None
    assert [event.id for event in fetched.events] == [old_event.id, new_event.id]
    assert service.degraded is True
    assert caplog.text.count("ADK session persistence degraded") == 1
