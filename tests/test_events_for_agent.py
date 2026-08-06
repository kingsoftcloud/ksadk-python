"""get_events_for_agent / count_events_for_agent 跨会话查询的存储层测试。"""

from __future__ import annotations

import pytest

from ksadk.ids import new_run_id
from ksadk.sessions.base import SessionEvent
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.sessions.local_service import LocalSessionService
from ksadk.sessions.resilient import ResilientSessionService


async def _seed(service, agent_id: str) -> None:
    """两个会话各写两条事件,timestamp 交叉,验证跨会话归并排序。"""
    for session_id in ("sess-a", "sess-b"):
        await service.create_session(agent_id=agent_id, user_id="user", session_id=session_id)
    for index, (session_id, ts) in enumerate(
        [("sess-a", 100.0), ("sess-b", 200.0), ("sess-a", 300.0), ("sess-b", 400.0)]
    ):
        await service.append_event(
            session_id,
            SessionEvent(
                session_id=session_id,
                author="user",
                event_type="user_message",
                content={"text": f"msg-{index}"},
                timestamp=ts,
            ),
        )


async def _seed_equal_timestamp_events(service) -> None:
    """Build an order where sorting by event id alone gives the wrong result."""
    await service.create_session("demo-agent", "user", session_id="sess-a")
    await service.create_session("demo-agent", "user", session_id="sess-b")
    for session_id, event_id, text in (
        ("sess-a", "evt-z", "a-seq-1"),
        ("sess-a", "evt-a", "a-seq-2"),
        ("sess-b", "evt-y", "b-seq-1"),
    ):
        await service.append_event(
            session_id,
            SessionEvent(
                id=event_id,
                session_id=session_id,
                author="user",
                event_type="user_message",
                content={"text": text},
                timestamp=100.0,
            ),
        )


@pytest.mark.asyncio
async def test_in_memory_events_for_agent_full_and_count():
    service = InMemorySessionService()
    await _seed(service, "demo-agent")

    events = await service.get_events_for_agent("demo-agent")
    assert [event.content["text"] for event in events] == ["msg-0", "msg-1", "msg-2", "msg-3"]
    assert {event.session_id for event in events} == {"sess-a", "sess-b"}
    assert await service.count_events_for_agent("demo-agent") == 4

    # 其他 agent 隔离
    assert await service.get_events_for_agent("other-agent") == []
    assert await service.count_events_for_agent("other-agent") == 0


@pytest.mark.asyncio
async def test_in_memory_events_for_agent_tail_pagination():
    service = InMemorySessionService()
    await _seed(service, "demo-agent")

    latest = await service.get_events_for_agent("demo-agent", limit=2)
    assert [event.content["text"] for event in latest] == ["msg-2", "msg-3"]

    page = await service.get_events_for_agent("demo-agent", offset=2, limit=1)
    assert [event.content["text"] for event in page] == ["msg-1"]


@pytest.mark.asyncio
async def test_in_memory_events_for_agent_filters_optional_user():
    service = InMemorySessionService()
    await service.create_session("demo-agent", "user-a", session_id="sess-a")
    await service.create_session("demo-agent", "user-b", session_id="sess-b")
    for session_id, text in (("sess-a", "a"), ("sess-b", "b")):
        await service.append_event(
            session_id,
            SessionEvent(
                session_id=session_id,
                author="user",
                event_type="user_message",
                content={"text": text},
            ),
        )
    events = await service.get_events_for_agent("demo-agent", "user-a")
    assert [event.content["text"] for event in events] == ["a"]
    assert await service.count_events_for_agent("demo-agent", "user-a") == 1


@pytest.mark.asyncio
async def test_resilient_forwards_cross_session_queries_without_cap():
    primary = InMemorySessionService()
    for index in range(51):
        session_id = f"sess-{index:02d}"
        await primary.create_session("demo-agent", "user", session_id=session_id)
        await primary.append_event(
            session_id,
            SessionEvent(
                session_id=session_id,
                author="user",
                event_type="user_message",
                content={"index": index},
                timestamp=float(index),
            ),
        )
    service = ResilientSessionService(primary)
    assert await service.count_events_for_agent("demo-agent") == 51
    assert len(await service.get_events_for_agent("demo-agent")) == 51


@pytest.mark.asyncio
async def test_resilient_list_sessions_does_not_apply_pagination_twice():
    primary = InMemorySessionService()
    for index in range(6):
        await primary.create_session(
            "demo-agent",
            "user",
            session_id=f"sess-{index:02d}",
        )
    expected = await primary.list_sessions("demo-agent", "user", offset=2, limit=2)

    service = ResilientSessionService(primary)
    actual = await service.list_sessions("demo-agent", "user", offset=2, limit=2)

    assert [session.id for session in actual] == [session.id for session in expected]
    assert await service.count_sessions("demo-agent", "user") == 6


@pytest.mark.asyncio
async def test_local_events_for_agent_full_count_and_pagination(tmp_path):
    service = LocalSessionService(db_path=tmp_path / "sessions.sqlite")
    await _seed(service, "demo-agent")

    events = await service.get_events_for_agent("demo-agent")
    assert [event.content["text"] for event in events] == ["msg-0", "msg-1", "msg-2", "msg-3"]
    assert await service.count_events_for_agent("demo-agent") == 4

    latest = await service.get_events_for_agent("demo-agent", limit=2)
    assert [event.content["text"] for event in latest] == ["msg-2", "msg-3"]

    page = await service.get_events_for_agent("demo-agent", offset=2, limit=1)
    assert [event.content["text"] for event in page] == ["msg-1"]

    assert await service.get_events_for_agent("other-agent") == []
    assert await service.count_events_for_agent("other-agent") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["memory", "local"])
async def test_events_for_agent_orders_equal_timestamps_by_seq_then_id(backend, tmp_path):
    service = (
        InMemorySessionService()
        if backend == "memory"
        else LocalSessionService(db_path=tmp_path / "ordered-sessions.sqlite")
    )
    await _seed_equal_timestamp_events(service)

    events = await service.get_events_for_agent("demo-agent")

    assert [event.id for event in events] == ["evt-y", "evt-z", "evt-a"]
    assert [(event.seq_id, event.id) for event in events] == [
        (1, "evt-y"),
        (1, "evt-z"),
        (2, "evt-a"),
    ]


def test_new_run_id_is_fixed_length_and_does_not_encode_session_id():
    first = new_run_id("sess-visible-owner")
    second = new_run_id("sess-visible-owner")

    assert first.startswith("run_")
    assert len(first) == 36
    assert first != second
    assert "sess-visible-owner" not in first
