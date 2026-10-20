"""PostgreSQL-backed regression tests for bounded runtime history."""

from __future__ import annotations

import asyncio
import math
import os
import time
import uuid

import asyncpg
import httpx
import pytest

from ksadk.server.composition import configure_runtime_app
from ksadk.server.factory import RuntimeAppConfig, create_runtime_app
from ksadk.sessions.base import SessionEvent
from ksadk.sessions.db_budget import DatabaseBudget, budgeted_pool_connection
from ksadk.sessions.errors import SessionBackendUnavailable
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.sessions.postgres_service import PostgresSessionService
from ksadk.sessions.resilience import is_session_backend_failure
from ksadk.sessions.resilient import ResilientSessionService


@pytest.fixture
def pg_dsn() -> str:
    value = os.getenv("KSADK_TEST_POSTGRES_DSN")
    if not value:
        pytest.fail("KSADK_TEST_POSTGRES_DSN is required for runtime DB acceptance")
    return value


@pytest.mark.asyncio
async def test_clean_history_uses_durable_cross_instance_and_fails_closed(pg_dsn):
    namespace = "history_" + uuid.uuid4().hex[:16]
    primary_a = PostgresSessionService(dsn=pg_dsn, namespace=namespace)
    primary_b = PostgresSessionService(dsn=pg_dsn, namespace=namespace)
    service_a = ResilientSessionService(primary_a)
    service_b = ResilientSessionService(primary_b)
    try:
        await service_a.create_session("agent", "user", "session")
        await service_b.append_event("session", SessionEvent(id="b-1", event_type="user_message"))
        assert [event.id for event in await service_a.get_events("session")] == ["b-1"]
        assert (await service_a.get_state("agent", "user", "session", "session")).state == {}
        assert [event.seq_id for event in await service_a.get_events("session")] == [1]

        # A new process with no complete live mirror must not report an empty
        # successful history when its durable read is unavailable.
        cold = ResilientSessionService(primary_a, InMemorySessionService())
        cold._primary_enabled = False
        with pytest.raises(SessionBackendUnavailable):
            await cold.count_events("session")
        with pytest.raises(SessionBackendUnavailable):
            await cold.get_events("session", limit=50)
        with pytest.raises(SessionBackendUnavailable):
            await cold.append_event("session", SessionEvent(id="cold-write"))
        assert await primary_a.count_events("session") == 1
    finally:
        await primary_a.delete_session("session")
        await service_a.aclose()
        await service_b.aclose()


@pytest.mark.asyncio
async def test_dirty_live_history_remains_authority_after_recovery(pg_dsn):
    namespace = "dirty_" + uuid.uuid4().hex[:16]
    primary = PostgresSessionService(dsn=pg_dsn, namespace=namespace)
    service = ResilientSessionService(primary)
    try:
        await service.create_session("agent", "user", "session")
        await service.append_event("session", SessionEvent(id="durable", event_type="user_message"))
        original = primary.append_event

        async def unavailable(*_args, **_kwargs):
            raise TimeoutError("test transport loss")

        primary.append_event = unavailable
        await service.append_event("session", SessionEvent(id="live", event_type="assistant_message"))
        primary.append_event = original
        service._primary_enabled = True  # recovery probe succeeded
        assert service.persistence_degraded("session") is True
        assert [event.id for event in await service.get_events("session")] == ["durable", "live"]
        assert [event.id for event in await primary.get_events("session")] == ["durable"]
        await service.append_event("session", SessionEvent(id="later", event_type="user_message"))
        assert [event.id for event in await service.get_events("session")] == ["durable", "live", "later"]
        assert [event.id for event in await primary.get_events("session")] == ["durable"]
    finally:
        await primary.delete_session("session")
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["agent", "user"])
async def test_dirty_scoped_state_stays_live_after_recovery(pg_dsn, scope):
    namespace = "dirty_state_" + uuid.uuid4().hex[:16]
    primary = PostgresSessionService(dsn=pg_dsn, namespace=namespace)
    service = ResilientSessionService(primary)
    try:
        await service.create_session("agent", "user", "session")
        kwargs = dict(agent_id="agent", user_id="user", session_id="session", scope=scope)
        await service.update_state(**kwargs, state_delta={"before": 1})
        original = primary.update_state

        async def unavailable(**_kwargs):
            raise TimeoutError("injected outage")

        primary.update_state = unavailable
        await service.update_state(**kwargs, state_delta={"during": 2})
        primary.update_state = original
        service._primary_enabled = True
        await service.update_state(**kwargs, state_delta={"after": 3})
        live = await service.get_state(**kwargs)
        durable = await primary.get_state(**kwargs)
        assert live is not None and live.state == {"before": 1, "during": 2, "after": 3}
        assert durable is not None and durable.state == {"before": 1}
    finally:
        await primary.delete_session("session")
        await service.aclose()


@pytest.mark.asyncio
async def test_complete_agent_mirror_reads_during_outage_then_returns_to_durable(pg_dsn):
    namespace = "agent_failopen_" + uuid.uuid4().hex[:16]
    primary = PostgresSessionService(dsn=pg_dsn, namespace=namespace)
    peer = PostgresSessionService(dsn=pg_dsn, namespace=namespace)
    service = ResilientSessionService(primary)
    try:
        await service.create_session("agent", "user", "session")
        await service.update_state(agent_id="agent", user_id="user", session_id="session",
                                   scope="session", state_delta={"phase": "before"})
        assert (await service.get_session("session")).state == {"phase": "before"}
        original = primary.get_session

        async def unavailable(_session_id):
            # A capacity deadline keeps the durable source selected. Exercise
            # actual transport unavailability for the mirror fallback here.
            raise ConnectionResetError("injected outage")

        primary.get_session = unavailable
        assert (await service.get_session("session")).state == {"phase": "before"}
        assert (await service.get_session_metadata("session")).id == "session"
        assert (await service.get_state("agent", "user", "session", "session")).state == {
            "phase": "before"
        }
        assert service.persistence_degraded("session") is True
        assert "session" not in service._dirty_session_ids
        with pytest.raises(SessionBackendUnavailable):
            await service.get_events("session")
        primary.get_session = original
        service._primary_enabled = True
        await peer.update_state(agent_id="agent", user_id="user", session_id="session",
                                scope="session", state_delta={"phase": "after"})
        assert (await service.get_state("agent", "user", "session", "session")).state == {
            "phase": "after"
        }
        assert "session" not in service._dirty_session_ids
    finally:
        await primary.delete_session("session")
        await service.aclose()
        await peer.aclose()


@pytest.mark.asyncio
async def test_clean_checkpoint_failure_cannot_return_partial_live_cache(pg_dsn):
    from ksadk.sessions.base import CheckpointEventQuery

    namespace = "checkpoint_failclosed_" + uuid.uuid4().hex[:16]
    primary = PostgresSessionService(dsn=pg_dsn, namespace=namespace)
    peer = PostgresSessionService(dsn=pg_dsn, namespace=namespace)
    service = ResilientSessionService(primary)
    try:
        await service.create_session("agent", "user", "session")
        await service.append_event("session", SessionEvent(
            id="first", event_type="run_checkpoint",
            metadata={"run_id": "run", "checkpoint_id": "cp-first"},
        ))
        await peer.append_event("session", SessionEvent(
            id="second", event_type="run_checkpoint",
            metadata={"run_id": "run", "checkpoint_id": "cp-second"},
        ))
        original_scan = primary.scan_checkpoint_events

        async def unavailable_scan(_query):
            raise TimeoutError("injected read outage")

        primary.scan_checkpoint_events = unavailable_scan
        with pytest.raises(SessionBackendUnavailable):
            await service.scan_checkpoint_events(CheckpointEventQuery(
                session_ids=["session"], limit=50,
            ))
        primary.scan_checkpoint_events = original_scan
        service._primary_enabled = True
        original_lookup = primary.get_checkpoint_lookup_stats

        async def unavailable_lookup(*_args):
            raise ConnectionResetError("injected stats outage")

        primary.get_checkpoint_lookup_stats = unavailable_lookup
        with pytest.raises(SessionBackendUnavailable):
            await service.get_checkpoint_lookup_stats("session", "run", "cp-second")
        primary.get_checkpoint_lookup_stats = original_lookup
        service._primary_enabled = True
        assert [event.id for event in await service.scan_checkpoint_events(
            CheckpointEventQuery(session_ids=["session"], limit=50)
        )] == ["first", "second"]
    finally:
        await primary.delete_session("session")
        await service.aclose()
        await peer.aclose()


@pytest.mark.asyncio
async def test_duplicate_event_does_not_modify_live_or_degrade(pg_dsn):
    namespace = "duplicate_" + uuid.uuid4().hex[:16]
    primary = PostgresSessionService(dsn=pg_dsn, namespace=namespace)
    service = ResilientSessionService(primary)
    try:
        await service.create_session("agent", "user", "session")
        await service.append_event("session", SessionEvent(id="same", event_type="user_message"))
        with pytest.raises(asyncpg.UniqueViolationError):
            await service.append_event("session", SessionEvent(id="same", event_type="user_message"))
        assert service.degraded is False
        assert service.persistence_degraded("session") is False
        assert [event.id for event in await service.get_events("session")] == ["same"]
        assert [event.id for event in await service.fallback.get_events("session")] == ["same"]
    finally:
        await primary.delete_session("session")
        await service.aclose()


@pytest.mark.asyncio
async def test_real_postgres_lock_timeout_is_typed_and_no_live_write(pg_dsn):
    namespace = "lock_" + uuid.uuid4().hex[:16]
    budget = DatabaseBudget(lock_seconds=.05)
    primary = PostgresSessionService(dsn=pg_dsn, namespace=namespace, db_budget=budget)
    service = ResilientSessionService(primary)
    blocker = await asyncpg.connect(pg_dsn)
    try:
        await service.create_session("agent", "user", "session")
        async with blocker.transaction():
            await blocker.execute("SELECT id FROM ksadk_sessions WHERE namespace=$1 AND id=$2 FOR UPDATE",
                                  namespace, "session")
            with pytest.raises(asyncpg.LockNotAvailableError):
                await service.append_event("session", SessionEvent(id="blocked"))
        assert service.degraded is False
        assert service.persistence_degraded("session") is False
        assert await service.count_events("session") == 0
        assert await service.fallback.count_events("session") == 0
    finally:
        await blocker.close()
        await primary.delete_session("session")
        await service.aclose()


@pytest.mark.asyncio
async def test_unknown_commit_confirms_stable_event_id_without_reappend(pg_dsn):
    namespace = "unknown_commit_" + uuid.uuid4().hex[:16]
    primary = PostgresSessionService(dsn=pg_dsn, namespace=namespace)
    service = ResilientSessionService(primary)
    try:
        await service.create_session("agent", "user", "session")
        actual_append = primary.append_event
        attempts = 0

        async def committed_then_lost(session_id, event):
            nonlocal attempts
            attempts += 1
            await actual_append(session_id, event)
            raise TimeoutError("commit reply lost")

        primary.append_event = committed_then_lost
        persisted = await service.append_event("session", SessionEvent(id="stable-id"))
        assert persisted.id == "stable-id"
        assert persisted.seq_id == 1
        assert attempts == 1
        assert [event.id for event in await primary.get_events("session")] == ["stable-id"]
        assert service.persistence_degraded("session") is False
    finally:
        await primary.delete_session("session")
        await service.aclose()


@pytest.mark.asyncio
async def test_failed_delete_tombstone_hides_recovered_postgres_row(pg_dsn):
    namespace = "delete_" + uuid.uuid4().hex[:16]
    primary = PostgresSessionService(dsn=pg_dsn, namespace=namespace)
    service = ResilientSessionService(primary)
    try:
        await service.create_session("agent", "user", "session")
        actual_delete = primary.delete_session

        async def unavailable(_session_id):
            raise TimeoutError("delete unavailable")

        primary.delete_session = unavailable
        assert await service.delete_session("session") is True
        primary.delete_session = actual_delete
        service._primary_enabled = True
        assert await primary.get_session_metadata("session") is not None
        assert await service.get_session_metadata("session") is None
        assert not any(s.id == "session" for s in await service.list_session_metadata("agent"))
        await service.delete_session("session")
        assert await primary.get_session_metadata("session") is None
    finally:
        await primary.delete_session("session")
        await service.aclose()


@pytest.mark.asyncio
async def test_fixed_snapshot_forward_reverse_and_live_epoch(pg_dsn, monkeypatch):
    from ksadk.server.routes import sessions as routes

    namespace = "snapshot_" + uuid.uuid4().hex[:16]
    primary = PostgresSessionService(dsn=pg_dsn, namespace=namespace)
    service = ResilientSessionService(primary)
    monkeypatch.setattr(routes, "get_runtime_execution", lambda: (None, None))
    app = create_runtime_app(RuntimeAppConfig(
        route_groups={"sessions"}, session_service_provider=lambda: service,
    ), configure_runtime_app)
    url = "/agentengine/api/v1/ListSessionEvents"
    try:
        await service.create_session("agent", "user", "session")
        for index in range(1, 7):
            await service.append_event("session", SessionEvent(
                id=f"e-{index}", event_type="user_message", content={"text": str(index)},
            ))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            first = await client.post(url, json={"SessionId": "session", "AgentId": "agent",
                                                 "EventOrder": "session_seq", "AfterSeqId": 0, "Limit": 2})
            assert first.status_code == 200, first.text
            page = first.json()["Data"]
            assert [event["SeqId"] for event in page["Events"]] == [1, 2]
            assert page["SnapshotSeqId"] == 6
            assert page["Total"] == 6
            assert page["HistoryEpoch"]
            await service.append_event("session", SessionEvent(id="e-7", event_type="user_message"))
            second = await client.post(url, json={"SessionId": "session", "AgentId": "agent",
                "EventOrder": "session_seq", "AfterSeqId": 2, "Limit": 2,
                "SnapshotSeqId": page["SnapshotSeqId"], "HistoryEpoch": page["HistoryEpoch"]})
            assert second.status_code == 200, second.text
            assert [event["SeqId"] for event in second.json()["Data"]["Events"]] == [3, 4]
            assert second.json()["Data"]["Total"] == 4
            reverse = await client.post(url, json={"SessionId": "session", "AgentId": "agent",
                "BeforeSeqId": 7, "Limit": 2, "SnapshotSeqId": 6})
            assert reverse.status_code == 200, reverse.text
            assert [event["SeqId"] for event in reverse.json()["Data"]["Events"]] == [6, 5]
            stale = await client.post(url, json={"SessionId": "session", "AgentId": "agent",
                "EventOrder": "session_seq", "AfterSeqId": 2, "Limit": 2,
                "HistoryEpoch": "old"})
            assert stale.status_code == 409
            assert stale.json()["ReasonCode"] == "HISTORY_RELOAD_REQUIRED"
    finally:
        await primary.delete_session("session")
        await service.aclose()


@pytest.mark.asyncio
async def test_large_state_history_page_does_not_fetch_state_blob(pg_dsn, monkeypatch):
    namespace = "large_state_" + uuid.uuid4().hex[:16]
    primary = PostgresSessionService(dsn=pg_dsn, namespace=namespace)
    service = ResilientSessionService(primary)
    app = create_runtime_app(RuntimeAppConfig(
        route_groups={"sessions"}, session_service_provider=lambda: service,
    ), configure_runtime_app)
    try:
        await service.create_session("agent", "user", "session")
        await service.update_state(
            agent_id="agent", user_id="user", session_id="session", scope="session",
            state_delta={"large": "x" * (2 * 1024 * 1024)},
        )
        for index in range(100):
            await service.append_event("session", SessionEvent(
                id=f"e-{index}", author="user", event_type="user_message",
                content={"role": "user", "parts": [{"text": str(index)}]},
                invocation_id=f"inv-{index}",
            ))
        async def forbidden(*_args, **_kwargs):
            raise AssertionError("history must use state-free session header")

        monkeypatch.setattr(primary, "get_session_metadata", forbidden)
        start = time.monotonic()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/agentengine/api/v1/ListSessionMessages",
                                         json={"SessionId": "session", "AgentId": "agent", "Limit": 50})
        assert response.status_code == 200, response.text
        assert len(response.json()["Data"]["Messages"]) == 50
        assert time.monotonic() - start < 2.6
        assert len((await primary.get_state("agent", "user", "session", "session")).state["large"]) == 2 * 1024 * 1024
    finally:
        await primary.delete_session("session")
        await service.aclose()


@pytest.mark.asyncio
async def test_state_and_metadata_reads_do_not_replay_or_create_events(pg_dsn, monkeypatch):
    namespace = "state_only_" + uuid.uuid4().hex[:16]
    primary = PostgresSessionService(dsn=pg_dsn, namespace=namespace)
    try:
        await primary.create_session("agent", "user", "session")
        async def forbidden(*_args, **_kwargs):
            raise AssertionError("state/metadata/list must not read event bodies")

        monkeypatch.setattr(primary, "get_events", forbidden)
        assert (await primary.get_state("agent", "user", "session", "session")).state == {}
        await primary.update_state(agent_id="agent", user_id="user", session_id="session",
                                   scope="session", state_delta={"only": "state"})
        assert (await primary.get_session_metadata("session")).events == []
        assert (await primary.list_sessions("agent", "user", limit=1))[0].events == []
        assert await primary.count_events("session") == 0
    finally:
        await primary.delete_session("session")
        await primary.aclose()


@pytest.mark.asyncio
async def test_single_oversize_event_returns_typed_422():
    service = InMemorySessionService()
    await service.create_session("agent", "user", "session")
    await service.append_event("session", SessionEvent(
        id="huge", event_type="assistant_message",
        content={"role": "assistant", "parts": [{"text": "x" * (17 * 1024 * 1024)}]},
    ))
    app = create_runtime_app(RuntimeAppConfig(
        route_groups={"sessions"}, session_service_provider=lambda: service,
    ), configure_runtime_app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/agentengine/api/v1/ListSessionMessages",
                                     json={"SessionId": "session", "AgentId": "agent", "Limit": 50})
    assert response.status_code == 422, response.text
    assert response.json()["ReasonCode"] == "HISTORY_PROJECTION_BUDGET_EXCEEDED"


@pytest.mark.asyncio
async def test_real_postgres_oversize_event_returns_typed_422(pg_dsn, monkeypatch):
    from ksadk.server.routes import sessions as routes

    monkeypatch.setattr(routes, "get_runtime_execution", lambda: (None, None))
    namespace = "oversize_" + uuid.uuid4().hex[:16]
    primary = PostgresSessionService(dsn=pg_dsn, namespace=namespace)
    service = ResilientSessionService(primary)
    app = create_runtime_app(RuntimeAppConfig(
        route_groups={"sessions"}, session_service_provider=lambda: service,
    ), configure_runtime_app)
    try:
        await service.create_session("agent", "user", "session")
        await service.append_event("session", SessionEvent(
            id="huge", event_type="assistant_message",
            content={"role": "assistant", "parts": [{"text": "x" * (17 * 1024 * 1024)}]},
        ))
        def body_must_not_decode(_row):
            raise AssertionError("history JSON body was fetched before the byte gate")

        monkeypatch.setattr(primary, "_event_from_row", body_must_not_decode)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/agentengine/api/v1/ListSessionMessages",
                                         json={"SessionId": "session", "AgentId": "agent", "Limit": 50})
            forward = await client.post("/agentengine/api/v1/ListSessionEvents",
                                        json={"SessionId": "session", "AgentId": "agent",
                                              "AfterSeqId": 0, "EventOrder": "session_seq", "Limit": 50})
        assert response.status_code == 422, response.text[:250]
        assert response.json()["ReasonCode"] == "HISTORY_PROJECTION_BUDGET_EXCEEDED"
        assert forward.status_code == 422, forward.text[:250]
        assert forward.json()["ReasonCode"] == "HISTORY_PROJECTION_BUDGET_EXCEEDED"
        assert await primary.count_events("session") == 1
    finally:
        await primary.delete_session("session")
        await service.aclose()


@pytest.mark.asyncio
async def test_real_postgres_checkpoint_route_rejects_large_body_before_fetch(pg_dsn,
                                                                            monkeypatch):
    from ksadk.server.routes import sessions as routes

    monkeypatch.setattr(routes, "get_runtime_execution", lambda: (None, None))
    namespace = "checkpoint_large_" + uuid.uuid4().hex[:16]
    primary = PostgresSessionService(dsn=pg_dsn, namespace=namespace)
    service = ResilientSessionService(primary)
    app = create_runtime_app(RuntimeAppConfig(
        route_groups={"sessions"}, session_service_provider=lambda: service,
    ), configure_runtime_app)
    try:
        await service.create_session("agent", "user", "session")
        await service.append_event("session", SessionEvent(
            id="checkpoint-huge", event_type="run_checkpoint",
            content={"text": "x" * (17 * 1024 * 1024)},
            metadata={"checkpoint_id": "cp", "run_id": "run", "framework": "langgraph"},
        ))
        def must_not_decode(_row):
            raise AssertionError("checkpoint JSON body fetched before byte precheck")

        monkeypatch.setattr(primary, "_event_from_row", must_not_decode)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            response = await client.post(
                "/agentengine/api/v1/ListSessionCheckpoints",
                json={"AgentId": "agent", "SessionId": "session", "Limit": 1},
            )
        assert response.status_code == 422, response.text[:200]
        assert response.json()["ReasonCode"] == "HISTORY_PROJECTION_BUDGET_EXCEEDED"
    finally:
        await primary.delete_session("session")
        await service.aclose()


@pytest.mark.asyncio
async def test_checkpoint_snapshot_pages_accumulate_byte_budget(pg_dsn, monkeypatch):
    from ksadk.sessions.base import CheckpointEventQuery
    from ksadk.sessions.errors import HistoryProjectionBudgetExceeded
    from ksadk.sessions.history_budget import CURRENT_HISTORY_READ_BUDGET, HistoryReadBudget

    namespace = "checkpoint_budget_" + uuid.uuid4().hex[:16]
    primary = PostgresSessionService(dsn=pg_dsn, namespace=namespace)
    try:
        await primary.create_session("agent", "user", "session")
        for index in range(2):
            await primary.append_event("session", SessionEvent(
                id=f"checkpoint-{index}", event_type="run_checkpoint",
                # Exercise cumulative accounting independently of transfer
                # latency; the adjacent oversized-body test covers 16 MiB.
                content={"text": "x" * (9 * 1024)},
                metadata={"checkpoint_id": f"cp-{index}", "run_id": "run",
                          "framework": "langgraph"},
            ))
        decoded = []
        original = primary._event_from_row
        def counted_decode(row):
            decoded.append(row["id"])
            return original(row)
        monkeypatch.setattr(primary, "_event_from_row", counted_decode)
        token = CURRENT_HISTORY_READ_BUDGET.set(HistoryReadBudget(maximum=16 * 1024))
        try:
            with pytest.raises(HistoryProjectionBudgetExceeded):
                async for _batch in primary.iter_checkpoint_event_chunks(
                    CheckpointEventQuery(session_ids=["session"], limit=1,
                                         framework="langgraph")
                ):
                    pass
        finally:
            CURRENT_HISTORY_READ_BUDGET.reset(token)
        assert decoded == ["checkpoint-0"]
    finally:
        await primary.delete_session("session")
        await primary.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("query_api", ["get_events", "query_events", "get_message_event_window"])
async def test_history_byte_precheck_and_body_share_pg_snapshot(pg_dsn, monkeypatch, query_api):
    """A concurrent larger version must not replace the prechecked JSON row."""
    import json

    from ksadk.sessions._postgres_tables import KSADK_PG_EVENTS_TABLE
    from ksadk.sessions.base import SessionEventQuery
    from ksadk.sessions.errors import HistoryProjectionBudgetExceeded
    from ksadk.sessions.history_budget import (
        CURRENT_HISTORY_READ_BUDGET,
        HistoryReadBudget,
    )

    namespace = "mvcc_" + uuid.uuid4().hex[:16]
    primary = PostgresSessionService(dsn=pg_dsn, namespace=namespace)
    await primary.create_session("agent", "user", "session")
    await primary.append_event("session", SessionEvent(
        id="small", event_type="assistant_message", content={"text": "before"},
    ))
    original = primary._precheck_history_body
    updated = False

    async def precheck_then_replace(connection, candidate, params):
        nonlocal updated
        size = await original(connection, candidate, params)
        if not updated:
            updated = True
            peer = await asyncpg.connect(pg_dsn)
            try:
                await peer.execute(
                    f"UPDATE {KSADK_PG_EVENTS_TABLE} SET content_json=$1::jsonb "
                    "WHERE namespace=$2 AND session_id=$3 AND id=$4",
                    json.dumps({"text": "x" * (17 * 1024 * 1024)}),
                    namespace, "session", "small",
                )
            finally:
                await peer.close()
        return size

    monkeypatch.setattr(primary, "_precheck_history_body", precheck_then_replace)
    if query_api == "get_message_event_window":
        from dataclasses import replace
        from ksadk.sessions.postgres_service import _TimedConnection
        primary.db_budget = replace(primary.db_budget, read_sql_seconds=3)
        original_fetch = _TimedConnection.fetch

        async def fetch_during_concurrent_update(connection, query, *params, **kwargs):
            nonlocal updated
            if updated or 'required_bytes FROM checked' not in query:
                return await original_fetch(connection, query, *params, **kwargs)
            # Suspend the real PG statement after its MVCC snapshot exists;
            # mutate the row from a separate committed connection meanwhile.
            query = query.replace('required_bytes FROM checked',
                                  'required_bytes, pg_sleep(1) FROM checked')
            reading = asyncio.create_task(original_fetch(connection, query, *params, **kwargs))
            peer = await asyncpg.connect(pg_dsn)
            try:
                for _ in range(200):
                    sleeping = await peer.fetchval(
                        "SELECT wait_event='PgSleep' FROM pg_stat_activity WHERE pid=$1",
                        connection._connection.get_server_pid(),
                    )
                    if sleeping:
                        break
                    assert not reading.done()
                    await asyncio.sleep(.005)
                else:
                    raise AssertionError('PG statement did not enter the snapshot barrier')
                await peer.execute(
                    f"UPDATE {KSADK_PG_EVENTS_TABLE} SET content_json=$1::jsonb "
                    "WHERE namespace=$2 AND session_id=$3 AND id=$4",
                    json.dumps({"text": "x" * (17 * 1024 * 1024)}),
                    namespace, "session", "small",
                )
                updated = True
                return await reading
            finally:
                if not reading.done():
                    reading.cancel()
                await asyncio.gather(reading, return_exceptions=True)
                await peer.close()

        monkeypatch.setattr(_TimedConnection, 'fetch', fetch_during_concurrent_update)
    token = CURRENT_HISTORY_READ_BUDGET.set(HistoryReadBudget())
    try:
        if query_api == "get_events":
            first = await primary.get_events("session", limit=1)
        elif query_api == "query_events":
            first = await primary.query_events(SessionEventQuery(
                session_ids=["session"], limit=1, order_by_seq=True,
            ))
        else:
            first = await primary.get_message_event_window("session", limit=1)
        assert first[0].content == {"text": "before"}
        assert updated
        CURRENT_HISTORY_READ_BUDGET.set(HistoryReadBudget())
        with pytest.raises(HistoryProjectionBudgetExceeded):
            if query_api == "get_events":
                await primary.get_events("session", limit=1)
            elif query_api == "query_events":
                await primary.query_events(SessionEventQuery(
                    session_ids=["session"], limit=1, order_by_seq=True,
                ))
            else:
                await primary.get_message_event_window("session", limit=1)
    finally:
        CURRENT_HISTORY_READ_BUDGET.reset(token)
        await primary.delete_session("session")
        await primary.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_summary_finds_older_active_run_beyond_256_lifecycle_facts(pg_dsn, backend):
    from ksadk.server.routes.models import _latest_session_run_metadata

    namespace = "summary_" + uuid.uuid4().hex[:16]
    service = (InMemorySessionService() if backend == "memory" else
               PostgresSessionService(dsn=pg_dsn, namespace=namespace))
    try:
        await service.create_session("agent", "user", "session")
        await service.append_event("session", SessionEvent(
            id="active-classified", event_type="run_status", invocation_id="active",
            metadata={"run_id": "active", "status": "running",
                      "run_mode": "background", "run_trigger": "resume"},
        ))
        await service.append_event("session", SessionEvent(
            id="active-canonical", event_type="run.progress", invocation_id="active",
            content={"payload": {"status": "in_progress"}},
            metadata={"run_id": "active"},
        ))
        for index in range(270):
            await service.append_event("session", SessionEvent(
                id=f"terminal-{index}", event_type="run_status",
                invocation_id=f"terminal-{index}",
                metadata={"run_id": f"terminal-{index}", "status": "completed"},
            ))
        summary = await service.get_session_summary_events("session")
        assert len(summary) == 1
        assert _latest_session_run_metadata(summary) == (
            "active", "in_progress", "background", "resume",
        )
    finally:
        await service.delete_session("session")
        close = getattr(service, "aclose", None)
        if close is not None:
            await close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["append", "metadata", "state", "delete"])
async def test_local_session_write_lock_wait_is_bounded(operation):
    primary = InMemorySessionService()
    primary.db_budget = DatabaseBudget(lock_seconds=.01, write_operation_seconds=.03)
    service = ResilientSessionService(primary)
    await service.create_session("agent", "user", "session")
    lock = service._hydrate_locks.setdefault("session", asyncio.Lock())
    await lock.acquire()
    started = time.perf_counter()
    try:
        with pytest.raises(TimeoutError):
            if operation == "append":
                await service.append_event("session", SessionEvent(id="blocked"))
            elif operation == "metadata":
                await service.update_session_metadata("session", title="blocked")
            elif operation == "state":
                await service.update_state(agent_id="agent", user_id="user",
                                           session_id="session", scope="session",
                                           state_delta={"blocked": True})
            else:
                await service.delete_session("session")
        assert time.perf_counter() - started < .1
    finally:
        lock.release()
        await service.aclose()


@pytest.mark.asyncio
async def test_dirty_list_page_fetches_only_bounded_durable_prefix(monkeypatch):
    primary = InMemorySessionService()
    service = ResilientSessionService(primary)
    for index in range(12):
        await service.create_session("agent", "user", f"session-{index}")
    service._mark_dirty("session-11")
    service._deleted_session_ids.add("session-10")
    calls = []
    original = primary.list_sessions

    async def measured(agent_id, user_id=None, offset=None, limit=None):
        calls.append((offset, limit))
        return await original(agent_id, user_id, offset, limit)

    monkeypatch.setattr(primary, "list_sessions", measured)
    page = await service.list_sessions("agent", "user", offset=3, limit=4)
    assert calls == [(0, 9)]
    assert len(page) == 4
    assert all(session.id != "session-10" and not session.events for session in page)
    assert await service.count_sessions("agent", "user") == 11
    await service.aclose()


@pytest.mark.asyncio
async def test_real_postgres_read_sql_and_pool_acquire_deadlines(pg_dsn):
    namespace = "phases_" + uuid.uuid4().hex[:16]
    budget = DatabaseBudget(read_acquire_seconds=.05, read_sql_seconds=.05,
                            cleanup_seconds=.2)
    primary = PostgresSessionService(dsn=pg_dsn, namespace=namespace,
                                     min_size=1, max_size=1, db_budget=budget)
    try:
        await primary.create_session("agent", "user", "session")
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            async with primary._connection() as connection:
                await connection.fetchval("SELECT pg_sleep(0.2)")
        assert time.monotonic() - started < .2
        async with primary._pool.acquire():
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                await primary.count_events("session")
            assert time.monotonic() - started < .2
        assert await primary.count_events("session") == 0
        async with primary._pool.acquire() as connection:
            async with connection.transaction():
                await connection.fetchval(
                    "SELECT set_config('statement_timeout', $1, true)", "50ms"
                )
                with pytest.raises(asyncpg.QueryCanceledError) as captured:
                    await connection.fetchval("SELECT pg_sleep(0.2)")
                assert is_session_backend_failure(captured.value) is False
    finally:
        await primary.delete_session("session")
        await primary.aclose()


def test_postgres_sqlstates_are_not_backend_unavailability():
    for error in (
        asyncpg.QueryCanceledError("statement timeout"),
        asyncpg.LockNotAvailableError("lock timeout"),
        asyncpg.UniqueViolationError("duplicate"),
        asyncpg.CheckViolationError("check"),
    ):
        assert is_session_backend_failure(error) is False


@pytest.mark.asyncio
async def test_sqlstate_http_classification_and_total_asgi_deadline():
    from ksadk.server.history_deadline import HistoryDeadlineMiddleware

    app = create_runtime_app(RuntimeAppConfig(
        route_groups=set(), session_service_provider=InMemorySessionService,
    ))

    @app.post("/agentengine/api/v1/ListSessions")
    async def statement_timeout():
        raise asyncpg.QueryCanceledError("statement timeout")

    @app.post("/agentengine/api/v1/GetSession")
    async def lock_timeout():
        raise asyncpg.LockNotAvailableError("lock timeout")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/agentengine/api/v1/ListSessions")
        assert response.status_code == 504
        assert response.json()["ReasonCode"] == "DATABASE_STATEMENT_TIMEOUT"
        response = await client.post("/agentengine/api/v1/GetSession")
        assert response.status_code == 503
        assert response.json()["ReasonCode"] == "DATABASE_LOCK_TIMEOUT"

    # A handler that has sent its 200 headers but stalls while writing its
    # body must still yield one complete 504, with no partial success body.
    async def slow_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await asyncio.sleep(.03)
        await send({"type": "http.response.body", "body": b"late"})

    middleware = HistoryDeadlineMiddleware(slow_app)
    scope = {"type": "http", "method": "POST", "path": "/agentengine/api/v1/ListSessionMessages",
             "headers": [(b"x-agentengine-remaining-ms", b"1")]}
    messages = []
    async def send(message):
        messages.append(message)

    from ksadk.server import history_deadline
    old = history_deadline.trusted_invocation_identity_from_headers
    try:
        history_deadline.trusted_invocation_identity_from_headers = lambda _h: type(
            "Trusted", (), {"is_empty": False}
        )()
        await middleware(scope, lambda: None, send)
    finally:
        history_deadline.trusted_invocation_identity_from_headers = old
    assert [message["type"] for message in messages] == [
        "http.response.start", "http.response.body"
    ]
    assert messages[0]["status"] == 504
    assert b"late" not in messages[1]["body"]


def test_remaining_header_requires_canonical_decimal():
    from ksadk.server.history_deadline import _remaining_ms

    assert _remaining_ms("0") == 0
    assert _remaining_ms("2600") == 2600
    assert _remaining_ms("00") == 0
    assert _remaining_ms("01") == 1
    assert _remaining_ms("-1") == 0
    assert _remaining_ms("1" * 100) == math.inf
    for invalid in ("", "1.5", "+1", " 1", "1 ",
                    "1e3", "NaN", "inf", "١"):
        assert _remaining_ms(invalid) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [504, 422])
@pytest.mark.parametrize("blocked_stage", ["header", "body"])
async def test_history_error_transport_has_one_bounded_send(status, blocked_stage,
                                                              monkeypatch):
    from ksadk.server import history_deadline

    monkeypatch.setattr(history_deadline, "_ERROR_SEND_SECONDS", .02)
    monkeypatch.setattr(history_deadline, "_MAX_BUFFER_BYTES", 1)
    monkeypatch.setattr(
        history_deadline, "trusted_invocation_identity_from_headers",
        lambda _headers: type("Trusted", (), {"is_empty": False})(),
    )

    async def app(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"xx"})

    scope = {"type": "http", "method": "POST",
             "path": "/agentengine/api/v1/ListSessionMessages",
             "headers": ([(b"x-agentengine-remaining-ms", b"0")]
                         if status == 504 else [])}
    delivered = []

    async def blocked_send(message):
        if message["type"] == ("http.response.start" if blocked_stage == "header"
                               else "http.response.body"):
            await asyncio.Event().wait()
        delivered.append(message)

    started = time.perf_counter()
    with pytest.raises(TimeoutError):
        await history_deadline.HistoryDeadlineMiddleware(app)(
            scope, lambda: None, blocked_send,
        )
    assert time.perf_counter() - started < .2
    assert [message["type"] for message in delivered] == (
        [] if blocked_stage == "header" else ["http.response.start"]
    )
    if delivered:
        assert delivered[0]["status"] == status


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["session", "shared"])
@pytest.mark.parametrize("failure", ["deadline", "cancel"])
async def test_cancelled_transaction_preserves_failure_and_pool_recovers(pg_dsn, wrapper, failure):
    from ksadk.sessions.db_budget import TimedDatabaseConnection, bounded_driver_call
    from ksadk.sessions.postgres_service import _TimedConnection

    pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=1)
    table = "cleanup_" + uuid.uuid4().hex
    entered = asyncio.Event()
    task = None
    try:
        async with pool.acquire() as raw:
            await raw.execute(f"CREATE TABLE {table} (id integer)")
            original_pid = await raw.fetchval("SELECT pg_backend_pid()")

        async def operation():
            async with pool.acquire() as raw:
                connection = (
                    _TimedConnection(raw, 1)
                    if wrapper == "session"
                    else TimedDatabaseConnection(raw, DatabaseBudget(), write=False)
                )
                async with connection.transaction():
                    await connection.execute(f"INSERT INTO {table} VALUES (1)")
                    entered.set()
                    # Cancel a real transaction; cleanup must roll it back.
                    await bounded_driver_call(
                        raw.execute("SELECT pg_sleep(1)"), raw,
                        .05 if failure == "deadline" else 5,
                    )

        task = asyncio.create_task(operation())
        await asyncio.wait_for(entered.wait(), 3)
        if failure == "cancel":
            task.cancel()
        expected = TimeoutError if failure == "deadline" else asyncio.CancelledError
        with pytest.raises(expected):
            await asyncio.wait_for(task, 2)
        async with pool.acquire(timeout=1) as raw:
            assert not raw.is_in_transaction()
            assert not await raw.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=$1 AND state='active' AND pid<>pg_backend_pid())",
                original_pid,
            )
            assert await raw.fetchval(f"SELECT COUNT(*) FROM {table}") == 0
            assert await raw.fetchval("SELECT 1") == 1
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with pool.acquire() as raw:
            await raw.execute(f"DROP TABLE IF EXISTS {table}")
        await pool.close()


@pytest.mark.asyncio
async def test_shared_db_budget_bounds_acquire_sql_operation_and_cleanup():
    class SlowConnection:
        async def fetchval(self, *_args):
            await asyncio.sleep(.1)

        def transaction(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Lease:
        def __init__(self, *, enter_delay=0, exit_delay=0):
            self.enter_delay = enter_delay
            self.exit_delay = exit_delay
            self.closed = False

        async def __aenter__(self):
            await asyncio.sleep(self.enter_delay)
            return SlowConnection()

        async def __aexit__(self, *_args):
            await asyncio.sleep(self.exit_delay)
            self.closed = True

    class Pool:
        def __init__(self, lease):
            self.lease = lease

        def acquire(self, **_kwargs):
            return self.lease

    budget = DatabaseBudget(read_acquire_seconds=.01, read_sql_seconds=.01,
                            write_acquire_seconds=.01, write_sql_seconds=.01,
                            write_operation_seconds=.02, cleanup_seconds=.01)
    with pytest.raises(TimeoutError):
        async with budgeted_pool_connection(Pool(Lease(enter_delay=.1)), budget):
            pass
    lease = Lease()
    with pytest.raises(TimeoutError):
        async with budgeted_pool_connection(Pool(lease), budget) as connection:
            await connection.fetchval("SELECT 1")
    assert lease.closed
    lease = Lease()
    with pytest.raises(TimeoutError):
        async with budgeted_pool_connection(Pool(lease), budget, write=True):
            await asyncio.sleep(.1)
    assert lease.closed
    with pytest.raises(TimeoutError):
        async with budgeted_pool_connection(Pool(Lease(exit_delay=.1)), budget):
            pass
