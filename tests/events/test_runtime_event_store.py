# -*- coding: utf-8 -*-
"""RuntimeEventStore + 两类订阅 + projection + cursor 断线续传 的测试 (goal-10)。

验证:append/list/两类 subscribe(SubscribeRunEvents 单 invocation 终态关闭 /
SubscribeSessionEvents session 级 cursor stream 跨 invocation)/ projection replay,
以及断线续传(断开后按 cursor 重连不丢事件、无重复终态)。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid
from typing import Any

import pytest

from ksadk.events.canonical import ItemUpdated, RunCompleted, RunStarted, SourceRef
from ksadk.events.canonical_store import (
    RuntimeEventStore as CanonicalRuntimeEventStore,
)
from ksadk.events.canonical_store import (
    runtime_event_to_session_event,
    session_event_to_runtime_event,
)
from ksadk.events.content import TextContent
from ksadk.events.v1_compat import EventTypeV1, RuntimeEventV1
from ksadk.sessions.base import SessionEvent
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.sessions.local_service import LocalSessionService


def _canon_ev(
    event_type: str,
    session_id: str,
    run_id: str,
    seq: int,
    *,
    text: str | None = None,
    event_id: str | None = None,
):
    """Create a canonical schema-v2 event for store tests."""
    eid = event_id or f"evt_{uuid.uuid4().hex}"
    ts = time.time()
    scope = f"scope-{run_id}"
    source = SourceRef(framework="adk", native_run_id=run_id)
    common: dict[str, Any] = {
        "schema_version": 2,
        "event_id": eid,
        "seq": seq,
        "timestamp": ts,
        "run_id": run_id,
        "scope_id": scope,
        "source": source,
    }
    if event_type == "run.started":
        return RunStarted(**common, status="running")
    if event_type == "run.completed":
        return RunCompleted(**common, status="completed", output_refs=())
    if event_type == "text.delta":
        return ItemUpdated(
            **common,
            item_id=f"item-{run_id}",
            item_kind="message",
            op="append",
            update=TextContent(part_id="text-0", text=text or ""),
        )
    raise ValueError(f"unsupported canonical event type: {event_type!r}")


@pytest.fixture
async def store():
    svc = InMemorySessionService()
    await svc.create_session(agent_id="a", user_id="u", session_id="s1")
    return CanonicalRuntimeEventStore(svc), svc


# ---- SubscribeRunEvents:单 invocation,终态关闭 ----


@pytest.mark.asyncio
async def test_subscribe_run_single_invocation_terminal_close(store):
    st, _ = store
    # inv1 与 inv2 交错;subscribe_run(inv1) 只应产 inv1 且 run.completed 后关闭
    await st.append(
        "s1",
        [
            _canon_ev("run.started", "s1", "inv1", 1),
            _canon_ev("text.delta", "s1", "inv2", 2, text="other"),
            _canon_ev("text.delta", "s1", "inv1", 3, text="mine"),
            _canon_ev("run.completed", "s1", "inv1", 4),
        ],
    )
    got = [e async for e in st.subscribe_run("s1", "inv1", timeout=2)]
    assert [e.event_type for e in got] == ["run.started", "item.updated", "run.completed"]
    assert got[-1].event_type == "run.completed"  # 终态关闭


# ---- SubscribeSessionEvents:session 级跨 invocation ----


@pytest.mark.asyncio
async def test_subscribe_session_cross_invocation(store):
    st, _ = store
    await st.append(
        "s1",
        [
            _canon_ev("run.started", "s1", "inv1", 1),
            _canon_ev("text.delta", "s1", "inv2", 2, text="x"),
            _canon_ev("run.completed", "s1", "inv1", 3),
        ],
    )
    got = [e async for e in st.subscribe_session("s1", timeout=0.6)]
    # 跨 invocation:inv1 与 inv2 都产
    assert {e.run_id for e in got} == {"inv1", "inv2"}


# ---- cursor 断线续传:不丢、无重复终态 ----


@pytest.mark.asyncio
async def test_cursor_resume_no_loss_no_dup(store):
    st, _ = store
    await st.append(
        "s1",
        [
            _canon_ev("run.started", "s1", "inv1", 1),
            _canon_ev("text.delta", "s1", "inv1", 2, text="a"),
        ],
    )
    # 第一段订阅:取前 2 个事件后断开(模拟断线)
    first: list[Any] = []
    async for e in st.subscribe_session("s1", timeout=0.5):
        first.append(e)
        if len(first) >= 2:
            break
    assert len(first) == 2
    last_cursor = first[-1].seq

    # 断线期间 append 后续事件
    await st.append(
        "s1",
        [
            _canon_ev("text.delta", "s1", "inv1", 3, text="b"),
            _canon_ev("run.completed", "s1", "inv1", 4),
        ],
    )
    # 按 cursor 重连:应只补 3、4,不重发 1、2
    resumed = [e async for e in st.subscribe_session("s1", after_seq=last_cursor, timeout=0.5)]
    assert [e.seq for e in resumed] == [3, 4]
    # 无丢(1..4 全覆盖)、无重复
    all_seqs = [e.seq for e in first] + [e.seq for e in resumed]
    assert all_seqs == [1, 2, 3, 4]
    assert len(all_seqs) == len(set(all_seqs))


# ---- schema v2 canonical store (kept separate until Task 6 atomic switch) ----


def _canonical_run_started(
    *,
    event_id: str = "evt-1",
    seq: int = 0,
    timestamp: float = 1.0,
    native_cursor: str = "cursor-1",
) -> RunStarted:
    return RunStarted(
        schema_version=2,
        event_id=event_id,
        seq=seq,
        timestamp=timestamp,
        run_id="run-1",
        run_seq=1,
        scope_id="scope-1",
        source=SourceRef(
            framework="adk",
            native_event_id="native-1",
            native_cursor=native_cursor,
            native_run_id="native-run-1",
        ),
        status="running",
    )


@pytest.fixture
async def canonical_store():
    service = InMemorySessionService()
    await service.create_session(agent_id="agent-1", user_id="user-1", session_id="session-1")
    await service.create_session(agent_id="agent-1", user_id="user-1", session_id="session-2")
    return CanonicalRuntimeEventStore(service), service


@pytest.mark.asyncio
async def test_canonical_store_deduplicates_before_allocating_second_seq(canonical_store):
    store, service = canonical_store
    event = _canonical_run_started()

    first = await store.append_one("session-1", event)
    replay = await CanonicalRuntimeEventStore(service).append_one("session-1", event)

    assert replay.seq == first.seq == 1
    assert len(await store.list("session-1")) == 1


@pytest.mark.asyncio
async def test_canonical_store_concurrent_insert_loser_reuses_winner_seq(canonical_store):
    store, service = canonical_store
    event = _canonical_run_started()

    left, right = await asyncio.gather(
        store.append_one("session-1", event),
        CanonicalRuntimeEventStore(service).append_one("session-1", event),
    )

    assert left.seq == right.seq == 1
    assert len(await store.list("session-1")) == 1


@pytest.mark.asyncio
async def test_canonical_store_allows_same_event_id_in_different_sessions(canonical_store):
    store, service = canonical_store
    event = _canonical_run_started(event_id="shared-event")

    left = await store.append_one("session-1", event)
    right = await store.append_one("session-2", event)
    left_raw = (await service.get_events("session-1"))[0]
    right_raw = (await service.get_events("session-2"))[0]

    assert left.event_id == right.event_id == "shared-event"
    assert left_raw.id != right_raw.id
    assert left_raw.id != left.event_id
    assert right_raw.id != right.event_id


@pytest.mark.asyncio
async def test_canonical_store_collision_compares_every_fact_except_assigned_seq(canonical_store):
    store, _ = canonical_store
    await store.append_one("session-1", _canonical_run_started(event_id="same", seq=0))

    # Producer seq is a placeholder and is deliberately ignored.
    replay = await store.append_one("session-1", _canonical_run_started(event_id="same", seq=999))
    assert replay.seq == 1

    for changed in (
        _canonical_run_started(event_id="same", timestamp=2.0),
        _canonical_run_started(event_id="same", native_cursor="cursor-2"),
    ):
        with pytest.raises(ValueError, match="id collision"):
            await store.append_one("session-1", changed)


@pytest.mark.asyncio
async def test_canonical_store_persists_before_publishing(canonical_store):
    store, service = canonical_store
    observed: list[tuple[str, int, int]] = []

    async def publish(session_id: str, event: Any) -> None:
        observed.append((session_id, event.seq, len(await service.get_events(session_id))))

    from ksadk.events.pipeline import CanonicalEventPipeline

    pipeline = CanonicalEventPipeline(store, session_id="session-1", publisher=publish)
    (persisted,) = await pipeline.ingest(_canonical_run_started())

    assert persisted.seq == 1
    assert observed == [("session-1", 1, 1)]


@pytest.mark.asyncio
async def test_canonical_store_rejects_v1_writes(canonical_store):
    store, _service = canonical_store
    legacy = RuntimeEventV1.create(
        EventTypeV1.RUN_STARTED,
        agent_id="agent-1",
        user_id="user-1",
        session_id="session-1",
        invocation_id="run-1",
        seq_id=0,
        payload={"status": "in_progress"},
    )

    with pytest.raises(ValueError, match="schema_version=2 only"):
        await store.append_one("session-1", legacy)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_canonical_store_cursor_uses_physical_session_sequence(canonical_store):
    store, service = canonical_store
    await service.append_event(
        "session-1",
        SessionEvent(session_id="session-1", author="legacy", event_type="legacy"),
    )
    first = await store.append_one("session-1", _canonical_run_started(event_id="evt-1"))
    second = await store.append_one("session-1", _canonical_run_started(event_id="evt-2"))

    assert [first.seq, second.seq] == [2, 3]
    assert [event.event_id for event in await store.list("session-1", after_seq=2)] == ["evt-2"]
    raw = await service.get_events("session-1")
    assert raw[1].metadata["schema_version"] == 2
    assert raw[1].content["runtime_event"]["schema_version"] == 2
    assert raw[1].content["runtime_event"]["seq"] == raw[1].seq_id == 2
    assert "_ksadk_bind_session_seq" not in raw[1].metadata


def test_local_session_schema_upgrades_existing_seq_index_to_unique(tmp_path):
    database = tmp_path / "sessions.sqlite"
    LocalSessionService(db_path=database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX idx_ksadk_events_session_seq")
        connection.execute(
            "CREATE INDEX idx_ksadk_events_session_seq ON ksadk_events (session_id, seq_id)"
        )

    LocalSessionService(db_path=database)

    with sqlite3.connect(database) as connection:
        index = next(
            row
            for row in connection.execute("PRAGMA index_list('ksadk_events')")
            if row[1] == "idx_ksadk_events_session_seq"
        )
    assert index[2] == 1


@pytest.mark.asyncio
async def test_canonical_store_local_backend_roundtrip_is_durable(tmp_path):
    database = tmp_path / "canonical.sqlite"
    service = LocalSessionService(db_path=database)
    await service.create_session(
        agent_id="agent-1",
        user_id="user-1",
        session_id="session-1",
    )
    event = _canonical_run_started()

    first = await CanonicalRuntimeEventStore(service).append_one("session-1", event)
    restarted_service = LocalSessionService(db_path=database)
    replay = await CanonicalRuntimeEventStore(restarted_service).append_one("session-1", event)
    raw = (await restarted_service.get_events("session-1"))[0]

    assert replay == first
    assert raw.content["runtime_event"]["seq"] == raw.seq_id == 1
    assert "_ksadk_bind_session_seq" not in raw.metadata


def test_canonical_reader_rejects_backend_that_did_not_bind_physical_seq():
    raw = runtime_event_to_session_event("session-1", _canonical_run_started())
    raw.seq_id = 1

    with pytest.raises(ValueError, match="physical seq"):
        session_event_to_runtime_event(raw)


@pytest.mark.asyncio
async def test_canonical_store_uses_indexed_point_lookup_not_full_event_scan():
    class CountingService(InMemorySessionService):
        def __init__(self):
            super().__init__()
            self.full_scans = 0
            self.point_lookups = 0

        async def get_events(self, *args, **kwargs):
            self.full_scans += 1
            return await super().get_events(*args, **kwargs)

        async def get_event_by_id(self, *args, **kwargs):
            self.point_lookups += 1
            return await super().get_event_by_id(*args, **kwargs)

    service = CountingService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    store = CanonicalRuntimeEventStore(service)
    event = _canonical_run_started()

    await store.append_one("session-1", event)
    await store.append_one("session-1", event)

    assert service.point_lookups >= 2
    assert service.full_scans == 0


@pytest.mark.asyncio
async def test_canonical_store_run_list_uses_indexed_invocation_lookup():
    class CountingService(InMemorySessionService):
        def __init__(self):
            super().__init__()
            self.full_scans = 0
            self.run_lookups = 0

        async def get_events(self, *args, **kwargs):
            self.full_scans += 1
            return await super().get_events(*args, **kwargs)

        async def get_events_by_invocation_id(self, *args, **kwargs):
            self.run_lookups += 1
            return await super().get_events_by_invocation_id(*args, **kwargs)

    service = CountingService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    store = CanonicalRuntimeEventStore(service)
    await store.append_one("session-1", _canonical_run_started())

    events = await store.list("session-1", run_id="run-1")

    assert [event.event_id for event in events] == ["evt-1"]
    assert service.run_lookups == 1
    assert service.full_scans == 0


@pytest.mark.asyncio
async def test_canonical_subscribe_session_advances_over_legacy_rows():
    class CountingService(InMemorySessionService):
        def __init__(self):
            super().__init__()
            self.after_values: list[int | None] = []

        async def get_events(self, *args, **kwargs):
            self.after_values.append(kwargs.get("after_seq_id"))
            return await super().get_events(*args, **kwargs)

    service = CountingService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    await service.append_event("session-1", SessionEvent(author="legacy", event_type="legacy"))
    store = CanonicalRuntimeEventStore(service)

    events = [
        event
        async for event in store.subscribe_session("session-1", poll_interval=0.01, timeout=0.035)
    ]

    assert events == []
    assert service.after_values[0] == 0
    assert 1 in service.after_values[1:]
    assert service.after_values.count(0) == 1


@pytest.mark.asyncio
async def test_canonical_subscribe_run_advances_over_other_run_rows():
    class CountingService(InMemorySessionService):
        def __init__(self):
            super().__init__()
            self.after_values: list[int | None] = []

        async def get_events(self, *args, **kwargs):
            self.after_values.append(kwargs.get("after_seq_id"))
            return await super().get_events(*args, **kwargs)

    service = CountingService()
    await service.create_session("agent-1", "user-1", session_id="session-1")
    await CanonicalRuntimeEventStore(service).append_one(
        "session-1",
        _canonical_run_started().model_copy(update={"run_id": "other-run"}),
    )
    service.after_values.clear()
    store = CanonicalRuntimeEventStore(service)

    events = [
        event
        async for event in store.subscribe_run(
            "session-1", "missing-run", poll_interval=0.01, timeout=0.035
        )
    ]

    assert events == []
    assert service.after_values[0] == 0
    assert 1 in service.after_values[1:]
    assert service.after_values.count(0) == 1


@pytest.mark.asyncio
async def test_unsupported_seq_binding_backend_is_rejected_before_write():
    from ksadk.sessions.base import SessionServiceStorageCapabilities

    class UnsupportedService(InMemorySessionService):
        storage_capabilities = SessionServiceStorageCapabilities()

        def __init__(self):
            super().__init__()
            self.append_calls = 0

        async def append_event(self, session_id, event):
            self.append_calls += 1
            return await super().append_event(session_id, event)

    service = UnsupportedService()
    await service.create_session("agent-1", "user-1", session_id="session-1")

    with pytest.raises(RuntimeError, match="atomic runtime_event.seq"):
        await CanonicalRuntimeEventStore(service).append_one("session-1", _canonical_run_started())

    assert service.append_calls == 0


@pytest.mark.asyncio
async def test_backend_without_indexed_invocation_lookup_is_rejected_before_write():
    from ksadk.sessions.base import SessionServiceStorageCapabilities

    class UnsupportedService(InMemorySessionService):
        storage_capabilities = SessionServiceStorageCapabilities(
            atomic_seq_bindings=frozenset({"runtime_event.seq"}),
            indexed_event_lookup=True,
        )

        def __init__(self):
            super().__init__()
            self.append_calls = 0

        async def append_event(self, session_id, event):
            self.append_calls += 1
            return await super().append_event(session_id, event)

    service = UnsupportedService()
    await service.create_session("agent-1", "user-1", session_id="session-1")

    with pytest.raises(RuntimeError, match="indexed invocation"):
        await CanonicalRuntimeEventStore(service).append_one("session-1", _canonical_run_started())

    assert service.append_calls == 0


@pytest.mark.asyncio
async def test_local_backend_has_indexed_invocation_read_contract(tmp_path):
    database = tmp_path / "indexed-run.sqlite"
    service = LocalSessionService(db_path=database)
    await service.create_session("agent-1", "user-1", session_id="session-1")
    store = CanonicalRuntimeEventStore(service)
    await store.append_one("session-1", _canonical_run_started())
    await store.append_one(
        "session-1",
        _canonical_run_started(event_id="evt-2").model_copy(update={"run_id": "run-2"}),
    )

    events = await service.get_events_by_invocation_id("session-1", "run-1")

    assert [event.invocation_id for event in events] == ["run-1"]
    with sqlite3.connect(database) as connection:
        indexes = {row[1] for row in connection.execute("PRAGMA index_list('ksadk_events')")}
    assert "idx_ksadk_events_session_invocation_seq" in indexes


# ---- Task 2: RuntimeEventStore 作为 SessionEventStore 的 typed view ----


@pytest.mark.asyncio
async def test_typed_view_shares_cursor_with_legacy_carrier_rows():
    from ksadk.events.session_event import SessionServiceEventStore
    from ksadk.kernel.contracts import ActivationWriteGuard

    service = InMemorySessionService()
    await service.create_session(agent_id="a", user_id="u", session_id="s1")
    generic = SessionServiceEventStore(service)
    typed = CanonicalRuntimeEventStore(generic, session_id="s1")
    guard = ActivationWriteGuard(activation_id="act-1", fencing_token=1)

    legacy_first = await typed.append_one(
        "s1", _canonical_run_started(event_id="legacy-1")
    )
    typed_second = await typed.append(_canonical_run_started(event_id="typed-2"), guard=guard)
    typed_replay = await typed.append(
        _canonical_run_started(event_id="typed-2", seq=99), guard=guard
    )

    # 单日志：legacy carrier 与 typed envelope 共享同一物理 cursor
    assert (legacy_first.seq, typed_second.seq) == (1, 2)
    assert typed_replay.seq == typed_second.seq == 2
    assert [event.seq for event in await typed.list("s1")] == [1, 2]
    envelopes = await generic.read("s1", 0, 10)
    # legacy carrier 行没有 envelope marker，generic read 跳过
    assert [envelope.seq for envelope in envelopes] == [2]
    assert envelopes[0].payload["seq"] == 2
