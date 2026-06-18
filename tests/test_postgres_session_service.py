from __future__ import annotations

import os

import pytest

from ksadk.sessions.base import SessionEvent

pytestmark = pytest.mark.asyncio


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

        assert [session.agent_id for session in await service_a.list_sessions("agent-a", "user-1")] == [
            "agent-a"
        ]
        assert [session.agent_id for session in await service_b.list_sessions("agent-b", "user-1")] == [
            "agent-b"
        ]
        assert await service_a.list_sessions("agent-b", "user-1") == []
        assert await service_b.list_sessions("agent-a", "user-1") == []
    finally:
        await service_a.delete_session(session_id)
        await service_b.delete_session(session_id)
        await service_a.aclose()
        await service_b.aclose()
