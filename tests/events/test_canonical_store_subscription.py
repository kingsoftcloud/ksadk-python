from __future__ import annotations

import asyncio

import pytest

from ksadk.events.canonical import RunCompleted, RunStarted, SourceRef
from ksadk.events.canonical_store import RuntimeEventStore
from ksadk.events.session_event import SessionServiceEventStore
from ksadk.kernel.contracts import ActivationWriteGuard
from ksadk.sessions.in_memory import InMemorySessionService

pytestmark = pytest.mark.asyncio


class _EnvelopeOnlyStore:
    """Hide the legacy session service to exercise the typed-only path."""

    def __init__(self, sessions: InMemorySessionService) -> None:
        self._delegate = SessionServiceEventStore(sessions)

    async def append(self, envelope, *, guard):
        return await self._delegate.append(envelope, guard=guard)

    async def read(self, session_id: str, after_seq: int, limit: int):
        return await self._delegate.read(session_id, after_seq, limit)

    def subscribe(self, session_id: str, after_seq: int):
        return self._delegate.subscribe(session_id, after_seq)


class _TrackingEnvelopeStore(_EnvelopeOnlyStore):
    def __init__(self, sessions: InMemorySessionService) -> None:
        super().__init__(sessions)
        self.closed = asyncio.Event()
        self._wake = asyncio.Event()

    async def append(self, envelope, *, guard):
        persisted = await super().append(envelope, guard=guard)
        self._wake.set()
        return persisted

    async def subscribe(self, session_id: str, after_seq: int):
        cursor = int(after_seq)
        try:
            while True:
                rows = await self.read(session_id, cursor, 1000)
                for envelope in rows:
                    cursor = max(cursor, int(envelope.seq))
                    yield envelope
                await self._wake.wait()
                self._wake.clear()
        finally:
            self.closed.set()


def _run_started(event_id: str, *, run_id: str = "run-1") -> RunStarted:
    return RunStarted(
        schema_version=2,
        event_id=event_id,
        seq=0,
        timestamp=1.0,
        run_id=run_id,
        scope_id="scope-1",
        source=SourceRef(framework="adk"),
        status="running",
    )


def _run_completed(event_id: str, *, run_id: str = "run-1") -> RunCompleted:
    return RunCompleted(
        schema_version=2,
        event_id=event_id,
        seq=0,
        timestamp=2.0,
        run_id=run_id,
        scope_id="scope-1",
        source=SourceRef(framework="adk"),
        status="completed",
        output_refs=(),
    )


async def _typed_store() -> tuple[RuntimeEventStore, ActivationWriteGuard]:
    sessions = InMemorySessionService()
    await sessions.create_session("agent-1", "user-1", session_id="session-1")
    runtime_events = RuntimeEventStore(
        SessionServiceEventStore(sessions),
        session_id="session-1",
    )
    guard = ActivationWriteGuard(activation_id="activation-1", fencing_token=1)
    return runtime_events, guard


async def test_typed_view_subscription_replays_runtime_events() -> None:
    runtime_events, guard = await _typed_store()
    await runtime_events.append(_run_started("event-1"), guard=guard)

    stream = runtime_events.subscribe_session("session-1", timeout=1)
    event = await anext(stream)
    await stream.aclose()

    assert event.event_id == "event-1"
    assert event.seq == 1


async def test_typed_view_run_subscription_stops_at_terminal_event() -> None:
    runtime_events, guard = await _typed_store()
    await runtime_events.append(_run_started("event-1"), guard=guard)
    await runtime_events.append(_run_completed("event-2"), guard=guard)

    events = [
        event
        async for event in runtime_events.subscribe_run("session-1", "run-1", timeout=1)
    ]

    assert [event.event_type for event in events] == ["run.started", "run.completed"]
    assert [event.seq for event in events] == [1, 2]


async def test_service_backed_typed_view_includes_legacy_carrier_events() -> None:
    sessions = InMemorySessionService()
    await sessions.create_session("agent-1", "user-1", session_id="session-1")
    legacy_store = RuntimeEventStore(sessions)
    await legacy_store.append("session-1", [_run_started("legacy-event")])
    typed_store = RuntimeEventStore(
        SessionServiceEventStore(sessions),
        session_id="session-1",
    )

    events = [
        event
        async for event in typed_store.subscribe_session("session-1", timeout=0.01)
    ]

    assert [event.event_id for event in events] == ["legacy-event"]


async def test_envelope_only_subscription_replays_with_zero_timeout() -> None:
    sessions = InMemorySessionService()
    await sessions.create_session("agent-1", "user-1", session_id="session-1")
    backend = _EnvelopeOnlyStore(sessions)
    runtime_events = RuntimeEventStore(backend, session_id="session-1")
    guard = ActivationWriteGuard(activation_id="activation-1", fencing_token=1)
    await runtime_events.append(_run_started("event-1"), guard=guard)

    events = [
        event
        async for event in runtime_events.subscribe_session("session-1", timeout=0)
    ]

    assert [(event.event_id, event.seq) for event in events] == [("event-1", 1)]


async def test_envelope_only_subscription_closes_backend_on_outer_close() -> None:
    sessions = InMemorySessionService()
    await sessions.create_session("agent-1", "user-1", session_id="session-1")
    backend = _TrackingEnvelopeStore(sessions)
    runtime_events = RuntimeEventStore(backend, session_id="session-1")
    guard = ActivationWriteGuard(activation_id="activation-1", fencing_token=1)
    await runtime_events.append(_run_started("event-1"), guard=guard)

    stream = runtime_events.subscribe_session("session-1", after_seq=1, timeout=1)
    next_event = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    await runtime_events.append(_run_started("event-2"), guard=guard)
    assert (await next_event).event_id == "event-2"
    await stream.aclose()

    await asyncio.wait_for(backend.closed.wait(), timeout=1)


async def test_envelope_only_subscription_closes_backend_on_cancellation() -> None:
    sessions = InMemorySessionService()
    await sessions.create_session("agent-1", "user-1", session_id="session-1")
    backend = _TrackingEnvelopeStore(sessions)
    runtime_events = RuntimeEventStore(backend, session_id="session-1")
    guard = ActivationWriteGuard(activation_id="activation-1", fencing_token=1)
    await runtime_events.append(_run_started("event-1"), guard=guard)

    stream = runtime_events.subscribe_session("session-1", after_seq=1, timeout=10)
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    await asyncio.wait_for(backend.closed.wait(), timeout=1)


async def test_envelope_only_run_subscription_closes_backend_at_terminal() -> None:
    sessions = InMemorySessionService()
    await sessions.create_session("agent-1", "user-1", session_id="session-1")
    backend = _TrackingEnvelopeStore(sessions)
    runtime_events = RuntimeEventStore(backend, session_id="session-1")
    guard = ActivationWriteGuard(activation_id="activation-1", fencing_token=1)
    await runtime_events.append(_run_started("event-1"), guard=guard)
    stream = runtime_events.subscribe_run("session-1", "run-1", after_seq=1, timeout=1)
    next_event = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    await runtime_events.append(_run_completed("event-2"), guard=guard)

    events = [await next_event]
    async for event in stream:
        events.append(event)

    await asyncio.wait_for(backend.closed.wait(), timeout=1)
    assert [event.event_type for event in events] == ["run.completed"]
