# -*- coding: utf-8 -*-
"""RuntimeEventStore + 两类订阅 + projection + cursor 断线续传 的测试 (goal-10)。

验证:append/list/两类 subscribe(SubscribeRunEvents 单 invocation 终态关闭 /
SubscribeSessionEvents session 级 cursor stream 跨 invocation)/ projection replay,
以及断线续传(断开后按 cursor 重连不丢事件、无重复终态)。
"""

from __future__ import annotations

import pytest

from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.events.store import RuntimeEventStore
from ksadk.sessions.base import SessionEvent
from ksadk.sessions.in_memory import InMemorySessionService


def _ev(
    event_type,
    session_id,
    invocation_id,
    seq,
    payload=None,
    phase=None,
    agent="a",
    user="u",
    event_id=None,
):
    return RuntimeEvent.create(
        event_type,
        agent_id=agent,
        user_id=user,
        session_id=session_id,
        invocation_id=invocation_id,
        seq_id=seq,
        payload=payload or {},
        phase=phase,
        event_id=event_id,
    )


@pytest.fixture
async def store():
    svc = InMemorySessionService()
    await svc.create_session(agent_id="a", user_id="u", session_id="s1")
    return RuntimeEventStore(svc), svc


# ---- append / list ----


@pytest.mark.asyncio
async def test_append_and_list_roundtrip(store):
    st, svc = store
    await st.append(
        [
            _ev(EventType.RUN_STARTED, "s1", "inv1", 1, {"status": "in_progress"}),
            _ev(EventType.TEXT_DELTA, "s1", "inv1", 2, {"text": "你好"}, phase="commentary"),
        ]
    )
    # legacy 事件(无 runtime marker)应被过滤
    await svc.append_event(
        "s1", SessionEvent(session_id="s1", author="a", event_type="assistant_message")
    )
    events = await st.list("s1")
    assert len(events) == 2
    assert [e.event_type for e in events] == [EventType.RUN_STARTED, EventType.TEXT_DELTA]
    assert events[1].phase == "commentary"
    assert events[1].payload == {"text": "你好"}
    # seq_id 单调(服务分配的 session 级 cursor)
    assert [e.seq_id for e in events] == sorted(e.seq_id for e in events)


@pytest.mark.asyncio
async def test_append_returns_store_assigned_session_cursor(store):
    st, svc = store
    await svc.append_event(
        "s1", SessionEvent(session_id="s1", author="legacy", event_type="legacy")
    )

    appended = await st.append(
        [_ev(EventType.RUN_STARTED, "s1", "inv1", 99, {"status": "in_progress"})]
    )

    assert [event.seq_id for event in appended] == [2]
    assert [event.seq_id for event in await st.list("s1")] == [2]


@pytest.mark.asyncio
async def test_append_is_idempotent_by_durable_event_id(store):
    st, svc = store
    event = _ev(
        EventType.TEXT_DELTA,
        "s1",
        "inv1",
        1,
        {"text": "same"},
        phase="commentary",
        event_id="wire-event-1",
    )

    first = await st.append_one(event)
    replay = await RuntimeEventStore(svc).append_one(event)

    assert replay.seq_id == first.seq_id
    assert [item.event_id for item in await st.list("s1")] == ["wire-event-1"]


@pytest.mark.asyncio
async def test_reserve_once_reports_only_the_durable_winner(store):
    st, svc = store
    event = _ev(
        EventType.APPROVAL_RESOLVED,
        "s1",
        "inv1",
        1,
        {
            "approval_id": "approval-1",
            "call_id": "approval-1",
            "decision": "approved",
        },
        event_id="approval-reservation-1",
    )

    first, first_created = await st.reserve_once(event)
    replay, replay_created = await RuntimeEventStore(svc).reserve_once(event)

    assert first_created is True
    assert replay_created is False
    assert replay.seq_id == first.seq_id


@pytest.mark.asyncio
async def test_event_id_collision_does_not_drop_distinct_content(store):
    st, _ = store
    await st.append_one(
        _ev(
            EventType.TEXT_DELTA,
            "s1",
            "inv1",
            1,
            {"text": "first"},
            phase="commentary",
            event_id="wire-event-1",
        )
    )

    with pytest.raises(ValueError, match="id collision"):
        await st.append_one(
            _ev(
                EventType.TEXT_DELTA,
                "s1",
                "inv1",
                2,
                {"text": "second"},
                phase="commentary",
                event_id="wire-event-1",
            )
        )


@pytest.mark.asyncio
async def test_task_agent_locator_survives_event_store_recreation(store):
    st, svc = store
    await st.set_task_agent("s1", "task-1", "agent-remote")

    restarted = RuntimeEventStore(svc)
    assert await restarted.get_task_agent("s1", "task-1") == "agent-remote"


@pytest.mark.asyncio
async def test_list_cursor_and_invocation_filter(store):
    st, _ = store
    await st.append(
        [
            _ev(EventType.RUN_STARTED, "s1", "inv1", 1, {"status": "in_progress"}),
            _ev(EventType.TEXT_DELTA, "s1", "inv2", 2, {"text": "x"}, phase="commentary"),
            _ev(EventType.RUN_COMPLETED, "s1", "inv1", 3, {"status": "completed"}),
        ]
    )
    after = await st.list("s1", after_seq_id=1)
    assert [e.event_type for e in after] == [EventType.TEXT_DELTA, EventType.RUN_COMPLETED]
    inv1 = await st.list("s1", invocation_id="inv1")
    assert [e.event_type for e in inv1] == [EventType.RUN_STARTED, EventType.RUN_COMPLETED]
    before = await st.list("s1", before_seq_id=3)
    assert [e.event_type for e in before] == [EventType.RUN_STARTED, EventType.TEXT_DELTA]


# ---- SubscribeRunEvents:单 invocation,终态关闭 ----


@pytest.mark.asyncio
async def test_subscribe_run_single_invocation_terminal_close(store):
    st, _ = store
    # inv1 与 inv2 交错;subscribe_run(inv1) 只应产 inv1 且 run.completed 后关闭
    await st.append(
        [
            _ev(EventType.RUN_STARTED, "s1", "inv1", 1, {"status": "in_progress"}),
            _ev(EventType.TEXT_DELTA, "s1", "inv2", 2, {"text": "other"}, phase="commentary"),
            _ev(EventType.TEXT_DELTA, "s1", "inv1", 3, {"text": "mine"}, phase="commentary"),
            _ev(EventType.RUN_COMPLETED, "s1", "inv1", 4, {"status": "completed"}),
        ]
    )
    got = [e async for e in st.subscribe_run("s1", "inv1", timeout=2)]
    assert [e.event_type for e in got] == [
        EventType.RUN_STARTED,
        EventType.TEXT_DELTA,
        EventType.RUN_COMPLETED,
    ]
    assert got[-1].event_type == EventType.RUN_COMPLETED  # 终态关闭


# ---- SubscribeSessionEvents:session 级跨 invocation ----


@pytest.mark.asyncio
async def test_subscribe_session_cross_invocation(store):
    st, _ = store
    await st.append(
        [
            _ev(EventType.RUN_STARTED, "s1", "inv1", 1, {"status": "in_progress"}),
            _ev(EventType.TEXT_DELTA, "s1", "inv2", 2, {"text": "x"}, phase="commentary"),
            _ev(EventType.RUN_COMPLETED, "s1", "inv1", 3, {"status": "completed"}),
        ]
    )
    got = [e async for e in st.subscribe_session("s1", timeout=0.6)]
    # 跨 invocation:inv1 与 inv2 都产
    assert {e.invocation_id for e in got} == {"inv1", "inv2"}


# ---- cursor 断线续传:不丢、无重复终态 ----


@pytest.mark.asyncio
async def test_cursor_resume_no_loss_no_dup(store):
    st, _ = store
    await st.append(
        [
            _ev(EventType.RUN_STARTED, "s1", "inv1", 1, {"status": "in_progress"}),
            _ev(EventType.TEXT_DELTA, "s1", "inv1", 2, {"text": "a"}, phase="commentary"),
        ]
    )
    # 第一段订阅:取前 2 个事件后断开(模拟断线)
    first: list[RuntimeEvent] = []
    async for e in st.subscribe_session("s1", timeout=0.5):
        first.append(e)
        if len(first) >= 2:
            break
    assert len(first) == 2
    last_cursor = first[-1].seq_id

    # 断线期间 append 后续事件
    await st.append(
        [
            _ev(EventType.TEXT_DELTA, "s1", "inv1", 3, {"text": "b"}, phase="commentary"),
            _ev(EventType.RUN_COMPLETED, "s1", "inv1", 4, {"status": "completed"}),
        ]
    )
    # 按 cursor 重连:应只补 3、4,不重发 1、2
    resumed = [e async for e in st.subscribe_session("s1", after_seq_id=last_cursor, timeout=0.5)]
    assert [e.seq_id for e in resumed] == [3, 4]
    # 无丢(1..4 全覆盖)、无重复
    all_seqs = [e.seq_id for e in first] + [e.seq_id for e in resumed]
    assert all_seqs == [1, 2, 3, 4]
    assert len(all_seqs) == len(set(all_seqs))


# ---- projection / replay ----


@pytest.mark.asyncio
async def test_project_replay_default(store):
    st, _ = store
    await st.append(
        [
            _ev(EventType.RUN_STARTED, "s1", "inv1", 1, {"status": "in_progress"}),
            _ev(EventType.RUN_COMPLETED, "s1", "inv1", 2, {"status": "completed"}),
        ]
    )
    replay = await st.project("s1")
    assert [e.event_type for e in replay] == [EventType.RUN_STARTED, EventType.RUN_COMPLETED]


@pytest.mark.asyncio
async def test_project_fold_projection(store):
    st, _ = store
    await st.append(
        [
            _ev(EventType.TEXT_DELTA, "s1", "inv1", 1, {"text": "你"}, phase="commentary"),
            _ev(EventType.TEXT_DELTA, "s1", "inv1", 2, {"text": "好"}, phase="commentary"),
            _ev(EventType.RUN_COMPLETED, "s1", "inv1", 3, {"status": "completed"}),
        ]
    )

    def concat_text(acc, event):
        return acc + (
            event.payload.get("text", "") if event.event_type == EventType.TEXT_DELTA else ""
        )

    text = await st.project("s1", concat_text, initial="")
    assert text == "你好"
