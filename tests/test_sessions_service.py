from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

import pytest

from ksadk.sessions import (
    close_session_service,
    create_session_service,
    get_session_service,
    reset_session_service,
    resolve_session_service,
)
from ksadk.sessions.base import SessionEvent, SessionEventQuery
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.sessions.local_service import LocalSessionService
from ksadk.sessions.resilient import ResilientSessionService


@pytest.mark.asyncio
async def test_in_memory_session_service_crud_append_event_and_state_updates():
    service = InMemorySessionService()

    session = await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
    )

    assert session.id == "sess-1"
    assert session.agent_id == "demo-agent"
    assert session.user_id == "user-1"
    assert session.title == ""
    assert session.title_source == ""
    assert session.summary == ""
    assert session.first_prompt == ""
    assert session.last_prompt == ""
    assert session.state == {}

    appended = await service.append_event(
        "sess-1",
        SessionEvent(
            id="evt-1",
            author="user",
            event_type="text",
            content={"role": "user", "parts": [{"text": "hello"}]},
            state_delta={"turns": 1},
        ),
    )

    assert appended.id == "evt-1"
    assert appended.content["parts"][0]["text"] == "hello"

    fetched = await service.get_session("sess-1")
    assert fetched is not None
    assert fetched.state == {"turns": 1}

    updated = await service.update_state(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
        scope="session",
        state_delta={"topic": "billing"},
    )
    assert updated.scope == "session"
    assert updated.state == {"turns": 1, "topic": "billing"}
    assert updated.version == 2

    state = await service.get_state(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
        scope="session",
    )
    assert state is not None
    assert state.state == {"turns": 1, "topic": "billing"}

    listed = await service.list_sessions(agent_id="demo-agent", user_id="user-1")
    assert [item.id for item in listed] == ["sess-1"]

    events = await service.get_events("sess-1")
    assert [event.id for event in events] == ["evt-1"]

    assert await service.delete_session("sess-1") is True
    assert await service.get_session("sess-1") is None


@pytest.mark.asyncio
async def test_in_memory_session_service_get_events_pages_from_latest_and_returns_ascending():
    service = InMemorySessionService()
    await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
    )

    for index in range(4):
        await service.append_event(
            "sess-1",
            SessionEvent(
                id=f"evt-{index + 1}",
                author="user",
                event_type="text",
                content={"index": index},
            ),
        )

    newest_page = await service.get_events("sess-1", offset=0, limit=2)
    older_page = await service.get_events("sess-1", offset=2, limit=2)
    without_latest = await service.get_events("sess-1", offset=2)

    assert [event.seq_id for event in newest_page] == [3, 4]
    assert [event.seq_id for event in older_page] == [1, 2]
    assert [event.seq_id for event in without_latest] == [1, 2]


@pytest.mark.asyncio
async def test_local_session_service_get_events_pages_from_latest_and_returns_ascending(tmp_path):
    service = LocalSessionService(db_path=tmp_path / "sessions.sqlite")
    await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
    )

    for index in range(4):
        await service.append_event(
            "sess-1",
            SessionEvent(
                id=f"evt-{index + 1}",
                author="user",
                event_type="text",
                content={"index": index},
            ),
        )

    newest_page = await service.get_events("sess-1", offset=0, limit=2)
    older_page = await service.get_events("sess-1", offset=2, limit=2)
    without_latest = await service.get_events("sess-1", offset=2)

    assert [event.seq_id for event in newest_page] == [3, 4]
    assert [event.seq_id for event in older_page] == [1, 2]
    assert [event.seq_id for event in without_latest] == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "local"])
async def test_batch_events_are_globally_sorted_and_bounded(backend, tmp_path):
    service = (
        InMemorySessionService()
        if backend == "memory"
        else LocalSessionService(db_path=tmp_path / "batch.sqlite")
    )
    await service.create_session("agent-a", "user", "batch-a")
    await service.create_session("agent-a", "user", "batch-b")
    await service.create_session("agent-b", "user", "batch-other")
    await service.append_event("batch-a", SessionEvent(id="a-1", event_type="keep", timestamp=1))
    await service.append_event("batch-b", SessionEvent(id="b-1", event_type="keep", timestamp=2))
    await service.append_event("batch-a", SessionEvent(id="a-2", event_type="keep", timestamp=3))
    await service.append_event("batch-other", SessionEvent(id="other", event_type="keep", timestamp=4))

    page = await service.get_events_batch(agent_id="agent-a", offset=1, limit=1)
    count = await service.count_events_batch(agent_id="agent-a")
    metadata = await service.get_sessions_by_ids(["batch-b", "missing", "batch-a"])

    assert [event.id for event in page] == ["b-1"]
    assert count == 3
    assert [session.id for session in metadata] == ["batch-b", "batch-a"]


@pytest.mark.asyncio
async def test_lightweight_session_metadata_does_not_copy_event_history():
    service = InMemorySessionService()
    await service.create_session("agent-a", "user", "metadata")
    await service.append_event("metadata", SessionEvent(id="history", event_type="x"))

    metadata = await service.get_session_metadata("metadata")

    assert metadata is not None
    assert metadata.id == "metadata"
    assert metadata.events == []


@pytest.mark.asyncio
async def test_in_memory_list_sessions_keeps_legacy_event_history_shape():
    service = InMemorySessionService()
    await service.create_session("agent-a", "user", "legacy-list")
    await service.append_event("legacy-list", SessionEvent(id="event", event_type="x"))

    listed = await service.list_sessions("agent-a")
    metadata = await service.list_session_metadata("agent-a")

    assert [event.id for event in listed[0].events] == ["event"]
    assert metadata[0].events == []


@pytest.mark.asyncio
async def test_event_query_filters_run_and_checkpoint_before_returning_page():
    service = InMemorySessionService()
    await service.create_session("agent-a", "user", "query")
    for index in range(3):
        await service.append_event(
            "query",
            SessionEvent(
                event_type="run_checkpoint", timestamp=index,
                metadata={"run_id": "other", "checkpoint_id": f"other-{index}"},
            ),
        )
    await service.append_event(
        "query",
        SessionEvent(
            id="target", event_type="run_checkpoint", timestamp=10,
            metadata={"run_id": "run", "checkpoint_id": "checkpoint"},
        ),
    )

    events = await service.query_events(
        SessionEventQuery(
            session_ids=["query"], event_types=["run_checkpoint"], run_id="run",
            checkpoint_id="checkpoint", limit=1,
        )
    )

    assert [event.id for event in events] == ["target"]


@pytest.mark.asyncio
async def test_checkpoint_lookup_stats_are_exact_beyond_500_events():
    service = InMemorySessionService()
    await service.create_session("agent-a", "user", "stats")
    await service.append_event("stats", SessionEvent(id="old", event_type="run_checkpoint", metadata={"run_id": "run", "checkpoint_id": "same"}))
    for index in range(501):
        await service.append_event("stats", SessionEvent(event_type="run_resume", timestamp=index, metadata={"run_id": "run", "checkpoint_id": "same"}))
    await service.append_event("stats", SessionEvent(id="new", event_type="run_checkpoint", metadata={"run_id": "run", "checkpoint_id": "same"}))

    stats = await service.get_checkpoint_lookup_stats("stats", "run", "same")

    assert stats["candidate"].id == "new"
    assert stats["max_seq_id"] == 503
    assert stats["resume_count"] == 501


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "local"])
async def test_checkpoint_scan_pushes_combined_filters_before_bounded_page(backend, tmp_path):
    from ksadk.sessions.base import CheckpointEventQuery

    service = (
        InMemorySessionService()
        if backend == "memory"
        else LocalSessionService(db_path=tmp_path / "checkpoint-scan.sqlite")
    )
    for session_id, agent_id in (
        ("scan-a", "agent-a"),
        ("scan-b", "agent-a"),
        ("scan-other", "agent-b"),
    ):
        await service.create_session(agent_id, "user", session_id)
    fixtures = (
        ("scan-a", "a-target", 1, "run-1", "same", "langgraph"),
        ("scan-b", "b-target", 2, "run-1", "same", "langgraph"),
        ("scan-a", "wrong-checkpoint", 3, "run-1", "other", "langgraph"),
        ("scan-a", "wrong-run", 4, "run-2", "same", "langgraph"),
        ("scan-a", "wrong-framework", 5, "run-1", "same", "adk"),
        ("scan-other", "wrong-agent", 6, "run-1", "same", "langgraph"),
    )
    for session_id, event_id, timestamp, run_id, checkpoint_id, framework in fixtures:
        await service.append_event(
            session_id,
            SessionEvent(
                id=event_id,
                event_type="run_checkpoint",
                timestamp=timestamp,
                metadata={
                    "run_id": run_id,
                    "checkpoint_id": checkpoint_id,
                    "framework": framework,
                },
            ),
        )

    page = await service.scan_checkpoint_events(
        CheckpointEventQuery(
            session_ids=["scan-a", "scan-b", "scan-other"],
            agent_id="agent-a",
            checkpoint_ids=["same"],
            run_id="run-1",
            framework="langgraph",
            limit=50,
        )
    )

    assert [(event.session_id, event.id) for event in page] == [
        ("scan-a", "a-target"),
        ("scan-b", "b-target"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "local"])
async def test_checkpoint_stats_batch_isolates_session_audits_and_latest_runs(backend, tmp_path):
    service = (
        InMemorySessionService()
        if backend == "memory"
        else LocalSessionService(db_path=tmp_path / "checkpoint-stats.sqlite")
    )
    for session_id in ("stats-a", "stats-b"):
        await service.create_session("agent-a", "user", session_id)
        await service.append_event(
            session_id,
            SessionEvent(
                event_type="run_checkpoint",
                metadata={"run_id": "same-run", "checkpoint_id": "same-checkpoint"},
            ),
        )
    await service.append_event(
        "stats-a",
        SessionEvent(
            event_type="run_resume",
            timestamp=20,
            metadata={"run_id": "same-run", "checkpoint_id": "same-checkpoint"},
        ),
    )
    await service.append_event(
        "stats-a",
        SessionEvent(
            event_type="run_checkpoint",
            timestamp=30,
            metadata={"run_id": "same-run", "checkpoint_id": "new-checkpoint"},
        ),
    )

    stats = await service.get_checkpoint_stats(
        [
            ("stats-a", "same-run", "same-checkpoint"),
            ("stats-b", "same-run", "same-checkpoint"),
        ]
    )

    assert stats["audits"][("stats-a", "same-run", "same-checkpoint")]["resume_count"] == 1
    assert stats["audits"][("stats-b", "same-run", "same-checkpoint")]["resume_count"] == 0
    assert stats["latest_seq_ids"][("stats-a", "same-run")] == 3
    assert stats["latest_seq_ids"][("stats-b", "same-run")] == 1


@pytest.mark.asyncio
async def test_resilient_checkpoint_scan_merges_primary_and_dirty_live_in_stable_order():
    from ksadk.sessions.base import CheckpointEventQuery

    primary = InMemorySessionService()
    fallback = InMemorySessionService()
    service = ResilientSessionService(primary, fallback)
    await primary.create_session("agent-a", "user", "clean")
    await fallback.create_session("agent-a", "user", "dirty")
    await primary.append_event(
        "clean", SessionEvent(id="clean-2", event_type="run_checkpoint", timestamp=2,
                              metadata={"run_id": "run", "checkpoint_id": "cp"})
    )
    await fallback.append_event(
        "dirty", SessionEvent(id="dirty-1", event_type="run_checkpoint", timestamp=1,
                              metadata={"run_id": "run", "checkpoint_id": "cp"})
    )
    service._dirty_session_ids.add("dirty")

    page = await service.scan_checkpoint_events(
        CheckpointEventQuery(agent_id="agent-a", offset=0, limit=50)
    )
    stats = await service.get_checkpoint_stats(
        [(event.session_id, "run", "cp") for event in page]
    )

    assert [event.id for event in page] == ["dirty-1", "clean-2"]
    assert stats["latest_seq_ids"] == {("dirty", "run"): 1, ("clean", "run"): 1}


@pytest.mark.asyncio
async def test_resilient_checkpoint_scan_pushes_nonzero_offset_to_clean_primary_once():
    from ksadk.sessions.base import CheckpointEventQuery

    class TrackingPrimary(InMemorySessionService):
        def __init__(self):
            super().__init__()
            self.checkpoint_queries = []

        async def scan_checkpoint_events(self, query):
            self.checkpoint_queries.append(query)
            return await super().scan_checkpoint_events(query)

    primary = TrackingPrimary()
    service = ResilientSessionService(primary, InMemorySessionService())
    await primary.create_session("agent-a", "user", "clean")
    for index in range(70):
        await primary.append_event(
            "clean",
            SessionEvent(
                id=f"cp-{index}", event_type="run_checkpoint", timestamp=index,
                metadata={"run_id": "run", "checkpoint_id": f"cp-{index}"},
            ),
        )

    page = await service.scan_checkpoint_events(
        CheckpointEventQuery(agent_id="agent-a", offset=60, limit=10)
    )

    assert [event.id for event in page] == [f"cp-{index}" for index in range(60, 70)]
    assert [(query.offset, query.limit) for query in primary.checkpoint_queries] == [(60, 10)]


@pytest.mark.asyncio
async def test_resilient_query_merges_primary_and_live_once_before_nonzero_offset_page():
    primary = InMemorySessionService()
    fallback = InMemorySessionService()
    service = ResilientSessionService(primary, fallback)
    await primary.create_session("agent-a", "user", "primary")
    await fallback.create_session("agent-a", "user", "live")
    await primary.append_event("primary", SessionEvent(id="p1", timestamp=1))
    await primary.append_event("primary", SessionEvent(id="p3", timestamp=3))
    await fallback.append_event("live", SessionEvent(id="l2", timestamp=2))
    await fallback.append_event("live", SessionEvent(id="l4", timestamp=4))
    service._dirty_session_ids.add("live")

    events = await service.query_events(SessionEventQuery(agent_id="agent-a", offset=1, limit=2, from_start=True))

    assert [event.id for event in events] == ["l2", "p3"]


@pytest.mark.asyncio
async def test_resilient_query_uses_live_fallback_for_real_primary_backend_failure():
    class _FailingQueryPrimary(InMemorySessionService):
        async def query_events(self, query):
            raise ConnectionError("primary unavailable")

    primary = _FailingQueryPrimary()
    fallback = InMemorySessionService()
    service = ResilientSessionService(primary, fallback)
    for backend in (primary, fallback):
        await backend.create_session("agent-a", "user", "degraded")
    await fallback.append_event("degraded", SessionEvent(id="live", timestamp=1))

    events = await service.query_events(
        SessionEventQuery(session_ids=["degraded"], limit=10, from_start=True)
    )

    assert [event.id for event in events] == ["live"]
    assert service.degraded is True


@pytest.mark.asyncio
async def test_in_memory_session_service_get_events_filters_by_after_seq_id():
    service = InMemorySessionService()
    await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-after",
    )
    for index in range(4):
        await service.append_event(
            "sess-after",
            SessionEvent(
                id=f"evt-{index + 1}",
                author="user",
                event_type="text",
                content={"index": index},
            ),
        )

    all_events = await service.get_events("sess-after")
    assert [event.seq_id for event in all_events] == [1, 2, 3, 4]

    after2 = await service.get_events("sess-after", after_seq_id=2)
    assert [event.seq_id for event in after2] == [3, 4]

    after0 = await service.get_events("sess-after", after_seq_id=0)
    assert [event.seq_id for event in after0] == [1, 2, 3, 4]

    after_max = await service.get_events("sess-after", after_seq_id=4)
    assert [event.seq_id for event in after_max] == []

    # after_seq_id + limit: 先 seq 过滤得 [3,4],再"最新 1 条"得 [4]
    after_limit = await service.get_events("sess-after", after_seq_id=2, limit=1)
    assert [event.seq_id for event in after_limit] == [4]


@pytest.mark.asyncio
async def test_in_memory_session_service_get_events_filters_by_before_seq_id():
    service = InMemorySessionService()
    await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-before",
    )
    for index in range(5):
        await service.append_event(
            "sess-before",
            SessionEvent(
                id=f"evt-{index + 1}",
                author="user",
                event_type="text",
                content={"index": index},
            ),
        )

    before4 = await service.get_events("sess-before", before_seq_id=4)
    assert [event.seq_id for event in before4] == [1, 2, 3]
    assert await service.count_events("sess-before", before_seq_id=4) == 3

    # before_seq_id + limit keeps the latest N events from the older-history window.
    before4_limit = await service.get_events("sess-before", before_seq_id=4, limit=2)
    assert [event.seq_id for event in before4_limit] == [2, 3]

    before1 = await service.get_events("sess-before", before_seq_id=1)
    assert before1 == []


@pytest.mark.asyncio
async def test_local_session_service_get_events_filters_by_after_seq_id(tmp_path):
    service = LocalSessionService(db_path=tmp_path / "sessions.sqlite")
    await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-after",
    )
    for index in range(4):
        await service.append_event(
            "sess-after",
            SessionEvent(
                id=f"evt-{index + 1}",
                author="user",
                event_type="text",
                content={"index": index},
            ),
        )

    all_events = await service.get_events("sess-after")
    assert [event.seq_id for event in all_events] == [1, 2, 3, 4]

    after2 = await service.get_events("sess-after", after_seq_id=2)
    assert [event.seq_id for event in after2] == [3, 4]

    after0 = await service.get_events("sess-after", after_seq_id=0)
    assert [event.seq_id for event in after0] == [1, 2, 3, 4]

    after_max = await service.get_events("sess-after", after_seq_id=4)
    assert [event.seq_id for event in after_max] == []

    after_limit = await service.get_events("sess-after", after_seq_id=2, limit=1)
    assert [event.seq_id for event in after_limit] == [4]


@pytest.mark.asyncio
async def test_local_session_service_get_events_filters_by_before_seq_id(tmp_path):
    service = LocalSessionService(db_path=tmp_path / "sessions.sqlite")
    await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-before",
    )
    for index in range(5):
        await service.append_event(
            "sess-before",
            SessionEvent(
                id=f"evt-{index + 1}",
                author="user",
                event_type="text",
                content={"index": index},
            ),
        )

    before4 = await service.get_events("sess-before", before_seq_id=4)
    assert [event.seq_id for event in before4] == [1, 2, 3]
    assert await service.count_events("sess-before", before_seq_id=4) == 3

    before4_limit = await service.get_events("sess-before", before_seq_id=4, limit=2)
    assert [event.seq_id for event in before4_limit] == [2, 3]

    before1 = await service.get_events("sess-before", before_seq_id=1)
    assert before1 == []


@pytest.mark.asyncio
async def test_in_memory_session_service_create_session_is_idempotent_for_existing_explicit_id():
    service = InMemorySessionService()
    created = await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
    )
    await service.append_event(
        "sess-1",
        SessionEvent(
            id="evt-1",
            author="user",
            event_type="text",
            content={"role": "user", "parts": [{"text": "hello"}]},
            state_delta={"turns": 1},
        ),
    )

    fetched_before = await service.get_session("sess-1")
    recreated = await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
    )
    fetched_after = await service.get_session("sess-1")

    assert fetched_before is not None
    assert fetched_after is not None
    assert recreated.id == "sess-1"
    assert recreated.created_at == created.created_at
    assert recreated.state == {"turns": 1}
    assert [event.id for event in recreated.events] == ["evt-1"]
    assert fetched_after.created_at == fetched_before.created_at
    assert fetched_after.state == {"turns": 1}
    assert [event.id for event in await service.get_events("sess-1")] == ["evt-1"]


@pytest.mark.asyncio
async def test_sqlite_session_service_persists_sessions_events_and_state(tmp_path):
    db_path = tmp_path / "sessions.sqlite"
    service = LocalSessionService(db_path=db_path)

    session = await service.create_session(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
    )
    await service.append_event(
        "sess-1",
        SessionEvent(
            id="evt-1",
            author="user",
            event_type="text",
            content={"role": "user", "parts": [{"text": "hello"}]},
            state_delta={"turns": 1},
        ),
    )
    await service.update_state(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
        scope="session",
        state_delta={"topic": "billing"},
    )
    await service.aclose()

    reopened = LocalSessionService(db_path=db_path)
    fetched = await reopened.get_session("sess-1")
    assert fetched is not None
    assert fetched.id == session.id
    assert fetched.title == ""
    assert fetched.title_source == ""
    assert fetched.summary == ""
    assert fetched.first_prompt == ""
    assert fetched.last_prompt == ""
    assert fetched.state == {"turns": 1, "topic": "billing"}

    events = await reopened.get_events("sess-1")
    assert [event.id for event in events] == ["evt-1"]
    assert events[0].content["parts"][0]["text"] == "hello"

    state = await reopened.get_state(
        agent_id="demo-agent",
        user_id="user-1",
        session_id="sess-1",
        scope="session",
    )
    assert state is not None
    assert state.state == {"turns": 1, "topic": "billing"}

    await reopened.aclose()


@pytest.mark.asyncio
async def test_in_memory_and_local_session_services_sort_by_updated_at_desc_and_preserve_metadata(tmp_path):
    memory_service = InMemorySessionService()
    local_service = LocalSessionService(db_path=tmp_path / "sessions.sqlite")

    for service in (memory_service, local_service):
        await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-old")
        await service.create_session(agent_id="demo-agent", user_id="user-1", session_id="sess-new")
        await service.update_session_metadata(
            "sess-old",
            title="老会话",
            title_source="fallback_first_prompt",
            summary="旧摘要",
            first_prompt="最早问题",
            last_prompt="最近更新",
        )
        await service.append_event(
            "sess-old",
            SessionEvent(
                id="evt-refresh",
                author="user",
                event_type="user_message",
                content={"role": "user", "parts": [{"text": "刷新排序"}]},
            ),
        )

        listed = await service.list_sessions("demo-agent", "user-1")
        assert [item.id for item in listed] == ["sess-old", "sess-new"]
        assert listed[0].title == "老会话"
        assert listed[0].title_source == "fallback_first_prompt"
        assert listed[0].summary == "旧摘要"
        assert listed[0].first_prompt == "最早问题"
        assert listed[0].last_prompt == "最近更新"

    await local_service.aclose()


@pytest.mark.asyncio
async def test_resolve_session_service_defaults_to_local_backend(monkeypatch, tmp_path):
    module = importlib.import_module("ksadk.sessions")
    monkeypatch.delenv("AGENTENGINE_SESSION_ENDPOINT", raising=False)
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / ".agentengine" / "ui"))

    await reset_session_service()
    module._cached_session_service = None

    service = resolve_session_service()

    assert isinstance(service, LocalSessionService)
    assert Path(service.db_path).parent == tmp_path / ".agentengine" / "ui"

    await reset_session_service()


def test_resolve_session_service_auto_selects_implementation(monkeypatch):
    monkeypatch.delenv("AGENTENGINE_SESSION_BACKEND", raising=False)
    monkeypatch.setattr("ksadk.sessions._cached_session_service", None)

    service = resolve_session_service()
    assert isinstance(service, LocalSessionService)

    monkeypatch.setenv("AGENTENGINE_SESSION_BACKEND", "memory")
    monkeypatch.setattr("ksadk.sessions._cached_session_service", None)

    service = resolve_session_service()
    assert isinstance(service, InMemorySessionService)


def test_create_session_service_hides_backend_selection(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / ".agentengine" / "ui"))

    local_service = create_session_service()
    assert isinstance(local_service, LocalSessionService)

    memory_service = create_session_service(backend="memory")
    assert isinstance(memory_service, InMemorySessionService)


@pytest.mark.asyncio
async def test_legacy_session_service_aliases_remain_compatible(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTENGINE_UI_DIR", str(tmp_path / ".agentengine" / "ui"))
    monkeypatch.setattr("ksadk.sessions._cached_session_service", None)

    service = get_session_service()
    assert isinstance(service, LocalSessionService)
    assert get_session_service() is resolve_session_service()

    await close_session_service()


def test_legacy_sqlite_service_import_path_remains_available():
    module = importlib.import_module("ksadk.sessions.sqlite_service")
    assert module.LocalSessionService is LocalSessionService


@pytest.mark.asyncio
async def test_local_session_service_closes_sqlite_connections(monkeypatch, tmp_path):
    connections: list[sqlite3.Connection] = []

    class TrackingConnection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.closed = False

        def close(self):
            self.closed = True
            return super().close()

    original_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        kwargs["factory"] = TrackingConnection
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)

    service = LocalSessionService(db_path=tmp_path / "sessions.sqlite")
    session = await service.create_session("demo-agent", "user-1", "sess-1")
    await service.append_event(
        session.id,
        SessionEvent(
            id="evt-1",
            author="user",
            event_type="user_message",
            content={"role": "user", "parts": [{"text": "hello"}]},
        ),
    )
    await service.get_session(session.id)
    await service.list_sessions("demo-agent")
    await service.get_events(session.id)
    await service.delete_session(session.id)

    assert connections
    assert all(getattr(connection, "closed", False) for connection in connections)
