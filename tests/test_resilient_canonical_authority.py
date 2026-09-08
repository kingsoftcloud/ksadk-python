import pytest

from ksadk.events.canonical import RunStarted, SourceRef
from ksadk.events.canonical_store import RuntimeEventStore, canonical_storage_id
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.sessions.resilient import ResilientSessionService

pytestmark = pytest.mark.asyncio


async def test_runtime_events_use_resilient_primary_as_single_authority() -> None:
    primary = InMemorySessionService()
    fallback = InMemorySessionService()
    service = ResilientSessionService(primary, fallback=fallback)
    await service.create_session("agent-1", "user-1", session_id="session-1")
    event = RunStarted(
        schema_version=2,
        event_id="event-1",
        seq=0,
        timestamp=1.0,
        run_id="run-1",
        scope_id="scope-1",
        source=SourceRef(framework="adk", native_event_id="native-1"),
        status="running",
    )

    persisted = await RuntimeEventStore(service).append_one("session-1", event)

    assert persisted.seq == 1
    assert [stored.id for stored in await primary.get_events("session-1")] == [
        canonical_storage_id("session-1", "event-1")
    ]
    assert await fallback.get_events("session-1") == []
