from __future__ import annotations

import json

import pytest

from ksadk.events.canonical_store import runtime_event_to_session_event
from ksadk.sessions._postgres_checkpoint_stats import get_checkpoint_stats
from tests.server.test_checkpoint_resume_audit import (
    _continuation_created,
    _continuation_resumed,
)


def _raw(event):
    return {
        "id": event.id,
        "event_type": event.event_type,
        "content": event.content,
        "timestamp": event.timestamp,
        "seq_id": event.seq_id,
        "invocation_id": event.invocation_id,
        "metadata": event.metadata,
    }


@pytest.mark.asyncio
async def test_postgres_jsonb_aggregate_text_is_decoded() -> None:
    created = runtime_event_to_session_event("session-1", _continuation_created())
    resumed = runtime_event_to_session_event("session-1", _continuation_resumed())
    resume_rows = json.dumps([_raw(resumed)])
    creation_rows = json.dumps([_raw(created)])

    class FakeConnection:
        async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
            if "canonical_carrier_timestamps" in query:
                return [
                    {
                        "session_id": "session-1",
                        "run_id": "run-1",
                        "checkpoint_id": "checkpoint-1",
                        "legacy_resume_count": 0,
                        "legacy_last_resumed_at": None,
                        "canonical_resume_rows": resume_rows,
                    }
                ]
            return [
                {
                    "session_id": "session-1",
                    "run_id": "run-1",
                    "latest_seq_id": 1,
                    "canonical_creation_rows": creation_rows,
                }
            ]

    class FakePool:
        def __init__(self) -> None:
            self.connection = FakeConnection()

        async def acquire(self) -> FakeConnection:
            return self.connection

        async def release(self, connection: FakeConnection) -> None:
            return None

    class FakeSnapshot:
        def get(self):
            return None

    class FakeService:
        namespace = "default"
        _pool = FakePool()
        _checkpoint_snapshot_connection = FakeSnapshot()

        async def _ensure_schema(self) -> None:
            return None

    stats = await get_checkpoint_stats(FakeService(), [("session-1", "run-1", "checkpoint-1")])

    assert stats["audits"][("session-1", "run-1", "checkpoint-1")] == {
        "resume_count": 1,
        "last_resumed_at": 101.0,
    }
    assert stats["latest_seq_ids"][("session-1", "run-1")] == 1
