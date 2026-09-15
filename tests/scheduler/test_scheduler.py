from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from ksadk.scheduler import SchedulerEngine, SchedulerSQLiteStore, next_schedule_time
from ksadk.scheduler.contracts import (
    ScheduleCommandTemplate,
    ScheduledTask,
    ScheduledTaskTarget,
    ScheduleSpec,
)
from ksadk.scheduler.engine import SchedulerDispatchError


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _task(*, schedule: ScheduleSpec, next_run_at: datetime) -> ScheduledTask:
    return ScheduledTask(
        task_id="daily-report",
        target=ScheduledTaskTarget(
            tenant_id="local",
            agent_instance_id="local-agent",
            agent_version_ref="build_123",
            session_id="session-123",
            authorization_ref="credential://scheduler-local",
        ),
        schedule=schedule,
        command=ScheduleCommandTemplate(payload={"content": "write daily report"}),
        next_run_at=next_run_at,
        created_at=_at("2026-08-27T00:00:00Z"),
        updated_at=_at("2026-08-27T00:00:00Z"),
    )


def test_calendar_supports_interval_cron_and_dst_single_fallback() -> None:
    after = _at("2026-08-27T00:00:00Z")
    interval = ScheduleSpec(kind="interval", every_seconds=300, anchor_at=after)
    assert next_schedule_time(interval, after=after) == _at("2026-08-27T00:05:00Z")
    cron = ScheduleSpec(kind="cron", timezone="Asia/Shanghai", expression="30 9 * * 1-5")
    assert next_schedule_time(cron, after=_at("2026-08-28T01:29:00Z")) == _at(
        "2026-08-28T01:30:00Z"
    )
    fallback = ScheduleSpec(kind="cron", timezone="America/New_York", expression="30 1 * * *")
    first = next_schedule_time(fallback, after=_at("2026-11-01T04:00:00Z"))
    assert first == _at("2026-11-01T05:30:00Z")
    assert next_schedule_time(fallback, after=first) == _at("2026-11-02T06:30:00Z")


def test_board_summary_keeps_each_task_state_outside_the_history_page(tmp_path) -> None:
    store = SchedulerSQLiteStore(tmp_path / "scheduler.sqlite3")
    now = _at("2026-08-27T00:00:00Z")
    task = _task(schedule=ScheduleSpec(kind="interval", every_seconds=60), next_run_at=now)
    busy = task.model_copy(update={"task_id": "frequent-task"})
    store.put_task(task, generation=1)
    store.put_task(busy, generation=1)
    first = store.claim_manual(task, generation=1, now=now)
    store.finish(first.occurrence_id, succeeded=False, error_code="TEST_FAILURE")
    for index in range(201):
        item = store.claim_manual(busy, generation=1, now=now + timedelta(seconds=index + 1))
        store.finish(item.occurrence_id, succeeded=True)
    assert all(item.task_id == busy.task_id for item in store.list_all_occurrences())
    summaries = {item.task_id: item for item in store.list_task_occurrence_summaries()}
    assert summaries[task.task_id].state == "failed"
    assert summaries[busy.task_id].state == "succeeded"
    # A late/manual overlap cannot hide an earlier still-active execution.
    active = store.claim_manual(busy, generation=1, now=now + timedelta(milliseconds=500))
    summaries = {item.task_id: item for item in store.list_task_occurrence_summaries()}
    assert summaries[busy.task_id].occurrence_id == active.occurrence_id


class _RecordingDispatcher:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.fail = fail

    async def dispatch(self, task: ScheduledTask, occurrence) -> str:
        self.calls.append((task.task_id, occurrence.occurrence_id, occurrence.trigger))
        if self.fail:
            raise SchedulerDispatchError("QUEUE_FULL", "kernel inbox is full")
        return "command-123"


@pytest.mark.asyncio
async def test_engine_claims_once_dispatches_only_through_boundary_and_settles(tmp_path) -> None:
    now = _at("2026-08-27T00:00:00Z")
    store = SchedulerSQLiteStore(tmp_path / "scheduler.sqlite3")
    task = _task(
        schedule=ScheduleSpec(kind="interval", every_seconds=60, anchor_at=now),
        next_run_at=now,
    )
    assert store.put_task(task) == 1
    dispatcher = _RecordingDispatcher()
    engine = SchedulerEngine(store, dispatcher, owner_id="owner-a", clock=lambda: now)

    occurrences = await engine.tick()

    assert [(value.state, value.command_id) for value in occurrences] == [
        ("accepted", "command-123")
    ]
    assert occurrences[0].session_id.startswith("sched-occ_")
    assert dispatcher.calls == [("daily-report", occurrences[0].occurrence_id, "schedule")]
    assert await engine.tick() == []
    settled = await engine.settle(occurrences[0].occurrence_id, succeeded=True)
    assert settled.state == "succeeded"
    assert settled.completed_at is not None
    assert [transition.state for transition in settled.transitions] == [
        "claimed",
        "accepted",
        "succeeded",
    ]
    assert settled.claimed_at == now


@pytest.mark.asyncio
async def test_forbid_records_skip_until_runtime_settles_prior_occurrence(tmp_path) -> None:
    now = _at("2026-08-27T00:00:00Z")
    store = SchedulerSQLiteStore(tmp_path / "scheduler.sqlite3")
    task = _task(
        schedule=ScheduleSpec(kind="interval", every_seconds=60, anchor_at=now),
        next_run_at=now,
    )
    store.put_task(task)
    dispatcher = _RecordingDispatcher()
    engine = SchedulerEngine(store, dispatcher, owner_id="owner-a", clock=lambda: now)
    first = (await engine.tick())[0]
    engine.clock = lambda: now + timedelta(minutes=1)

    second = (await engine.tick())[0]

    assert first.state == "accepted"
    assert second.state == "skipped"
    assert second.detail == "concurrency_forbid_active_occurrence"
    assert len(dispatcher.calls) == 1


@pytest.mark.asyncio
async def test_dispatch_failure_is_durable_failure_not_fake_completion(tmp_path) -> None:
    now = _at("2026-08-27T00:00:00Z")
    store = SchedulerSQLiteStore(tmp_path / "scheduler.sqlite3")
    store.put_task(_task(schedule=ScheduleSpec(kind="once", at=now), next_run_at=now))
    engine = SchedulerEngine(store, _RecordingDispatcher(fail=True), clock=lambda: now)

    occurrence = (await engine.tick())[0]

    assert occurrence.state == "failed"
    assert occurrence.error_code == "QUEUE_FULL"
    assert occurrence.completed_at is not None


@pytest.mark.asyncio
async def test_run_now_preserves_natural_schedule_and_respects_concurrency(tmp_path) -> None:
    now = _at("2026-08-27T00:00:00Z")
    natural_next = now + timedelta(hours=1)
    store = SchedulerSQLiteStore(tmp_path / "scheduler.sqlite3")
    store.put_task(
        _task(
            schedule=ScheduleSpec(kind="interval", every_seconds=3600, anchor_at=now),
            next_run_at=natural_next,
        )
    )
    engine = SchedulerEngine(store, _RecordingDispatcher(), clock=lambda: now)

    manual = await engine.run_now("daily-report")

    assert manual.trigger == "manual"
    assert manual.state == "accepted"
    stored, _generation = store.get_task("daily-report") or (None, None)
    assert stored is not None
    assert stored.next_run_at == natural_next
    with pytest.raises(SchedulerDispatchError, match="earlier occurrence is still active"):
        await engine.run_now("daily-report")


@pytest.mark.asyncio
async def test_misfire_skip_is_recorded_once_without_backlog_replay(tmp_path) -> None:
    scheduled_for = _at("2026-08-27T00:00:00Z")
    now = scheduled_for + timedelta(hours=2)
    store = SchedulerSQLiteStore(tmp_path / "scheduler.sqlite3")
    store.put_task(
        _task(
            schedule=ScheduleSpec(
                kind="interval",
                every_seconds=60,
                anchor_at=scheduled_for,
                misfire_policy="skip",
            ),
            next_run_at=scheduled_for,
        )
    )
    dispatcher = _RecordingDispatcher()
    occurrences = await SchedulerEngine(store, dispatcher, clock=lambda: now).tick()

    assert [(item.state, item.detail) for item in occurrences] == [("skipped", "misfire_skipped")]
    assert dispatcher.calls == []
    stored, _generation = store.get_task("daily-report") or (None, None)
    assert stored is not None
    assert stored.next_run_at == now + timedelta(minutes=1)


@pytest.mark.asyncio
async def test_monitor_waits_for_runtime_then_claims_without_restart(tmp_path) -> None:
    now = _at("2026-08-27T00:00:00Z")
    store = SchedulerSQLiteStore(tmp_path / "scheduler.sqlite3")
    store.put_task(
        _task(
            schedule=ScheduleSpec(kind="interval", every_seconds=60, anchor_at=now),
            next_run_at=now,
        )
    )
    runtime_ready = False
    dispatcher = _RecordingDispatcher()
    engine = SchedulerEngine(
        store,
        dispatcher,
        clock=lambda: now,
        tick_guard=lambda: runtime_ready,
    )

    await engine.start(poll_seconds=0.01)
    try:
        await asyncio.sleep(0.03)
        assert store.list_occurrences("daily-report") == []
        assert engine.status()["lastScanResult"] == "waiting_runtime"

        runtime_ready = True
        for _ in range(20):
            if store.list_occurrences("daily-report"):
                break
            await asyncio.sleep(0.01)
    finally:
        await engine.stop()

    history = store.list_occurrences("daily-report")
    assert [item.state for item in history] == ["accepted"]
    assert dispatcher.calls == [("daily-report", history[0].occurrence_id, "schedule")]
    assert engine.status()["running"] is False


def test_cross_task_history_survives_task_deletion(tmp_path) -> None:
    now = _at("2026-08-27T00:00:00Z")
    store = SchedulerSQLiteStore(tmp_path / "scheduler.sqlite3")
    task = _task(schedule=ScheduleSpec(kind="once", at=now), next_run_at=now)
    store.put_task(task)
    occurrence = store.claim_and_advance(
        task,
        generation=1,
        next_run_at=None,
        claimed_at=now,
    )
    assert occurrence is not None
    assert store.delete_task(task.task_id) is True
    assert [item.occurrence_id for item in store.list_all_occurrences()] == [
        occurrence.occurrence_id
    ]


def test_monitor_can_restart_on_a_fresh_event_loop(tmp_path) -> None:
    """Studio test clients and embedded hosts may recreate their event loop."""

    store = SchedulerSQLiteStore(tmp_path / "scheduler.sqlite3")
    engine = SchedulerEngine(
        store,
        _RecordingDispatcher(),
        tick_guard=lambda: False,
    )

    async def cycle() -> None:
        await engine.start(poll_seconds=0.01)
        await asyncio.sleep(0)
        await engine.stop()

    asyncio.run(cycle())
    asyncio.run(cycle())
    assert engine.status()["running"] is False
