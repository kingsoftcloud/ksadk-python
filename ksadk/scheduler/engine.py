"""Explicitly started local Scheduler Lite engine.

There is intentionally no import-time loop and no Studio lifespan hook here.
Local schedules run only while a caller starts this engine; deployment-wide
always-on scheduling belongs to the Server/Operator phase, not this component.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import uuid4

from ksadk.scheduler.calendar import next_schedule_time
from ksadk.scheduler.contracts import ScheduledTask, ScheduleOccurrence
from ksadk.scheduler.sqlite_store import SchedulerSQLiteStore


class SchedulerEventReader(Protocol):
    """Read canonical SessionEvents for one accepted occurrence."""

    async def read_events(self, occurrence: ScheduleOccurrence) -> Sequence[tuple[int, object]]: ...


class SchedulerDispatchReceipt(str):
    """A backward-compatible dispatcher result with an event-log cursor.

    It remains a ``str`` so existing local dispatcher implementations keep
    working.  The new ``accepted_seq`` lets the scheduler resume from exactly
    the AgentControl admission fact rather than guessing from wall-clock time.
    """

    accepted_seq: int | None

    def __new__(cls, command_id: str, *, accepted_seq: int | None = None):
        value = super().__new__(cls, command_id)
        value.accepted_seq = accepted_seq
        return value


class SchedulerDispatchError(RuntimeError):
    """A typed dispatcher failure safe to persist in occurrence history."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class SchedulerDispatcher(Protocol):
    """The one execution seam. Implementations must submit AgentControl only."""

    async def dispatch(self, task: ScheduledTask, occurrence: ScheduleOccurrence) -> str:
        """Return AgentControl's accepted command ID, or raise a typed error."""


Clock = Callable[[], datetime]
TickGuard = Callable[[], bool]


class SchedulerEngine:
    def __init__(
        self,
        store: SchedulerSQLiteStore,
        dispatcher: SchedulerDispatcher,
        *,
        owner_id: str | None = None,
        clock: Clock | None = None,
        tick_guard: TickGuard | None = None,
        lease_seconds: int = 30,
    ) -> None:
        self.store = store
        self.dispatcher = dispatcher
        self.owner_id = owner_id or f"studio-{uuid4().hex}"
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.tick_guard = tick_guard
        self.lease_seconds = lease_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._poll_seconds: float | None = None
        self._last_scan_at: datetime | None = None
        self._last_scan_result: str | None = None
        self._last_scan_detail: str | None = None
        self._next_scan_at: datetime | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict[str, object]:
        """Return a UI-safe snapshot of the local trigger process.

        This is operational state, not a claim that every configured task is
        executable.  ``waiting_runtime`` deliberately means the monitor is
        alive but will not claim due work until its trusted Kernel route is
        available again.
        """

        return {
            "running": self.running,
            "ownerId": self.owner_id,
            "pollSeconds": self._poll_seconds,
            "lastScanAt": self._last_scan_at,
            "lastScanResult": self._last_scan_result,
            "lastScanDetail": self._last_scan_detail,
            "nextScanAt": self._next_scan_at,
        }

    async def tick(self) -> list[ScheduleOccurrence]:
        """Claim every due occurrence once and submit it through AgentControl."""

        now = self.clock().astimezone(timezone.utc)
        self._last_scan_at = now
        if self.tick_guard is not None and not self.tick_guard():
            self._last_scan_result = "waiting_runtime"
            self._last_scan_detail = "agent_kernel_route_inactive"
            return []
        owns_lease = await asyncio.to_thread(
            self.store.acquire_lease,
            owner_id=self.owner_id,
            now=now,
            ttl_seconds=self.lease_seconds,
        )
        if not owns_lease:
            self._last_scan_result = "lease_not_owned"
            self._last_scan_detail = "another_local_scheduler_owns_the_lease"
            return []
        await self.reconcile()
        due = await asyncio.to_thread(self.store.list_due, now)
        result: list[ScheduleOccurrence] = []
        for task, generation in due:
            occurrence = await self._claim_scheduled(task, generation, now)
            if occurrence is None:
                continue
            result.append(occurrence)
            if occurrence.state == "skipped":
                continue
            try:
                dispatch = await self.dispatcher.dispatch(task, occurrence)
            except SchedulerDispatchError as exc:
                result[-1] = await asyncio.to_thread(
                    self.store.finish,
                    occurrence.occurrence_id,
                    succeeded=False,
                    error_code=exc.code,
                    detail=exc.detail,
                )
            except Exception as exc:  # noqa: BLE001 - normalize boundary errors
                result[-1] = await asyncio.to_thread(
                    self.store.finish,
                    occurrence.occurrence_id,
                    succeeded=False,
                    error_code="DISPATCH_FAILED",
                    detail=str(exc)[:1024],
                )
            else:
                result[-1] = await asyncio.to_thread(
                    self.store.mark_accepted,
                    occurrence.occurrence_id,
                    str(dispatch),
                    accepted_seq=getattr(dispatch, "accepted_seq", None),
                )
        self._last_scan_result = "ok"
        self._last_scan_detail = f"claimed={len(result)}"
        return result

    async def _claim_scheduled(
        self,
        task: ScheduledTask,
        generation: int,
        now: datetime,
    ) -> ScheduleOccurrence | None:
        assert task.next_run_at is not None
        missed = task.next_run_at < now
        next_run_at = next_schedule_time(
            task.schedule,
            after=now if missed else task.next_run_at,
            anchor_at=task.created_at,
        )
        if missed and task.schedule.misfire_policy == "skip":
            return await asyncio.to_thread(
                self.store.claim_and_advance,
                task,
                generation=generation,
                next_run_at=next_run_at,
                state="skipped",
                detail="misfire_skipped",
                claimed_at=now,
            )
        if task.concurrency_policy == "forbid" and await asyncio.to_thread(
            self.store.has_active_occurrence, task.task_id
        ):
            return await asyncio.to_thread(
                self.store.claim_and_advance,
                task,
                generation=generation,
                next_run_at=next_run_at,
                state="skipped",
                detail="concurrency_forbid_active_occurrence",
                claimed_at=now,
            )
        return await asyncio.to_thread(
            self.store.claim_and_advance,
            task,
            generation=generation,
            next_run_at=next_run_at,
            claimed_at=now,
        )

    async def run_now(self, task_id: str) -> ScheduleOccurrence:
        """Explicit user action; it still uses the dispatcher and durable log."""

        value = await asyncio.to_thread(self.store.get_task, task_id)
        if value is None:
            raise KeyError(task_id)
        task, generation = value
        if not task.enabled:
            raise SchedulerDispatchError("TASK_DISABLED", "scheduled task is disabled")
        if task.concurrency_policy == "forbid" and await asyncio.to_thread(
            self.store.has_active_occurrence, task.task_id
        ):
            raise SchedulerDispatchError(
                "CONCURRENCY_FORBID", "an earlier occurrence is still active"
            )
        # Run-now has a separate manual occurrence identity. It is intentionally
        # not an implicit reschedule and does not advance the natural next time.
        now = self.clock().astimezone(timezone.utc)
        occurrence = await asyncio.to_thread(
            self.store.claim_manual,
            task,
            generation=generation,
            now=now,
        )
        if occurrence is None:
            raise SchedulerDispatchError("TASK_CHANGED", "task changed while starting manually")
        try:
            dispatch = await self.dispatcher.dispatch(task, occurrence)
        except SchedulerDispatchError as exc:
            return await asyncio.to_thread(
                self.store.finish,
                occurrence.occurrence_id,
                succeeded=False,
                error_code=exc.code,
                detail=exc.detail,
            )
        except Exception as exc:  # noqa: BLE001
            return await asyncio.to_thread(
                self.store.finish,
                occurrence.occurrence_id,
                succeeded=False,
                error_code="DISPATCH_FAILED",
                detail=str(exc)[:1024],
            )
        return await asyncio.to_thread(
            self.store.mark_accepted,
            occurrence.occurrence_id,
            str(dispatch),
            accepted_seq=getattr(dispatch, "accepted_seq", None),
        )

    async def reconcile(self) -> list[ScheduleOccurrence]:
        """Settle accepted work only from correlated canonical runtime facts.

        ``AgentControlReceipt.accepted`` means the command entered Inbox; it
        is deliberately not a success signal.  The control run-transition
        event carries the originating command id, then the matching runtime
        terminal event decides the immutable occurrence outcome.
        """

        reader = getattr(self.dispatcher, "read_events", None)
        if not callable(reader):
            return []
        changed: list[ScheduleOccurrence] = []
        for occurrence in await asyncio.to_thread(self.store.list_active_occurrences):
            if not occurrence.command_id or occurrence.target is None:
                # Legacy local records lack the new immutable target snapshot.
                # Preserve their accepted status rather than guessing a target.
                continue
            try:
                events = await reader(occurrence)
            except Exception:  # noqa: BLE001 - retain accepted state for a later poll
                continue
            current = occurrence
            for seq, envelope in events:
                current = await self._reconcile_event(current, seq=seq, envelope=envelope)
                if current.state in {"succeeded", "failed", "skipped", "cancelled"}:
                    break
            if current != occurrence:
                changed.append(current)
        return changed

    async def _reconcile_event(
        self, occurrence: ScheduleOccurrence, *, seq: int, envelope: object
    ) -> ScheduleOccurrence:
        event_type = str(getattr(envelope, "event_type", ""))
        event_run_id = str(getattr(envelope, "run_id", "") or "")
        causation_id = str(getattr(envelope, "causation_id", "") or "")
        payload = getattr(envelope, "payload", {}) or {}
        if not isinstance(payload, dict):
            payload = {}

        if event_type == "control.run_transition" and causation_id == occurrence.command_id:
            run_id = event_run_id or str(payload.get("run_id") or "")
            if run_id:
                run_state = str(payload.get("state") or "")
                if run_state not in {"running", "paused", "waiting"}:
                    return await asyncio.to_thread(
                        self.store.bind_run,
                        occurrence.occurrence_id,
                        run_id=run_id,
                        last_event_seq=seq,
                    )
                return await asyncio.to_thread(
                    self.store.mark_running,
                    occurrence.occurrence_id,
                    run_id=run_id,
                    last_event_seq=seq,
                )
        if occurrence.run_id and event_run_id == occurrence.run_id:
            if event_type == "run.completed":
                return await asyncio.to_thread(
                    self.store.finish,
                    occurrence.occurrence_id,
                    succeeded=True,
                    detail="runtime_completed",
                )
            if event_type in {"run.failed", "run.canceled", "run.interrupted"}:
                error = payload.get("error")
                code = (
                    str(error.get("code") or "RUNTIME_FAILED")
                    if isinstance(error, dict)
                    else event_type.upper().replace(".", "_")
                )
                detail = (
                    str(error.get("message") or event_type)
                    if isinstance(error, dict)
                    else event_type
                )
                return await asyncio.to_thread(
                    self.store.finish,
                    occurrence.occurrence_id,
                    succeeded=False,
                    error_code=code[:128],
                    detail=detail[:1024],
                )
        return await asyncio.to_thread(
            self.store.advance_reconciliation_cursor,
            occurrence.occurrence_id,
            last_event_seq=seq,
        )

    async def settle(
        self,
        occurrence_id: str,
        *,
        succeeded: bool,
        error_code: str | None = None,
        detail: str | None = None,
    ) -> ScheduleOccurrence:
        """Called by the runtime-event reconciler, never by receipt handling."""

        return await asyncio.to_thread(
            self.store.finish,
            occurrence_id,
            succeeded=succeeded,
            error_code=error_code,
            detail=detail,
        )

    async def start(self, *, poll_seconds: float = 5.0) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if self._task is not None and not self._task.done():
            return
        # asyncio primitives bind to the loop that first waits on them. Studio
        # tests and embedded hosts may start/stop the same service through a
        # fresh event loop, so never reuse the previous loop's Event.
        self._stop = asyncio.Event()
        self._poll_seconds = poll_seconds

        async def loop() -> None:
            while not self._stop.is_set():
                try:
                    await self.tick()
                except Exception as exc:  # noqa: BLE001 - keep the monitor alive
                    self._last_scan_at = self.clock().astimezone(timezone.utc)
                    self._last_scan_result = "error"
                    self._last_scan_detail = f"{type(exc).__name__}: {exc}"[:512]
                self._next_scan_at = self.clock().astimezone(timezone.utc) + timedelta(
                    seconds=poll_seconds
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=poll_seconds)
                except TimeoutError:
                    pass

        self._task = asyncio.create_task(loop(), name="ksadk-local-scheduler")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        await self._task
        self._task = None
        self._next_scan_at = None


__all__ = [
    "SchedulerDispatchError",
    "SchedulerDispatchReceipt",
    "SchedulerDispatcher",
    "SchedulerEngine",
    "SchedulerEventReader",
]
