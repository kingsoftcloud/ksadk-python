from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.observability.trajectory import encode_sse
from ksadk.studio.api import create_studio_app
from ksadk.studio.service import StudioService


def runtime_event(index: int, *, invocation_id: str = "run-1") -> RuntimeEvent:
    return RuntimeEvent.create(
        EventType.RUN_PROGRESS,
        agent_id="agent-1",
        user_id="user-1",
        session_id="session-1",
        invocation_id=invocation_id,
        seq_id=0,
        event_id=f"evt-{index}",
        payload={"status": "running", "progress": index},
    )


async def seed(service: StudioService, count: int = 10) -> None:
    await service.session_service.create_session("agent-1", "user-1", "session-1")
    for index in range(1, count + 1):
        await service.runtime_events.append_one(runtime_event(index))


def test_session_events_returns_tail_page_and_older_page(tmp_path: Path):
    service = StudioService(tmp_path)
    asyncio.run(seed(service))
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        tail = client.get("/api/v1/sessions/session-1/events?limit=3")
        older = client.get("/api/v1/sessions/session-1/events?limit=3&beforeSeqId=8")

    assert tail.status_code == 200
    assert [item["seqId"] for item in tail.json()["items"]] == [8, 9, 10]
    assert tail.json()["page"] == {
        "oldestSeqId": 8,
        "latestSeqId": 10,
        "hasMore": True,
    }
    assert all(item["projectionVersion"] == 1 for item in tail.json()["items"])
    assert all(item["recordId"] for item in tail.json()["items"])
    assert [item["seqId"] for item in older.json()["items"]] == [5, 6, 7]


def test_session_events_filters_invocation_before_applying_limit(tmp_path: Path):
    service = StudioService(tmp_path)

    async def seed_interleaved() -> None:
        await service.session_service.create_session("agent-1", "user-1", "session-1")
        for index, invocation_id in enumerate(
            ["run-1", "run-2", "run-1", "run-2", "run-2"],
            start=1,
        ):
            await service.runtime_events.append_one(
                runtime_event(index, invocation_id=invocation_id)
            )

    asyncio.run(seed_interleaved())
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        response = client.get("/api/v1/sessions/session-1/events?limit=2&invocationId=run-1")

    assert response.status_code == 200
    assert [item["seqId"] for item in response.json()["items"]] == [1, 3]
    assert response.json()["page"] == {
        "oldestSeqId": 1,
        "latestSeqId": 3,
        "hasMore": False,
    }


def test_session_events_validates_session_and_limit(tmp_path: Path):
    service = StudioService(tmp_path)
    asyncio.run(seed(service, count=1))
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        missing = client.get("/api/v1/sessions/missing/events")
        invalid_limit = client.get("/api/v1/sessions/session-1/events?limit=501")

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "SESSION_NOT_FOUND"
    assert invalid_limit.status_code == 422


def test_session_event_stream_prefers_last_event_id(tmp_path: Path, monkeypatch):
    service = StudioService(tmp_path)
    asyncio.run(seed(service, count=1))
    cursors: list[int] = []

    async def finite_stream(
        session_id: str,
        after_seq_id: int,
        *,
        invocation_id: str | None = None,
    ):
        cursors.append(after_seq_id)
        yield encode_sse({"seqId": after_seq_id + 1}, event_id=after_seq_id + 1)

    monkeypatch.setattr(service, "stream_trajectory", finite_stream)
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/sessions/session-1/events/stream?afterSeqId=2",
            headers={"Last-Event-ID": "7"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert cursors == [7]
    assert response.text == 'id: 8\nevent: runtime_event\ndata: {"seqId":8}\n\n'


@pytest.mark.asyncio
async def test_stream_trajectory_filters_invocation_and_advances_session_cursor(
    tmp_path: Path, monkeypatch
):
    service = StudioService(tmp_path)
    await service.session_service.create_session("agent-1", "user-1", "session-1")
    first = await service.runtime_events.append_one(runtime_event(1, invocation_id="run-1"))
    other = await service.runtime_events.append_one(runtime_event(2, invocation_id="run-2"))
    cursors: list[int] = []

    async def finite_subscription(session_id: str, *, after_seq_id: int, timeout: float):
        cursors.append(after_seq_id)
        if not cursors[:-1]:
            yield first
            yield other

    monkeypatch.setattr(service.runtime_events, "subscribe_session", finite_subscription)
    stream = service.stream_trajectory("session-1", invocation_id="run-1")

    frame = await anext(stream)
    keepalive = await anext(stream)
    next_keepalive = await anext(stream)
    await stream.aclose()

    assert frame.startswith(f"id: {first.seq_id}\n")
    assert keepalive == next_keepalive == ": keepalive\n\n"
    assert cursors == [0, other.seq_id]


@pytest.mark.asyncio
async def test_stream_trajectory_keeps_cursor_across_keepalive(tmp_path: Path, monkeypatch):
    service = StudioService(tmp_path)
    await seed(service, count=1)
    monkeypatch.setattr("ksadk.studio.service._TRAJECTORY_KEEPALIVE_SECONDS", 0.01)
    stream = service.stream_trajectory("session-1", after_seq_id=1)

    assert await asyncio.wait_for(anext(stream), timeout=0.2) == ": keepalive\n\n"
    stored = await service.runtime_events.append_one(runtime_event(2))
    frame = await asyncio.wait_for(anext(stream), timeout=0.2)
    await stream.aclose()

    assert frame.startswith(f"id: {stored.seq_id}\n")
    assert json.loads(frame.split("data: ", 1)[1])["seqId"] == stored.seq_id


def test_session_export_api_restricts_target_to_workspace_exports(tmp_path: Path):
    service = StudioService(tmp_path)
    asyncio.run(seed(service, count=2))
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        exported = client.post(
            "/api/v1/sessions/session-1:export",
            json={"filename": "session-1.jsonl", "invocationId": "run-1"},
        )
        absolute = client.post(
            "/api/v1/sessions/session-1:export",
            json={"filename": str(tmp_path / "escape.jsonl")},
        )
        traversal = client.post(
            "/api/v1/sessions/session-1:export",
            json={"filename": "../escape.jsonl"},
        )

    assert exported.status_code == 200
    assert exported.json()["eventCount"] == 2
    assert exported.json()["path"] == ".agentkit/exports/session-1.jsonl"
    target = tmp_path / exported.json()["path"]
    assert target.is_file()
    assert target.parent.stat().st_mode & 0o777 == 0o700
    assert absolute.status_code == traversal.status_code == 422


def test_session_export_api_downloads_jsonl_without_leaving_staging_file(tmp_path: Path):
    service = StudioService(tmp_path)
    asyncio.run(seed(service, count=2))
    app = create_studio_app(tmp_path, service=service, security_enabled=False)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/sessions/session-1:export",
            json={
                "filename": "session-1.jsonl",
                "invocationId": "run-1",
                "download": True,
            },
        )

    lines = [json.loads(line) for line in response.text.splitlines()]
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-ndjson"
    assert response.headers["x-session-event-count"] == "2"
    assert "attachment" in response.headers["content-disposition"]
    assert lines[0]["schema"] == "ksadk.session-log/v1"
    assert lines[0]["version"] == 1
    assert [line["event_id"] for line in lines[1:]] == ["evt-1", "evt-2"]
    assert not (tmp_path / ".agentkit/exports/session-1.jsonl").exists()
