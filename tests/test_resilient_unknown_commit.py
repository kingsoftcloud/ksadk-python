"""Unconfirmed durable writes must fence the process-local recovery mirror."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest

from ksadk.sessions.base import SessionEvent
from ksadk.sessions.db_budget import DatabaseBudget
from ksadk.sessions.errors import SessionBackendUnavailable
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.sessions.postgres_service import PostgresSessionService
from ksadk.sessions.resilient import ResilientSessionService


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("failure", ["deadline", "cancel", "confirmation"])
async def test_unconfirmed_commit_fences_mirror_until_full_recovery(backend, failure, monkeypatch):
    if backend == "postgres":
        dsn = os.environ.get("KSADK_TEST_POSTGRES_DSN")
        assert dsn, "Set KSADK_TEST_POSTGRES_DSN to a dedicated test database"
        primary = PostgresSessionService(dsn=dsn, namespace="commit_" + uuid4().hex[:16])
    else:
        primary = InMemorySessionService()
    primary.db_budget = DatabaseBudget(write_operation_seconds=0.1)
    service = ResilientSessionService(primary)
    committed = asyncio.Event()
    append = primary.append_event
    get_session = primary.get_session
    get_state = primary.get_state
    get_event = primary.get_event_by_id
    calls = 0

    async def lost_ack(session_id, event):
        nonlocal calls
        calls += 1
        await append(session_id, event)  # A real PostgreSQL COMMIT in the PG cases.
        committed.set()
        if failure == "confirmation":
            raise ConnectionError("test acknowledgement lost")
        await asyncio.Event().wait()

    async def unavailable(*args, **kwargs):
        raise ConnectionError("test backend unavailable")

    try:
        await service.create_session("agent", "user", "session")
        await service.update_state(
            agent_id="agent",
            user_id="user",
            session_id="session",
            scope="session",
            state_delta={"value": "before"},
        )
        monkeypatch.setattr(primary, "append_event", lost_ack)
        if failure == "confirmation":
            monkeypatch.setattr(primary, "get_event_by_id", unavailable)
        task = asyncio.create_task(
            service.append_event(
                "session", SessionEvent(id="committed", state_delta={"value": "committed"})
            )
        )
        await asyncio.wait_for(committed.wait(), 2)
        if failure == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(SessionBackendUnavailable):
                await task
        monkeypatch.setattr(primary, "append_event", append)
        monkeypatch.setattr(primary, "get_event_by_id", get_event)
        assert calls == 1
        assert [(e.id, e.seq_id) for e in await primary.get_events("session")] == [("committed", 1)]

        monkeypatch.setattr(primary, "get_session", unavailable)
        monkeypatch.setattr(primary, "get_state", unavailable)
        with pytest.raises(SessionBackendUnavailable):
            await service.get_state("agent", "user", "session")
        with pytest.raises(SessionBackendUnavailable):
            await service.update_state(
                agent_id="agent",
                user_id="user",
                session_id="session",
                scope="session",
                state_delta={"value": "must not fork"},
            )
        with pytest.raises(SessionBackendUnavailable):
            await service.get_session("session")
        with pytest.raises(SessionBackendUnavailable):
            await service.create_session("agent", "user", "session")
        with pytest.raises(SessionBackendUnavailable):
            await service.update_session_metadata("session", title="must not fork")
        with pytest.raises(SessionBackendUnavailable):
            await service.append_event("session", SessionEvent(id="following"))
        assert [e.id for e in await primary.get_events("session")] == ["committed"]

        monkeypatch.setattr(primary, "get_session", get_session)
        monkeypatch.setattr(primary, "get_state", get_state)
        service._primary_enabled = True  # The normal recovery probe has succeeded.
        recovered = await service.get_session("session")
        assert [(e.id, e.seq_id) for e in recovered.events] == [("committed", 1)]
        assert recovered.state == {"value": "committed"}
        following = await service.append_event("session", SessionEvent(id="following"))
        assert following.seq_id == 2
        assert [(e.id, e.seq_id) for e in await service.get_events("session")] == [
            ("committed", 1),
            ("following", 2),
        ]
        assert [(e.id, e.seq_id) for e in await service.fallback.get_events("session")] == [
            ("committed", 1),
            ("following", 2),
        ]
    finally:
        await primary.delete_session("session")
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_state_only_refresh_cannot_revalidate_an_unsafe_mirror(backend, monkeypatch):
    if backend == "postgres":
        dsn = os.environ.get("KSADK_TEST_POSTGRES_DSN")
        assert dsn, "Set KSADK_TEST_POSTGRES_DSN to a dedicated test database"
        primary = PostgresSessionService(dsn=dsn, namespace="state_" + uuid4().hex[:16])
    else:
        primary = InMemorySessionService()
    primary.db_budget = DatabaseBudget(write_operation_seconds=0.1)
    service = ResilientSessionService(primary)
    append = primary.append_event

    async def lost_ack(session_id, event):
        await append(session_id, event)
        await asyncio.Event().wait()

    async def unavailable(*args, **kwargs):
        raise ConnectionError("test backend unavailable")

    try:
        await service.create_session("agent", "user", "session")
        monkeypatch.setattr(primary, "append_event", lost_ack)
        with pytest.raises(SessionBackendUnavailable):
            await service.append_event(
                "session", SessionEvent(id="committed", state_delta={"value": "committed"})
            )
        # A state read is valid while PG is healthy, but proves nothing about
        # the missing event log. Its cached key must not enable later fallback.
        state = await service.get_state("agent", "user", "session")
        assert state.state == {"value": "committed"}
        monkeypatch.setattr(primary, "get_state", unavailable)
        with pytest.raises(SessionBackendUnavailable):
            await service.get_state("agent", "user", "session")
        with pytest.raises(SessionBackendUnavailable):
            await service.update_state(
                agent_id="agent",
                user_id="user",
                session_id="session",
                scope="session",
                state_delta={"value": "must not fork"},
            )
        with pytest.raises(SessionBackendUnavailable):
            await service.append_event("session", SessionEvent(id="following"))
        assert [(e.id, e.seq_id) for e in await primary.get_events("session")] == [("committed", 1)]
    finally:
        await primary.delete_session("session")
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_precommit_snapshot_cannot_revalidate_an_unsafe_mirror(backend, monkeypatch):
    import copy

    if backend == "postgres":
        dsn = os.environ.get("KSADK_TEST_POSTGRES_DSN")
        assert dsn, "Set KSADK_TEST_POSTGRES_DSN to a dedicated test database"
        primary = PostgresSessionService(dsn=dsn, namespace="snapshot_" + uuid4().hex[:16])
    else:
        primary = InMemorySessionService()
    primary.db_budget = DatabaseBudget(write_operation_seconds=0.1)
    service = ResilientSessionService(primary)
    captured = asyncio.Event()
    release = asyncio.Event()
    get_session = primary.get_session
    append = primary.append_event

    async def stale_snapshot(session_id):
        snapshot = copy.deepcopy(await get_session(session_id))
        captured.set()
        await release.wait()
        return snapshot

    async def lost_ack(session_id, event):
        await append(session_id, event)
        await asyncio.Event().wait()

    reading = None
    try:
        await service.create_session("agent", "user", "session")
        monkeypatch.setattr(primary, "get_session", stale_snapshot)
        reading = asyncio.create_task(service.get_session("session"))
        await asyncio.wait_for(captured.wait(), 2)
        monkeypatch.setattr(primary, "append_event", lost_ack)
        with pytest.raises(SessionBackendUnavailable):
            await service.append_event("session", SessionEvent(id="committed"))
        monkeypatch.setattr(primary, "append_event", append)
        monkeypatch.setattr(primary, "get_session", get_session)
        release.set()
        recovered = await asyncio.wait_for(reading, 2)
        assert [(e.id, e.seq_id) for e in recovered.events] == [("committed", 1)]
        assert [e.id for e in await service.fallback.get_events("session")] == ["committed"]
        assert (await service.append_event("session", SessionEvent(id="following"))).seq_id == 2
    finally:
        if reading is not None and not reading.done():
            reading.cancel()
            await asyncio.gather(reading, return_exceptions=True)
        await primary.delete_session("session")
        await service.aclose()
