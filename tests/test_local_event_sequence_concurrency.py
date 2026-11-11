"""Independent local writers share one atomic sequence and state version."""

import asyncio

from ksadk.sessions.base import SessionEvent
from ksadk.sessions.local_service import LocalSessionService


async def test_multiple_local_services_append_without_losing_sequence_or_state(tmp_path):
    services = [LocalSessionService(project_dir=str(tmp_path)) for _ in range(2)]
    await services[0].create_session("agent", "user", "session")
    try:
        events = await asyncio.gather(*[
            services[index % 2].append_event("session", SessionEvent(
                author="agent", event_type="test", content={"index": index},
                state_delta={f"key-{index}": index},
            )) for index in range(32)
        ])
        assert sorted(event.seq_id for event in events) == list(range(1, 33))
        session = await services[1].get_session("session")
        assert session.state == {f"key-{i}": i for i in range(32)}
        assert session.version == 32
    finally:
        for service in services:
            await service.aclose()
