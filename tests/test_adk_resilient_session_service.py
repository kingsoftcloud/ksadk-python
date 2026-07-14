from __future__ import annotations

import asyncio
import logging

import pytest
from google.adk.events.event import Event
from google.adk.sessions import InMemorySessionService

from ksadk.memory.adk.resilient_session_service import ResilientADKSessionService


class AppendFailingSessionService(InMemorySessionService):
    async def append_event(self, session, event):
        raise ConnectionError("postgres connection lost")


class InvalidADKSessionService(InMemorySessionService):
    async def create_session(self, **kwargs):
        raise ValueError("invalid ADK session payload")


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
async def test_adk_non_backend_error_is_not_hidden_by_fail_open():
    service = ResilientADKSessionService(InvalidADKSessionService())

    with pytest.raises(ValueError, match="invalid ADK session payload"):
        await service.create_session(
            app_name="demo-agent",
            user_id="user-1",
            session_id="sess-invalid",
        )

    assert service.degraded is False
    await service.close()


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


@pytest.mark.asyncio
async def test_adk_session_refreshes_events_written_by_another_replica():
    primary = InMemorySessionService()
    durable = await primary.create_session(
        app_name="demo-agent",
        user_id="user-1",
        session_id="sess-1",
    )
    first_event = Event(author="user", invocation_id="inv-1")
    await primary.append_event(durable, first_event)

    service = ResilientADKSessionService(primary)
    hydrated = await service.get_session(
        app_name="demo-agent",
        user_id="user-1",
        session_id="sess-1",
    )
    assert hydrated is not None
    assert [event.id for event in hydrated.events] == [first_event.id]

    durable = await primary.get_session(
        app_name="demo-agent",
        user_id="user-1",
        session_id="sess-1",
    )
    assert durable is not None
    second_event = Event(author="demo-agent", invocation_id="inv-2")
    await primary.append_event(durable, second_event)

    refreshed = await service.get_session(
        app_name="demo-agent",
        user_id="user-1",
        session_id="sess-1",
    )
    assert refreshed is not None
    assert [event.id for event in refreshed.events] == [first_event.id, second_event.id]


class _RecoverableADKPrimary(InMemorySessionService):
    """Mock primary that fails N times then recovers."""

    def __init__(self, fail_count: int = 1) -> None:
        super().__init__()
        self._fail_remaining = fail_count

    async def get_session(self, *, app_name, user_id, session_id, config=None):
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise ConnectionError("pg temporarily down")
        return await super().get_session(
            app_name=app_name, user_id=user_id, session_id=session_id, config=config
        )


@pytest.mark.asyncio
async def test_adk_session_recovers_after_probe(caplog):
    primary = _RecoverableADKPrimary(fail_count=1)
    service = ResilientADKSessionService(primary)
    service._probe_interval_seconds = 0.05
    caplog.set_level(logging.INFO)

    session = await service.create_session(
        app_name="demo-agent",
        user_id="user-1",
        session_id="sess-1",
    )
    event = Event(author="demo-agent", invocation_id="inv-1")
    await service.append_event(session, event)

    assert service.degraded is True
    await asyncio.sleep(0.15)
    assert service.degraded is False
    assert "ADK session persistence recovered" in caplog.text
    await service.close()
