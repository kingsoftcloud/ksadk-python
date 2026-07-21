from __future__ import annotations

import asyncio
import logging
import os
import sys
from types import SimpleNamespace

import pytest

from ksadk.sessions import create_session_service
from ksadk.sessions.base import SessionEvent
from ksadk.sessions.errors import SessionBackendUnavailable
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.sessions.resilient import ResilientSessionService

pytestmark = pytest.mark.asyncio


async def test_postgres_session_service_uses_configured_connect_timeout(monkeypatch):
    from ksadk.sessions.postgres_service import PostgresSessionService

    observed: dict[str, object] = {}

    async def fake_create_pool(**kwargs):
        observed.update(kwargs)
        raise TimeoutError("connect timed out")

    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(create_pool=fake_create_pool))

    service = PostgresSessionService(
        dsn="postgresql://ksadk:secret@db.example.test:5432/session",
        connect_timeout=0.25,
    )

    with pytest.raises(SessionBackendUnavailable) as exc_info:
        await service.create_session("demo-agent", "user-1")

    assert observed["timeout"] == 0.25
    assert observed["command_timeout"] == 0.25
    assert "Postgres session backend unavailable" in str(exc_info.value)
    assert "secret" not in str(exc_info.value)


async def test_postgres_session_service_forces_pool_termination_when_close_times_out():
    class HangingPool:
        def __init__(self) -> None:
            self.terminated = False

        async def close(self):
            await asyncio.sleep(60)

        def terminate(self):
            self.terminated = True

    from ksadk.sessions.postgres_service import PostgresSessionService

    service = PostgresSessionService(
        dsn="postgresql://user@db.example.test/session",
        connect_timeout=0.01,
    )
    pool = HangingPool()
    service._pool = pool
    service._schema_ready = True

    await service.aclose()

    assert pool.terminated is True
    assert service._pool is None
    assert service._schema_ready is False


async def test_configured_postgres_backend_fails_open_to_memory(monkeypatch, caplog):
    observed = {"attempts": 0}

    async def fake_create_pool(**_kwargs):
        observed["attempts"] += 1
        raise TimeoutError("connect timed out")

    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(create_pool=fake_create_pool))
    monkeypatch.setenv("KSADK_SESSION_BACKEND", "postgres")
    monkeypatch.setenv(
        "KSADK_SESSION_DSN",
        "postgresql://ksadk:secret@db.example.test:5432/session",
    )

    service = create_session_service()
    session = await service.create_session("demo-agent", "user-1", session_id="sess-1")
    stored = await service.append_event(
        session.id,
        SessionEvent(
            id="evt-1",
            author="user",
            event_type="user_message",
            content={"role": "user", "parts": [{"text": "hello"}]},
        ),
    )
    events = await service.get_events(session.id)

    assert session.id == "sess-1"
    assert stored.seq_id == 1
    assert [event.id for event in events] == ["evt-1"]
    assert observed["attempts"] == 1
    assert "session persistence degraded" in caplog.text


async def test_configured_postgres_backend_fails_open_when_asyncpg_is_missing(
    monkeypatch,
    caplog,
):
    monkeypatch.setitem(sys.modules, "asyncpg", None)
    monkeypatch.setenv("KSADK_SESSION_BACKEND", "postgres")
    monkeypatch.setenv(
        "KSADK_SESSION_DSN",
        "postgresql://ksadk:secret@db.example.test:5432/session",
    )

    service = create_session_service()
    session = await service.create_session("demo-agent", "user-1", session_id="sess-1")

    assert session.id == "sess-1"
    assert isinstance(service, ResilientSessionService)
    assert service.degraded is True
    assert "asyncpg is required" in caplog.text
    assert "session persistence degraded" in caplog.text


async def test_postgres_schema_creates_readable_session_event_view(monkeypatch):
    executed: list[str] = []

    class FakeConnection:
        async def execute(self, sql, *_args):
            executed.append(sql)

    class AcquireContext:
        async def __aenter__(self):
            return FakeConnection()

        async def __aexit__(self, *_args):
            return None

    class FakePool:
        def acquire(self):
            return AcquireContext()

    async def fake_create_pool(**_kwargs):
        return FakePool()

    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(create_pool=fake_create_pool))

    from ksadk.sessions.postgres_service import PostgresSessionService

    service = PostgresSessionService(dsn="postgresql://user@db.example.test/session")
    await service._ensure_schema()

    schema_sql = "\n".join(executed)
    assert "CREATE OR REPLACE VIEW ksadk_session_events_readable" in schema_sql
    assert "message_text" in schema_sql
    assert "lifecycle_status" in schema_sql


async def test_postgres_events_for_agent_pushes_user_pagination_and_order_to_sql():
    observed: dict[str, object] = {}

    class FakeConnection:
        async def fetch(self, sql, *params):
            observed["fetch_sql"] = sql
            observed["fetch_params"] = params
            return [
                {
                    "id": "evt-1",
                    "session_id": "sess-1",
                    "author": "user",
                    "event_type": "user_message",
                    "content_json": {"text": "hello"},
                    "timestamp": 100.0,
                    "state_delta_json": {},
                    "seq_id": 1,
                    "invocation_id": "run_11111111111111111111111111111111",
                    "metadata_json": {},
                }
            ]

        async def fetchval(self, sql, *params):
            observed["count_sql"] = sql
            observed["count_params"] = params
            return 51

    class AcquireContext:
        async def __aenter__(self):
            return FakeConnection()

        async def __aexit__(self, *_args):
            return None

    class FakePool:
        def acquire(self):
            return AcquireContext()

    from ksadk.sessions.postgres_service import PostgresSessionService

    service = PostgresSessionService(
        dsn="postgresql://user@db.example.test/session",
        namespace="tenant-a",
    )
    service._pool = FakePool()
    service._schema_ready = True

    events = await service.get_events_for_agent(
        "demo-agent",
        user_id="user-a",
        offset=400,
        limit=200,
    )
    total = await service.count_events_for_agent("demo-agent", user_id="user-a")

    assert [event.id for event in events] == ["evt-1"]
    assert total == 51
    assert observed["fetch_params"] == ("tenant-a", "demo-agent", "user-a", 200, 400)
    assert observed["count_params"] == ("tenant-a", "demo-agent", "user-a")
    assert "JOIN ksadk_sessions s" in str(observed["fetch_sql"])
    assert "s.user_id = $3" in str(observed["fetch_sql"])
    assert "ORDER BY e.timestamp DESC, e.seq_id DESC, e.id DESC" in str(
        observed["fetch_sql"]
    )
    assert "ORDER BY timestamp ASC, seq_id ASC, id ASC" in str(observed["fetch_sql"])
    assert "JOIN ksadk_sessions s" in str(observed["count_sql"])


async def test_resilient_session_keeps_hydrated_history_when_primary_fails(caplog):
    primary = InMemorySessionService()
    await primary.create_session("demo-agent", "user-1", session_id="sess-1")
    await primary.append_event(
        "sess-1",
        SessionEvent(
            id="evt-old",
            author="user",
            event_type="user_message",
            content={"role": "user", "parts": [{"text": "old"}]},
        ),
    )
    service = ResilientSessionService(primary)

    hydrated = await service.get_session("sess-1")
    assert hydrated is not None
    assert [event.id for event in await service.get_events("sess-1")] == ["evt-old"]

    async def fail_append(*_args, **_kwargs):
        raise ConnectionError("postgres connection lost")

    primary.append_event = fail_append
    await service.append_event(
        "sess-1",
        SessionEvent(
            id="evt-new",
            author="demo-agent",
            event_type="assistant_message",
            content={"role": "assistant", "parts": [{"text": "new"}]},
        ),
    )

    assert [event.id for event in await service.get_events("sess-1")] == [
        "evt-old",
        "evt-new",
    ]
    assert service.degraded is True
    assert caplog.text.count("session persistence degraded") == 1


async def test_resilient_session_refreshes_healthy_primary_history():
    primary = InMemorySessionService()
    await primary.create_session("demo-agent", "user-1", session_id="sess-1")
    service = ResilientSessionService(primary)
    assert await service.get_events("sess-1") == []

    await primary.append_event(
        "sess-1",
        SessionEvent(
            id="evt-other-pod",
            author="user",
            event_type="user_message",
            content={"role": "user", "parts": [{"text": "from another pod"}]},
        ),
    )

    assert [event.id for event in await service.get_events("sess-1")] == ["evt-other-pod"]


async def test_postgres_readable_view_failure_does_not_disable_core_schema(monkeypatch, caplog):
    class FakeConnection:
        async def execute(self, sql, *_args):
            if "CREATE OR REPLACE VIEW" in sql:
                raise PermissionError("view creation denied")

    class AcquireContext:
        async def __aenter__(self):
            return FakeConnection()

        async def __aexit__(self, *_args):
            return None

    class FakePool:
        def acquire(self):
            return AcquireContext()

    async def fake_create_pool(**_kwargs):
        return FakePool()

    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(create_pool=fake_create_pool))

    from ksadk.sessions.postgres_service import PostgresSessionService

    service = PostgresSessionService(dsn="postgresql://user@db.example.test/session")
    await service._ensure_schema()

    assert service._schema_ready is True
    assert "readable session view unavailable" in caplog.text


async def test_postgres_session_service_two_instances_share_sessions_events_and_state():
    dsn = os.getenv("KSADK_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set KSADK_TEST_POSTGRES_DSN to run Postgres session integration tests")

    from ksadk.sessions.postgres_service import PostgresSessionService

    namespace = "pytest_cross_pod"
    service_a = PostgresSessionService(dsn=dsn, namespace=namespace)
    service_b = PostgresSessionService(dsn=dsn, namespace=namespace)
    session_id = "pytest-sess-cross-pod"

    try:
        await service_a.delete_session(session_id)
        created = await service_a.create_session(
            agent_id="demo-agent",
            user_id="user-1",
            session_id=session_id,
        )
        await service_a.append_event(
            session_id,
            SessionEvent(
                id="pytest-evt-1",
                author="user",
                event_type="user_message",
                content={"role": "user", "parts": [{"text": "hello"}]},
                state_delta={"turns": 1},
                metadata={"tenant_id": "tenant-a"},
            ),
        )
        await service_a.update_state(
            agent_id="demo-agent",
            user_id="user-1",
            session_id=session_id,
            scope="runner_runtime:langgraph",
            state_delta={"path": "replay", "level": "semantic"},
        )

        listed = await service_b.list_sessions("demo-agent", "user-1")
        fetched = await service_b.get_session(session_id)
        events = await service_b.get_events(session_id)
        session_state = await service_b.get_state("demo-agent", "user-1", session_id, "session")
        runtime_state = await service_b.get_state(
            "demo-agent",
            "user-1",
            session_id,
            "runner_runtime:langgraph",
        )

        assert created.id == session_id
        assert session_id in [session.id for session in listed]
        assert fetched is not None
        assert fetched.state == {"turns": 1}
        assert [event.id for event in events] == ["pytest-evt-1"]
        assert events[0].seq_id == 1
        assert session_state is not None
        assert session_state.state == {"turns": 1}
        assert runtime_state is not None
        assert runtime_state.state == {"path": "replay", "level": "semantic"}
    finally:
        await service_a.delete_session(session_id)
        await service_a.aclose()
        await service_b.aclose()


async def test_postgres_session_create_is_idempotent_across_instances():
    dsn = os.getenv("KSADK_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set KSADK_TEST_POSTGRES_DSN to run Postgres session integration tests")

    from ksadk.sessions.postgres_service import PostgresSessionService

    namespace = "pytest_concurrent_create"
    service_a = PostgresSessionService(dsn=dsn, namespace=namespace)
    service_b = PostgresSessionService(dsn=dsn, namespace=namespace)
    session_id = "pytest-sess-concurrent-create"

    try:
        await service_a.delete_session(session_id)
        created_a, created_b = await asyncio.gather(
            service_a.create_session("demo-agent", "user-1", session_id=session_id),
            service_b.create_session("demo-agent", "user-1", session_id=session_id),
        )

        assert created_a.id == session_id
        assert created_b.id == session_id
        assert created_a.agent_id == created_b.agent_id == "demo-agent"
        assert created_a.user_id == created_b.user_id == "user-1"
    finally:
        await service_a.delete_session(session_id)
        await service_a.aclose()
        await service_b.aclose()


async def test_postgres_session_service_get_events_filters_by_after_seq_id():
    dsn = os.getenv("KSADK_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set KSADK_TEST_POSTGRES_DSN to run Postgres session integration tests")

    from ksadk.sessions.postgres_service import PostgresSessionService

    namespace = "pytest_after_seq"
    service = PostgresSessionService(dsn=dsn, namespace=namespace)
    session_id = "pytest-sess-after-seq"

    try:
        await service.delete_session(session_id)
        await service.create_session(
            agent_id="demo-agent",
            user_id="user-1",
            session_id=session_id,
        )
        for index in range(4):
            await service.append_event(
                session_id,
                SessionEvent(
                    id=f"pytest-evt-after-{index + 1}",
                    author="user",
                    event_type="text",
                    content={"index": index},
                ),
            )

        all_events = await service.get_events(session_id)
        assert [event.seq_id for event in all_events] == [1, 2, 3, 4]

        after2 = await service.get_events(session_id, after_seq_id=2)
        assert [event.seq_id for event in after2] == [3, 4]

        after0 = await service.get_events(session_id, after_seq_id=0)
        assert [event.seq_id for event in after0] == [1, 2, 3, 4]

        after_max = await service.get_events(session_id, after_seq_id=4)
        assert [event.seq_id for event in after_max] == []

        after_limit = await service.get_events(session_id, after_seq_id=2, limit=1)
        assert [event.seq_id for event in after_limit] == [4]
    finally:
        await service.delete_session(session_id)
        await service.aclose()


async def test_postgres_session_service_get_events_filters_by_before_seq_id():
    dsn = os.getenv("KSADK_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set KSADK_TEST_POSTGRES_DSN to run Postgres session integration tests")

    from ksadk.sessions.postgres_service import PostgresSessionService

    namespace = "pytest_before_seq"
    service = PostgresSessionService(dsn=dsn, namespace=namespace)
    session_id = "pytest-sess-before-seq"

    try:
        await service.delete_session(session_id)
        await service.create_session(
            agent_id="demo-agent",
            user_id="user-1",
            session_id=session_id,
        )
        for index in range(5):
            await service.append_event(
                session_id,
                SessionEvent(
                    id=f"pytest-evt-before-{index + 1}",
                    author="user",
                    event_type="text",
                    content={"index": index},
                ),
            )

        before4 = await service.get_events(session_id, before_seq_id=4)
        assert [event.seq_id for event in before4] == [1, 2, 3]
        assert await service.count_events(session_id, before_seq_id=4) == 3

        before4_limit = await service.get_events(session_id, before_seq_id=4, limit=2)
        assert [event.seq_id for event in before4_limit] == [2, 3]

        before1 = await service.get_events(session_id, before_seq_id=1)
        assert before1 == []
    finally:
        await service.delete_session(session_id)
        await service.aclose()


async def test_postgres_session_service_namespaces_isolate_same_session_id():
    dsn = os.getenv("KSADK_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set KSADK_TEST_POSTGRES_DSN to run Postgres session integration tests")

    from ksadk.sessions.postgres_service import PostgresSessionService

    session_id = "pytest-sess-same-id"
    service_a = PostgresSessionService(dsn=dsn, namespace="pytest_tenant_a")
    service_b = PostgresSessionService(dsn=dsn, namespace="pytest_tenant_b")

    try:
        await service_a.delete_session(session_id)
        await service_b.delete_session(session_id)
        await service_a.create_session("agent-a", "user-1", session_id=session_id)
        await service_b.create_session("agent-b", "user-1", session_id=session_id)

        sessions_a = await service_a.list_sessions("agent-a", "user-1")
        sessions_b = await service_b.list_sessions("agent-b", "user-1")
        assert [session.agent_id for session in sessions_a] == ["agent-a"]
        assert [session.agent_id for session in sessions_b] == ["agent-b"]
        assert await service_a.list_sessions("agent-b", "user-1") == []
        assert await service_b.list_sessions("agent-a", "user-1") == []
    finally:
        await service_a.delete_session(session_id)
        await service_b.delete_session(session_id)
        await service_a.aclose()
        await service_b.aclose()


class _RecoverablePrimary(InMemorySessionService):
    """Mock primary that fails N times then recovers."""

    def __init__(self, fail_count: int = 1) -> None:
        super().__init__()
        self._fail_remaining = fail_count

    async def get_session(self, session_id, *args, **kwargs):
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise ConnectionError("pg temporarily down")
        return await super().get_session(session_id, *args, **kwargs)


class _CountingPrimary(InMemorySessionService):
    def __init__(self) -> None:
        super().__init__()
        self.get_session_calls = 0

    async def get_session(self, session_id, *args, **kwargs):
        self.get_session_calls += 1
        return await super().get_session(session_id, *args, **kwargs)


async def test_healthy_event_write_does_not_query_primary_session_each_time():
    primary = _CountingPrimary()
    service = ResilientSessionService(primary)
    session = await service.create_session("agent-1", "user-1", session_id="sess-1")
    primary.get_session_calls = 0

    await service.append_event(
        session.id,
        SessionEvent(
            id="evt-1",
            author="user",
            event_type="user_message",
            content={"role": "user", "parts": [{"text": "hello"}]},
        ),
    )

    assert primary.get_session_calls == 0
    await service.aclose()


class _InvalidPrimary(InMemorySessionService):
    async def create_session(self, *args, **kwargs):
        raise ValueError("invalid session payload")


async def test_non_backend_error_is_not_hidden_by_fail_open():
    service = ResilientSessionService(_InvalidPrimary())

    with pytest.raises(ValueError, match="invalid session payload"):
        await service.create_session("agent-1", "user-1", session_id="sess-invalid")

    assert service.degraded is False
    await service.aclose()


async def test_resilient_service_recovers_after_probe(monkeypatch, caplog):
    primary = _RecoverablePrimary(fail_count=1)
    service = ResilientSessionService(primary)
    service._probe_interval_seconds = 0.05
    caplog.set_level(logging.INFO)

    await service.create_session("agent-1", "user-1", session_id="sess-1")
    assert service.degraded is True

    await asyncio.sleep(0.15)
    assert service.degraded is False
    assert "session persistence recovered" in caplog.text
    await service.aclose()


class _ProbeRecoveringPrimary(InMemorySessionService):
    """Primary that fails until probed via get_session with sentinel id, then works."""

    def __init__(self) -> None:
        super().__init__()
        self._recovered = False

    async def get_session(self, session_id, *args, **kwargs):
        if not self._recovered:
            if session_id == "__ksadk_probe__":
                self._recovered = True
                return None
            raise ConnectionError("pg down")
        return await super().get_session(session_id, *args, **kwargs)

    async def create_session(self, *args, **kwargs):
        if not self._recovered:
            raise ConnectionError("pg down")
        return await super().create_session(*args, **kwargs)

    async def append_event(self, session_id, *args, **kwargs):
        if not self._recovered:
            raise ConnectionError("pg down")
        return await super().append_event(session_id, *args, **kwargs)

    async def update_session_metadata(self, *args, **kwargs):
        if not self._recovered:
            raise ConnectionError("pg down")
        return await super().update_session_metadata(*args, **kwargs)


async def test_degraded_session_resumes_pg_writes_after_recovery(caplog):
    """A degraded-era session should send subsequent events to PG after recovery."""
    primary = _ProbeRecoveringPrimary()
    service = ResilientSessionService(primary)
    service._probe_interval_seconds = 0.05
    caplog.set_level(logging.INFO)

    # Create session while PG is down — lives only in memory
    await service.create_session("agent-1", "user-1", session_id="sess-degraded")
    assert service.degraded is True

    # Append an event while degraded
    await service.append_event(
        "sess-degraded",
        SessionEvent(
            id="evt-1",
            author="user",
            event_type="user_message",
            content={"role": "user", "parts": [{"text": "hello"}]},
        ),
    )

    # Wait for probe to recover PG
    await asyncio.sleep(0.15)
    assert service.degraded is False

    # Append a new event after recovery — should create session in PG + write event
    await service.append_event(
        "sess-degraded",
        SessionEvent(
            id="evt-2",
            author="assistant",
            event_type="assistant_message",
            content={"role": "assistant", "parts": [{"text": "hi back"}]},
        ),
    )

    # Verify the session now exists in PG (primary)
    pg_session = await primary.get_session("sess-degraded")
    assert pg_session is not None
    pg_events = await primary.get_events("sess-degraded")
    pg_event_ids = [e.id for e in pg_events]
    assert "evt-2" in pg_event_ids
    await service.aclose()
