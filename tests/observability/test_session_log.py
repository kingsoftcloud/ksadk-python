from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ksadk.events.canonical import RunCompleted, RunProgress, RunStarted, RuntimeEvent, SourceRef, dump_runtime_event
from ksadk.events.store import RuntimeEventStore, runtime_event_to_session_event
from ksadk.events.v1_compat import EventTypeV1, RuntimeEventV1
from ksadk.observability.session_log import (
    SESSION_LOG_SCHEMA,
    SessionLogError,
    export_session_log,
    verify_session_log,
)
from ksadk.sessions.in_memory import InMemorySessionService


def make_event(
    event_id: str,
    event_type: str = "run.progress",
    *,
    invocation_id: str = "run-1",
) -> RuntimeEvent:
    envelope = {
        "schema_version": 2,
        "event_id": event_id,
        "seq": 0,
        "timestamp": 1.0,
        "run_id": invocation_id,
        "scope_id": "scope-1",
        "source": SourceRef(framework="ksadk"),
    }
    if event_type == "run.started":
        return RunStarted(status="running", **envelope)
    if event_type == "run.completed":
        return RunCompleted(status="completed", output_refs=(), **envelope)
    return RunProgress(status="running", **envelope)


async def make_store(service: InMemorySessionService | None = None):
    service = service or InMemorySessionService()
    await service.create_session("agent-1", "user-1", "session-1")
    return service, RuntimeEventStore(service)


@pytest.mark.asyncio
async def test_export_session_log_is_ordered_and_verifiable(tmp_path: Path):
    service, store = await make_store()
    await store.append_one("session-1", make_event("evt-1", "run.started"))
    await store.append_one("session-1", make_event("evt-2", "run.completed"))

    target = tmp_path / "session.jsonl"
    result = await export_session_log(service, "session-1", target)
    verified = verify_session_log(target)
    lines = [json.loads(line) for line in target.read_text().splitlines()]

    assert result.event_count == verified.event_count == 2
    assert result.first_seq_id == verified.first_seq_id == 1
    assert result.last_seq_id == verified.last_seq_id == 2
    assert lines[0]["type"] == "session"
    assert lines[0]["schema"] == SESSION_LOG_SCHEMA == "ksadk.session-log/v2"
    assert lines[0]["version"] == 2
    assert lines[0]["exported_through_seq_id"] == 2
    assert [line["event_id"] for line in lines[1:]] == ["evt-1", "evt-2"]
    assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_export_session_log_keeps_canonical_progress_events_lossless(tmp_path: Path):
    service, store = await make_store()
    for index in range(1, 5):
        await store.append_one("session-1", make_event(f"evt-{index}"))

    target = tmp_path / "session.jsonl"
    result = await export_session_log(service, "session-1", target)
    lines = [json.loads(line) for line in target.read_text().splitlines()]

    assert result.event_count == verify_session_log(target).event_count == 4
    assert [line["event_id"] for line in lines[1:]] == ["evt-1", "evt-2", "evt-3", "evt-4"]
    assert all(line["schema_version"] == 2 for line in lines[1:])


def test_verify_session_log_accepts_raw_v2_runtime_event_rows(tmp_path: Path):
    target = tmp_path / "raw.jsonl"
    event = make_event("evt-1").model_copy(update={"seq": 1})
    write_log(
        target,
        {
            "type": "session",
            "schema": "ksadk.session-log/v2",
            "version": 2,
            "session_id": "session-1",
            "agent_id": "agent-1",
            "user_id": "user-1",
            "exported_through_seq_id": 1,
        },
        [event],
    )

    assert verify_session_log(target).event_count == 1


def test_verify_session_log_retains_legacy_v1_read_compatibility(tmp_path: Path):
    target = tmp_path / "legacy-v1.jsonl"
    event = RuntimeEventV1.create(
        EventTypeV1.RUN_STARTED,
        agent_id="agent-1",
        user_id="user-1",
        session_id="session-1",
        invocation_id="run-1",
        seq_id=1,
        payload={"status": "running"},
        event_id="legacy-evt-1",
        timestamp=1.0,
    )
    target.write_text(
        "\n".join(
            json.dumps(value)
            for value in (
                {
                    "type": "session",
                    "schema": "ksadk.session-log/v1",
                    "version": 1,
                    "session_id": "session-1",
                    "agent_id": "agent-1",
                    "user_id": "user-1",
                    "exported_through_seq_id": 1,
                },
                event.to_dict(),
            )
        )
        + "\n"
    )

    verified = verify_session_log(target)

    assert verified.event_count == 1
    assert verified.first_seq_id == verified.last_seq_id == 1


@pytest.mark.asyncio
async def test_export_session_log_uses_fixed_watermark(tmp_path: Path):
    class AppendAfterCutoffService(InMemorySessionService):
        appended_after_cutoff = False

        async def get_events(self, session_id, *args, **kwargs):
            events = await super().get_events(session_id, *args, **kwargs)
            if (
                kwargs.get("after_seq_id") == 0
                and kwargs.get("before_seq_id") is None
                and not self.appended_after_cutoff
            ):
                self.appended_after_cutoff = True
                await self.append_event(
                    session_id,
                    runtime_event_to_session_event(session_id, make_event("evt-late")),
                )
            return events

    service, store = await make_store(AppendAfterCutoffService())
    await store.append_one("session-1", make_event("evt-1", "run.started"))
    await store.append_one("session-1", make_event("evt-2", "run.completed"))

    target = tmp_path / "session.jsonl"
    result = await export_session_log(service, "session-1", target)
    event_ids = [json.loads(line)["event_id"] for line in target.read_text().splitlines()[1:]]

    assert result.exported_through_seq_id == 2
    assert event_ids == ["evt-1", "evt-2"]
    assert [event.event_id for event in await store.list("session-1")] == [
        "evt-1",
        "evt-2",
        "evt-late",
    ]


@pytest.mark.asyncio
async def test_export_session_log_reads_at_most_500_events_per_page(tmp_path: Path):
    class PagingService(InMemorySessionService):
        requested_limits: list[int] = []

        async def get_events(self, session_id, *args, **kwargs):
            if kwargs.get("limit") is not None:
                self.requested_limits.append(kwargs["limit"])
            return await super().get_events(session_id, *args, **kwargs)

    service, store = await make_store(PagingService())
    for index in range(10_000):
        await store.append_one("session-1", make_event(f"evt-{index}"))

    result = await export_session_log(service, "session-1", tmp_path / "large.jsonl")

    assert result.event_count == 10_000
    assert max(service.requested_limits) <= 500
    assert len(service.requested_limits) > 2


@pytest.mark.asyncio
async def test_export_session_log_filters_invocation_without_renumbering(tmp_path: Path):
    service, store = await make_store()
    await store.append_one("session-1", make_event("evt-1", invocation_id="run-1"))
    await store.append_one("session-1", make_event("evt-2", invocation_id="run-2"))
    await store.append_one("session-1", make_event("evt-3", invocation_id="run-1"))

    target = tmp_path / "run-1.jsonl"
    result = await export_session_log(
        service,
        "session-1",
        target,
        invocation_id="run-1",
    )
    lines = [json.loads(line) for line in target.read_text().splitlines()]

    assert result.event_count == 2
    assert [line["seq"] for line in lines[1:]] == [1, 3]
    assert lines[0]["invocation_id"] == "run-1"
    assert verify_session_log(target).last_seq_id == 3


@pytest.mark.asyncio
async def test_export_session_log_rejects_missing_session_and_existing_target(tmp_path: Path):
    service, store = await make_store()
    await store.append_one("session-1", make_event("evt-1"))
    target = tmp_path / "existing.jsonl"
    target.write_text("keep")

    with pytest.raises(SessionLogError, match="SESSION_LOG_SESSION_NOT_FOUND"):
        await export_session_log(service, "missing", tmp_path / "missing.jsonl")
    with pytest.raises(SessionLogError, match="SESSION_LOG_TARGET_EXISTS"):
        await export_session_log(service, "session-1", target)

    assert target.read_text() == "keep"


@pytest.mark.asyncio
async def test_export_session_log_removes_partial_file_when_publish_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    service, store = await make_store()
    await store.append_one("session-1", make_event("evt-1"))
    target = tmp_path / "failed.jsonl"

    def fail_link(source, destination):
        raise OSError("hard links unavailable")

    monkeypatch.setattr(os, "link", fail_link)

    with pytest.raises(SessionLogError, match="SESSION_LOG_ATOMIC_PUBLISH_UNSUPPORTED"):
        await export_session_log(service, "session-1", target)

    assert not target.exists()
    assert list(tmp_path.glob(".session-log-*.tmp")) == []


def write_log(path: Path, header: dict, events: list[RuntimeEvent]) -> None:
    values = [header, *(dump_runtime_event(event) for event in events)]
    path.write_text("".join(json.dumps(value) + "\n" for value in values))


@pytest.mark.parametrize(
    ("header_update", "events", "message"),
    [
        ({"schema": "unsupported/v2"}, [], "unsupported schema"),
        (
            {"invocation_id": "run-1"},
            [make_event("evt-1", invocation_id="other").model_copy(update={"seq": 1})],
            "run id",
        ),
        (
            {"exported_through_seq_id": 3},
            [
                make_event("evt-1").model_copy(update={"seq": 1}),
                make_event("evt-3").model_copy(update={"seq": 3}),
            ],
            "continuous",
        ),
    ],
)
def test_verify_session_log_rejects_invalid_contract(
    tmp_path: Path,
    header_update: dict,
    events: list[RuntimeEvent],
    message: str,
):
    header = {
        "type": "session",
        "schema": SESSION_LOG_SCHEMA,
        "version": 2,
        "session_id": "session-1",
        "agent_id": "agent-1",
        "user_id": "user-1",
        "exported_through_seq_id": len(events),
    }
    header.update(header_update)
    target = tmp_path / "invalid.jsonl"
    write_log(target, header, events)

    with pytest.raises(SessionLogError, match=message):
        verify_session_log(target)
