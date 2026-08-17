# -*- coding: utf-8 -*-
"""Generic SessionEventStore（单一日志）+ guard 校验 + typed RuntimeEventStore view。

对应 docs/superpowers/plans/2026-08-17-agent-runtime-v2-phase1-agent-kernel.md Task 2。
验证：control 与 runtime 共享同一单调 session cursor、typed guard 写权限、
store 覆盖调用方未持久化 seq、订阅 replay→live 去重，以及
RuntimeEventStore 作为 family=runtime/family_version=2 的 typed view。
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Any

import pytest

from ksadk.events.canonical import RunStarted, SourceRef
from ksadk.events.canonical_store import RuntimeEventStore as TypedRuntimeEventStore
from ksadk.events.session_event import SessionServiceEventStore
from ksadk.kernel.contracts import (
    ActivationWriteGuard,
    AdmissionWriteGuard,
    SessionEventEnvelope,
)
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.sessions.local_service import LocalSessionService

SESSION_ID = "s1"


def admission_guard() -> AdmissionWriteGuard:
    return AdmissionWriteGuard(
        authorization_ref="auth-ref-1", command_id=uuid.uuid4()
    )


def activation_guard(fence: int = 7) -> ActivationWriteGuard:
    return ActivationWriteGuard(activation_id="act-1", fencing_token=fence)


def control_event(event_type: str = "control.command_accepted") -> SessionEventEnvelope:
    return SessionEventEnvelope(
        event_id=uuid.uuid4(),
        session_id=SESSION_ID,
        seq=0,
        timestamp="2026-08-17T00:00:00+00:00",
        family="control",
        family_version=1,
        event_type=event_type,
        payload={"command_id": str(uuid.uuid4()), "status": "accepted"},
        actor_ref="admission",
    )


def runtime_event(event_id: str | None = None) -> RunStarted:
    return RunStarted(
        schema_version=2,
        event_id=event_id or f"evt_{uuid.uuid4().hex}",
        seq=0,
        timestamp=time.time(),
        run_id="run-1",
        run_seq=1,
        scope_id="scope-1",
        source=SourceRef(framework="ksadk", native_run_id="run-1"),
        status="running",
    )


async def _make_service(backend: str, tmp_path):
    if backend == "memory":
        service = InMemorySessionService()
    elif backend == "sqlite":
        service = LocalSessionService(db_path=tmp_path / f"{backend}.sqlite")
    elif backend == "postgres":
        dsn = os.getenv("KSADK_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("Set KSADK_TEST_POSTGRES_DSN to run Postgres session integration tests")
        from ksadk.sessions.postgres_service import PostgresSessionService

        service = PostgresSessionService(dsn=dsn)
    await service.create_session(agent_id="a", user_id="u", session_id=SESSION_ID)
    return service


@pytest.fixture(params=["memory", "sqlite"])
async def store(request, tmp_path):
    service = await _make_service(request.param, tmp_path)
    return SessionServiceEventStore(service)


# ---- 单日志：control 与 runtime 共享同一单调 cursor ----


@pytest.mark.asyncio
async def test_control_and_runtime_share_one_monotonic_cursor(store):
    accepted = await store.append(control_event(), guard=admission_guard())
    runtime = await TypedRuntimeEventStore(store, session_id=SESSION_ID).append(
        runtime_event(), guard=activation_guard(fence=7)
    )
    assert (accepted.seq, runtime.seq) == (1, 2)
    families = [e.family for e in await store.read(SESSION_ID, 0, 10)]
    assert families == ["control", "runtime"]


# ---- guard 校验：禁止无 guard / 裸布尔 / 错误 guard 类型 ----


@pytest.mark.asyncio
async def test_append_without_guard_is_rejected(store):
    with pytest.raises(TypeError, match="guard"):
        await store.append(control_event())  # type: ignore[misc]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_guard", [True, False, None, "activation", 1])
async def test_append_rejects_untyped_guards(store, bad_guard):
    with pytest.raises(TypeError, match="typed SessionEventWriteGuard"):
        await store.append(control_event(), guard=bad_guard)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_admission_guard_cannot_write_runtime_facts(store):
    with pytest.raises(PermissionError, match="Admission"):
        await store.append(
            SessionEventEnvelope(
                event_id=uuid.uuid4(),
                session_id=SESSION_ID,
                seq=0,
                timestamp="2026-08-17T00:00:00+00:00",
                family="runtime",
                family_version=2,
                event_type="run.started",
                payload={},
            ),
            guard=admission_guard(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["control.command_claimed", "control.recovery_decided"])
async def test_admission_guard_only_allows_accepted_or_rejected(store, event_type):
    with pytest.raises(PermissionError, match="command_accepted"):
        await store.append(control_event(event_type), guard=admission_guard())


@pytest.mark.asyncio
async def test_runtime_view_rejects_admission_guard(store):
    with pytest.raises(TypeError, match="ActivationWriteGuard"):
        await TypedRuntimeEventStore(store, session_id=SESSION_ID).append(
            runtime_event(), guard=admission_guard()  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_runtime_view_rejects_missing_guard(store):
    with pytest.raises(TypeError, match="ActivationWriteGuard"):
        await TypedRuntimeEventStore(store, session_id=SESSION_ID).append(runtime_event())


# ---- seq 归属：store 覆盖调用方未持久化 seq ----


@pytest.mark.asyncio
async def test_store_overwrites_caller_seq_with_allocated_cursor(store):
    envelope = control_event().model_copy(update={"seq": 999})
    persisted = await store.append(envelope, guard=admission_guard())
    assert persisted.seq == 1
    assert (await store.read(SESSION_ID, 0, 10))[0].seq == 1


@pytest.mark.asyncio
async def test_session_seq_is_unique_and_monotonic_across_families(store):
    typed = TypedRuntimeEventStore(store, session_id=SESSION_ID)
    for index in range(3):
        if index % 2 == 0:
            await store.append(control_event(), guard=admission_guard())
        else:
            await typed.append(runtime_event(), guard=activation_guard())
    events = await store.read(SESSION_ID, 0, 10)
    assert [e.seq for e in events] == [1, 2, 3]
    assert [e.family for e in events] == ["control", "runtime", "control"]


# ---- idempotency：同一 event_id 复用同一 cursor ----


@pytest.mark.asyncio
async def test_append_is_idempotent_per_event_id(store):
    envelope = control_event()
    first = await store.append(envelope, guard=admission_guard())
    replay = await store.append(envelope.model_copy(update={"seq": 42}), guard=admission_guard())
    assert replay.seq == first.seq == 1
    assert len(await store.read(SESSION_ID, 0, 10)) == 1


@pytest.mark.asyncio
async def test_append_collision_on_same_event_id_raises(store):
    await store.append(control_event(), guard=admission_guard())
    different = control_event().model_copy(update={"payload": {"status": "other"}})
    different = different.model_copy(update={"event_id": (await store.read(SESSION_ID, 0, 1))[0].event_id})
    with pytest.raises(ValueError, match="collision"):
        await store.append(different, guard=admission_guard())


# ---- runtime payload seq 与 envelope seq 一致 ----


@pytest.mark.asyncio
async def test_runtime_payload_seq_matches_envelope_seq(store):
    persisted = await TypedRuntimeEventStore(store, session_id=SESSION_ID).append(
        runtime_event(), guard=activation_guard()
    )
    envelope = (await store.read(SESSION_ID, 0, 10))[0]
    assert envelope.family == "runtime"
    assert envelope.family_version == 2
    assert envelope.payload["seq"] == envelope.seq == persisted.seq


@pytest.mark.asyncio
async def test_read_validates_runtime_payload_against_schema_v2(store):
    await store.append(
        SessionEventEnvelope(
            event_id=uuid.uuid4(),
            session_id=SESSION_ID,
            seq=0,
            timestamp="2026-08-17T00:00:00+00:00",
            family="runtime",
            family_version=2,
            event_type="run.started",
            payload={"schema_version": 2, "event_id": "x", "seq": 0},
        ),
        guard=activation_guard(),
    )
    with pytest.raises(Exception):
        await store.read(SESSION_ID, 0, 10)


@pytest.mark.asyncio
async def test_read_returns_unknown_family_as_opaque_envelope(store):
    await store.append(
        SessionEventEnvelope(
            event_id=uuid.uuid4(),
            session_id=SESSION_ID,
            seq=0,
            timestamp="2026-08-17T00:00:00+00:00",
            family="workflow",
            family_version=1,
            event_type="workflow.started",
            payload={"opaque": True},
        ),
        guard=activation_guard(),
    )
    envelope = (await store.read(SESSION_ID, 0, 10))[0]
    assert envelope.family == "workflow"
    assert envelope.payload == {"opaque": True}


# ---- 订阅：replay → live 去重 ----


@pytest.mark.asyncio
async def test_subscribe_replays_history_then_follows_live_without_duplicates(store):
    typed = TypedRuntimeEventStore(store, session_id=SESSION_ID)
    await store.append(control_event(), guard=admission_guard())
    await typed.append(runtime_event(), guard=activation_guard())

    received: list[SessionEventEnvelope] = []

    async def consume() -> None:
        async for envelope in store.subscribe(SESSION_ID, 0, poll_interval=0.01, timeout=0.3):
            received.append(envelope)
            if len(received) == 4:
                return

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    await store.append(
        control_event("control.command_rejected"), guard=admission_guard()
    )
    await typed.append(runtime_event(), guard=activation_guard())
    await asyncio.wait_for(consumer, timeout=2)

    seqs = [envelope.seq for envelope in received]
    assert seqs == [1, 2, 3, 4]
    assert len(seqs) == len(set(seqs))


@pytest.mark.asyncio
async def test_subscribe_after_seq_only_yields_new_events(store):
    typed = TypedRuntimeEventStore(store, session_id=SESSION_ID)
    await store.append(control_event(), guard=admission_guard())
    await typed.append(runtime_event(), guard=activation_guard())
    events = [e async for e in store.subscribe(SESSION_ID, 1, poll_interval=0.01, timeout=0.05)]
    assert [e.seq for e in events] == [2]


# ---- typed view 与旧 carrier 共存：legacy 行被 read 过滤 ----


@pytest.mark.asyncio
async def test_read_skips_legacy_rows_without_envelope_marker(store):
    await store.session_service.append_event(SESSION_ID, _legacy_event())
    persisted = await store.append(control_event(), guard=admission_guard())
    envelopes = await store.read(SESSION_ID, 0, 10)
    assert [e.seq for e in envelopes] == [persisted.seq]


def _legacy_event() -> Any:
    from ksadk.sessions.base import SessionEvent

    return SessionEvent(session_id=SESSION_ID, author="legacy", event_type="legacy")
