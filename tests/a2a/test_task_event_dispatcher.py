from __future__ import annotations

import asyncio

import pytest

from ksadk.a2a.task_event_dispatcher import A2ATaskEventDispatcher
from ksadk.a2a.task_event_outbox import SQLiteA2ATaskEventOutbox


class _TaskSink:
    def __init__(self) -> None:
        self.available = False
        self.calls: list[dict] = []

    async def append_task_events(self, *, platform_task_id, events):  # noqa: ANN001
        if not self.available:
            raise RuntimeError("task sink unavailable")
        self.calls.append({"platform_task_id": platform_task_id, "events": events})
        return {"AcceptedCount": len(events)}


@pytest.mark.asyncio
async def test_dispatcher_replays_outbox_on_start_and_retries_in_background(tmp_path) -> None:
    outbox = SQLiteA2ATaskEventOutbox(tmp_path / "events.sqlite3")
    sink = _TaskSink()
    dispatcher = A2ATaskEventDispatcher(outbox, sink, retry_interval_seconds=0.01)

    await dispatcher.enqueue(
        platform_task_id="a2a-task-00000000000040008000000000000004",
        events=[{"SourceEventId": "event-1", "EventKind": "status"}],
    )
    assert len(await outbox.pending()) == 1
    assert dispatcher.degraded is True

    sink.available = True
    await dispatcher.start()
    for _ in range(100):
        if not await outbox.pending():
            break
        await asyncio.sleep(0.01)
    await dispatcher.stop(flush_timeout_seconds=0.1)

    assert await outbox.pending() == []
    assert sink.calls[0]["events"][0]["SourceEventId"] == "event-1"
