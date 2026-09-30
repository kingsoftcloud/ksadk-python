from __future__ import annotations

import pytest

from ksadk.events.canonical import RunCompleted, RunStarted, SourceRef
from ksadk.events.canonical_store import RuntimeEventStore
from ksadk.events.session_event import SessionServiceEventStore
from ksadk.kernel.contracts import ActivationWriteGuard
from ksadk.sessions.in_memory import InMemorySessionService

pytestmark = pytest.mark.asyncio


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
