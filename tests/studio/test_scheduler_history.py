"""Scheduled Kernel output remains visible through the shared Studio history."""

from datetime import datetime, timezone

import pytest

from ksadk.events.canonical import (
    ContentSnapshot,
    ItemCompleted,
    RunCompleted,
    RunStarted,
    SourceRef,
)
from ksadk.events.canonical_store import RuntimeEventStore
from ksadk.events.content import TextContent
from ksadk.studio.contracts import RunRecord, RunStatus
from ksadk.studio.errors import StudioError
from ksadk.studio.service import StudioService
from ksadk.studio.shared_web import StudioSharedWebBridge


@pytest.mark.asyncio
async def test_scheduler_history_replays_beside_foreground_turn_without_duplicates(tmp_path):
    studio = StudioService(tmp_path)
    try:
        await studio.session_service.create_session("agent", "local-studio", "scheduled")
        await studio.session_service.create_session("agent", "another-user", "unrelated")
        bridge = StudioSharedWebBridge(studio)
        common = dict(
            schema_version=2,
            seq=0,
            run_id="kernel-run",
            scope_id="kernel-run",
            source=SourceRef(framework="ksadk"),
        )
        store = RuntimeEventStore(studio.session_service)
        await store.append(
            "scheduled",
            [
                RunStarted(event_id="start", timestamp=10, status="running", **common),
                ItemCompleted(
                    event_id="answer",
                    timestamp=11,
                    item_id="answer",
                    item_kind="message",
                    snapshot=ContentSnapshot(
                        parts=(TextContent(part_id="text", text="定时执行结果"),)
                    ),
                    **common,
                ),
                RunCompleted(
                    event_id="done", timestamp=12, status="completed", output_refs=(), **common
                ),
            ],
        )
        assert studio.event_store.list_runs() == []
        sessions = await bridge.list_sessions("agent")
        assert [item["SessionId"] for item in sessions["Sessions"]] == ["scheduled"]
        messages = await bridge.list_messages("scheduled")
        assert [item["Content"]["text"] for item in messages["Messages"]] == ["定时执行结果"]
        assert messages["Messages"][0]["InvocationId"] == "kernel-run"
        events = await bridge.list_session_events("scheduled")
        assert [item["EventType"] for item in events["Events"]] == [
            "run.started",
            "message.completed",
            "run.completed",
        ]
        stream = [
            item
            async for item in bridge.subscribe_run_events("scheduled", "kernel-run", after_seq_id=1)
        ]
        assert "message.completed" in stream[0]
        assert "[DONE]" in stream[-1]
        with pytest.raises(StudioError):
            await bridge.subscription_run_id("unrelated", "kernel-run")

        record = RunRecord(
            id="run_foreground",
            build_id="b",
            agent_id="agent",
            session_id="scheduled",
            trace_id="t",
            input="继续分析",
            output="人工追问结果",
            status=RunStatus.COMPLETED,
            started_at=datetime.fromtimestamp(20, timezone.utc),
            completed_at=datetime.fromtimestamp(22, timezone.utc),
            runtime_handle={"run_id": "native-foreground"},
        )
        studio.event_store.create(record)
        await store.append(
            "scheduled",
            [
                ItemCompleted(
                    schema_version=2,
                    seq=0,
                    event_id="foreground-answer",
                    timestamp=21,
                    run_id="native-foreground",
                    scope_id="native-foreground",
                    source=SourceRef(framework="ksadk"),
                    item_id="answer",
                    item_kind="message",
                    snapshot=ContentSnapshot(
                        parts=(TextContent(part_id="text", text="人工追问结果"),)
                    ),
                ),
            ],
        )
        mixed = await bridge.list_messages("scheduled")
        assert [item["Content"]["text"] for item in mixed["Messages"]] == [
            "定时执行结果",
            "继续分析",
            "人工追问结果",
        ]
        first_page = await bridge.list_messages("scheduled", limit=2)
        assert first_page["HasMore"] is True
        previous = await bridge.list_messages("scheduled", before_seq_id=first_page["NextCursor"])
        assert [item["Content"]["text"] for item in previous["Messages"]] == ["定时执行结果"]
    finally:
        await studio.aclose()
