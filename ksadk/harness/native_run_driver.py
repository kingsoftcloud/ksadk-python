"""Own local driver tasks and project their committed events to stream consumers."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from ksadk.harness.engine.base import ExecutionEngineError
from ksadk.harness.events import EventType, RuntimeEvent
from ksadk.harness.native.state import NativeEvent, RunPhase
from ksadk.harness.native_driver_failure import NativeDriverFailure, driver_failure
from ksadk.runtime import RunHandle

_TERMINALS = {
    "run.completed": EventType.RUN_COMPLETED,
    "run.failed": EventType.RUN_FAILED,
    "run.canceled": EventType.RUN_CANCELED,
    "run.interrupted": EventType.RUN_INTERRUPTED,
}


class NativeRunDriver:
    """Own local driver tasks and project their committed events to stream consumers."""

    def _ensure_driver(self, run_id: str) -> None:
        ownership = self._ownership.get(run_id)
        if ownership is not None and ownership[1].done():
            self._driver_result(ownership[1])
        self._kernel.attach(run_id)
        previous = self._drivers.get(run_id)
        if previous is None or previous.done():
            if (
                ownership is None
                and self._kernel.leased_ownership
                and not self._kernel.snapshot(run_id).phase.terminal
            ):
                released = asyncio.Event()
                keeper = asyncio.create_task(self._retain_ownership(run_id, released))
                keeper.add_done_callback(
                    lambda completed: completed.exception() if not completed.cancelled() else None
                )
                self._ownership[run_id] = (released, keeper)
            from ksadk.harness.public_output import current_public_output_sink, public_output_scope

            sink = current_public_output_sink()
            if sink is not None:
                self._public_output_sinks[run_id] = sink
            with public_output_scope(self._public_output_sinks.get(run_id)):
                task = asyncio.create_task(self._drive(run_id))
            # Retrieve exceptions even if a client disconnects; stream/control
            # still observes task.result(), and native facts are never replaced.
            task.add_done_callback(
                lambda completed: completed.exception() if not completed.cancelled() else None
            )
            self._drivers[run_id] = task

    async def _retain_ownership(self, run_id: str, released: asyncio.Event) -> None:
        try:
            await self._kernel.retain_ownership(run_id, released)
        except Exception as error:
            raise driver_failure(error, snapshot=self._kernel.snapshot(run_id)) from None
        finally:
            self._changed[run_id].set()

    async def _drive(self, run_id: str) -> None:
        terminal = False
        try:
            while True:
                before = self._kernel.snapshot(run_id)
                after = await self._kernel.advance(run_id)
                self._changed[run_id].set()
                terminal = after.phase.terminal
                if after.phase.terminal or after.revision == before.revision:
                    return
                await asyncio.sleep(0)
        except Exception as error:
            # Observe committed facts only. A lost fence or failed Store write
            # cannot authorize this host driver to manufacture a terminal.
            snapshot, effect_kind = None, None
            try:
                snapshot = self._kernel.snapshot(run_id)
                if snapshot.pending_intent is not None:
                    intent = self._kernel.store.get_intent(
                        snapshot.scope, run_id, snapshot.pending_intent
                    )
                    effect_kind = intent.kind if intent is not None else None
            except Exception:
                pass
            if ownership := self._ownership.get(run_id):
                ownership[0].set()
            raise driver_failure(error, snapshot=snapshot, effect_kind=effect_kind) from None
        finally:
            if terminal and (ownership := self._ownership.get(run_id)):
                ownership[0].set()
            self._changed[run_id].set()

    async def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        self._check(handle)
        run_id = handle.run_id
        if run_id in self._streaming:
            raise ExecutionEngineError("only one active event consumer is allowed per native run")
        self._streaming.add(run_id)
        try:
            while True:
                changed = self._changed[run_id]
                changed.clear()
                for native in self._kernel.events(run_id, after_seq=self._cursors[run_id]):
                    projected = self._project_event(native, handle)
                    self._cursors[run_id] = native.seq
                    yield projected
                snapshot = self._check(handle)
                if self._cursors[run_id] < snapshot.event_seq:
                    continue
                driver = self._drivers.get(run_id)
                if driver is None or driver.done():
                    if (ownership := self._ownership.get(run_id)) and ownership[1].done():
                        self._driver_result(ownership[1])
                    if driver is not None:
                        self._driver_result(driver)
                    return
                if snapshot.phase.terminal:
                    return
                # Deltas may commit while a model effect is still in flight.
                # Refresh committed facts without coupling the kernel to a UI.
                try:
                    await asyncio.wait_for(changed.wait(), timeout=0.1)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._streaming.discard(run_id)

    @staticmethod
    def _driver_result(driver: asyncio.Task[None]) -> None:
        try:
            driver.result()
        except NativeDriverFailure as error:
            raise error from None
        except (Exception, asyncio.CancelledError) as error:
            raise driver_failure(error) from None

    def _project_event(self, native: NativeEvent, handle: RunHandle) -> RuntimeEvent:
        payload = {
            **native.payload,
            "native_event_kind": native.kind,
            "timestamp_source": "unavailable",
            "turn_id": native.turn_id,
        }
        phase = None
        if native.kind == "text.completed":
            kind, phase = EventType.TEXT_COMPLETED, "final_answer"
        elif native.kind == "text.delta":
            kind, phase = EventType.TEXT_DELTA, "commentary"
            payload["provisional"] = True
        elif native.kind in _TERMINALS:
            kind = _TERMINALS[native.kind]
            payload["status"] = native.kind.removeprefix("run.")
            if kind == EventType.RUN_FAILED:
                payload["error"] = payload.get("reason", "native_run_failed")
        elif native.kind == "run.started":
            kind, payload["status"] = EventType.RUN_STARTED, "running"
        elif native.kind == "usage.reported":
            kind = EventType.USAGE_REPORTED
            payload["total_tokens"] = int(payload["input_tokens"]) + int(payload["output_tokens"])
            payload["cached_tokens"] = payload.pop("cached_input_tokens", 0)
        elif native.kind in {EventType.APPROVAL_REQUESTED, EventType.APPROVAL_RESOLVED}:
            kind = native.kind
        else:
            kind, payload["status"] = EventType.RUN_PROGRESS, "running"
            if (
                native.kind == "command.consumed"
                and payload.get("kind") == "pause"
                and payload.get("reason") in {"applied", "already_paused"}
            ):
                payload["status"] = "paused"
        scope = self._kernel.scope
        return RuntimeEvent.create(
            kind,
            agent_id=scope.agent_id,
            user_id=scope.user_id,
            session_id=handle.session_id,
            invocation_id=handle.run_id,
            run_id=handle.run_id,
            scope_id=f"agent:{scope.agent_id}",
            seq_id=native.seq,
            event_id=native.event_id,
            timestamp=0,
            phase=phase,
            payload=payload,
        )

    def release_notification_lease(self, source_run_id: str, task_id: str) -> None:
        """Release a stale durable lease for an admitted-but-not-driven turn.

        A notification turn admitted via ``start_notification_turn`` attaches the
        run (acquiring its durable lease) before the driver is scheduled. If the
        process exits in that crash window, the lease is orphaned and a restart
        cannot re-drive the turn until the lease TTL elapses. The wakeup
        coordinator calls this on service shutdown for admitted watches so a
        restart can re-drive promptly. If the run still has an active driver, it
        is left untouched — the activation close owns its disposal. Idempotent.
        """
        from .native.notification_admission import notification_run_id

        run_id = notification_run_id(self._kernel.scope, source_run_id, task_id)
        driver = self._drivers.get(run_id)
        if driver is not None and not driver.done():
            return  # actively driven; activation close will dispose it
        # Admission registers a handle before scheduling its first driver.
        # Sealing that never-started local handle prevents activation disposal
        # from turning a recoverable durable admission into a cancellation.
        # A driven turn (including a failed driver) keeps ordinary close rules.
        unstarted = (
            run_id in self._handles
            and driver is None
            and run_id not in self._ownership
            and self._kernel.snapshot(run_id).phase == RunPhase.CREATED
        )
        self._kernel.release(run_id)
        if unstarted:
            self._closed.add(run_id)
