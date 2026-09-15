from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from ksadk.kernel.contracts import SessionEventEnvelope
from ksadk.scheduler import SchedulerEngine, SchedulerSQLiteStore
from ksadk.scheduler.contracts import (
    ScheduleCommandTemplate,
    ScheduledTask,
    ScheduledTaskTarget,
    ScheduleSpec,
)
from ksadk.scheduler.engine import SchedulerDispatchReceipt


def _task(now: datetime) -> ScheduledTask:
    return ScheduledTask(
        task_id="daily-report",
        target=ScheduledTaskTarget(
            tenant_id="local",
            agent_instance_id="local-agent",
            agent_version_ref="build-1",
            authorization_ref="credential://local-scheduler",
        ),
        schedule=ScheduleSpec(kind="once", at=now),
        command=ScheduleCommandTemplate(payload={"content": "write report"}),
        next_run_at=now,
    )


def _event(
    *,
    seq: int,
    family: str,
    family_version: int,
    event_type: str,
    run_id: str | None = None,
    causation_id: str | None = None,
    payload: dict | None = None,
) -> SessionEventEnvelope:
    return SessionEventEnvelope(
        event_id=uuid4(),
        session_id="sched-placeholder",
        seq=seq,
        timestamp="2026-08-27T00:00:00Z",
        family=family,  # type: ignore[arg-type]
        family_version=family_version,
        event_type=event_type,
        run_id=run_id,
        causation_id=causation_id,
        payload=payload or {},
    )


class _ReplayingDispatcher:
    def __init__(self, batches: list[tuple[tuple[int, object], ...]]) -> None:
        self.batches = batches

    async def dispatch(self, _task, _occurrence) -> SchedulerDispatchReceipt:
        return SchedulerDispatchReceipt("command-123", accepted_seq=10)

    async def read_events(self, _occurrence):
        return self.batches.pop(0) if self.batches else ()


@pytest.mark.asyncio
async def test_reconcile_uses_command_then_run_identity_to_settle_terminal(tmp_path) -> None:
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    store = SchedulerSQLiteStore(tmp_path / "scheduler.sqlite3")
    store.put_task(_task(now))
    dispatcher = _ReplayingDispatcher(
        [
            (
                (
                    11,
                    _event(
                        seq=11,
                        family="control",
                        family_version=1,
                        event_type="control.run_transition",
                        run_id="run-123",
                        causation_id="command-123",
                        payload={"state": "pending", "run_id": "run-123"},
                    ),
                ),
                (
                    12,
                    _event(
                        seq=12,
                        family="control",
                        family_version=1,
                        event_type="control.run_transition",
                        run_id="run-123",
                        causation_id="command-123",
                        payload={"state": "running", "run_id": "run-123"},
                    ),
                ),
                (
                    13,
                    _event(
                        seq=13,
                        family="runtime",
                        family_version=2,
                        event_type="run.completed",
                        run_id="run-123",
                    ),
                ),
            )
        ]
    )
    engine = SchedulerEngine(store, dispatcher, clock=lambda: now)

    accepted = (await engine.tick())[0]
    assert accepted.state == "accepted"
    assert accepted.accepted_seq == 10
    assert accepted.target is not None

    settled = await engine.reconcile()

    assert [(item.state, item.run_id, item.detail) for item in settled] == [
        ("succeeded", "run-123", "runtime_completed")
    ]
    stored = store.list_occurrences("daily-report")[0]
    assert stored.completed_at is not None


@pytest.mark.asyncio
async def test_reconcile_never_settles_from_an_unrelated_terminal_event(tmp_path) -> None:
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    store = SchedulerSQLiteStore(tmp_path / "scheduler.sqlite3")
    store.put_task(_task(now))
    dispatcher = _ReplayingDispatcher(
        [
            (
                (
                    11,
                    _event(
                        seq=11,
                        family="runtime",
                        family_version=2,
                        event_type="run.completed",
                        run_id="another-run",
                    ),
                ),
            )
        ]
    )
    engine = SchedulerEngine(store, dispatcher, clock=lambda: now)
    accepted = (await engine.tick())[0]

    reconciled = await engine.reconcile()
    assert [item.state for item in reconciled] == ["accepted"]
    stored = store.list_occurrences("daily-report")[0]
    assert stored.state == "accepted"
    assert stored.command_id == accepted.command_id
    assert stored.last_event_seq == 11


@pytest.mark.asyncio
async def test_restarted_engine_reconciles_a_previously_accepted_occurrence(tmp_path) -> None:
    """A Studio restart must not strand accepted work or synthesize success."""

    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    store = SchedulerSQLiteStore(tmp_path / "scheduler.sqlite3")
    store.put_task(_task(now))
    dispatcher = _ReplayingDispatcher(
        [
            (
                (
                    11,
                    _event(
                        seq=11,
                        family="control",
                        family_version=1,
                        event_type="control.run_transition",
                        run_id="run-after-restart",
                        causation_id="command-123",
                        payload={"state": "running", "run_id": "run-after-restart"},
                    ),
                ),
                (
                    12,
                    _event(
                        seq=12,
                        family="runtime",
                        family_version=2,
                        event_type="run.completed",
                        run_id="run-after-restart",
                    ),
                ),
            )
        ]
    )
    first_process = SchedulerEngine(store, dispatcher, clock=lambda: now)
    accepted = (await first_process.tick())[0]
    assert accepted.state == "accepted"

    # Recreate the engine over the same SQLite file. Only correlated durable
    # SessionEvents are allowed to move the old accepted row to terminal.
    restarted = SchedulerEngine(store, dispatcher, clock=lambda: now)
    settled = await restarted.reconcile()

    assert [(item.state, item.run_id) for item in settled] == [("succeeded", "run-after-restart")]
    stored = store.list_occurrences("daily-report")[0]
    assert [transition.state for transition in stored.transitions] == [
        "claimed",
        "accepted",
        "running",
        "succeeded",
    ]
