"""Studio-facing lifecycle for the local Scheduler Lite backend.

This service owns only local scheduling intent and occurrence history.  It
does not silently start a second runtime or submit prompts directly: when the
local AgentKernel route is unavailable it reports that condition explicitly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ksadk.kernel.ingress import kernel_route_active
from ksadk.scheduler import (
    AgentControlSchedulerDispatcher,
    SchedulerDispatchError,
    SchedulerEngine,
    SchedulerSQLiteStore,
    next_schedule_time,
)
from ksadk.scheduler.contracts import ScheduledTask, ScheduledTaskTarget, ScheduleOccurrence
from ksadk.studio.errors import StudioError, not_found
from ksadk.studio.workspace import Workspace

if TYPE_CHECKING:
    from ksadk.studio.scheduler_runtime import StudioScheduledKernelRegistry


class StudioSchedulerService:
    """CRUD and explicit local process lifecycle for ``ScheduledTask/v1``."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        runtime_registry: StudioScheduledKernelRegistry | None = None,
    ) -> None:
        self.workspace = workspace
        self.runtime_registry = runtime_registry
        self.store = SchedulerSQLiteStore(
            workspace.resolve(".agentkit/scheduler/scheduler.sqlite3")
        )
        dispatcher = (
            AgentControlSchedulerDispatcher(
                runtime_registry.submit,
                event_reader=runtime_registry.read_events,
                target_preparer=self._prepare_target,
            )
            if runtime_registry is not None
            else AgentControlSchedulerDispatcher()
        )
        self.engine = SchedulerEngine(
            self.store,
            dispatcher,
            # Keep the local monitor alive when Studio starts before a Runtime.
            # It observes availability on every scan and never claims work while
            # the trusted Kernel route is absent.
            tick_guard=self._runtime_available,
        )

    def availability(self) -> dict[str, object]:
        active = self._runtime_available()
        engine = self.engine.status()
        return {
            "available": active,
            "scope": "local_studio_process",
            "alwaysOn": False,
            "reason": "agent_kernel_route_inactive" if not active else None,
            "triggerActive": bool(active and engine["running"]),
            "store": "sqlite",
            **engine,
        }

    def list_tasks(self) -> list[ScheduledTask]:
        return [task for task, _generation in self.store.list_tasks()]

    def get_task(self, task_id: str) -> ScheduledTask:
        item = self.store.get_task(task_id)
        if item is None:
            raise not_found("schedule", task_id)
        return item[0]

    def list_occurrences(self, task_id: str, *, limit: int = 50) -> list[ScheduleOccurrence]:
        # A deleted task intentionally retains occurrence history, so query the
        # history table directly rather than requiring a live task row.
        return self.store.list_occurrences(task_id, limit=limit)

    def list_all_occurrences(self, *, limit: int = 200) -> list[ScheduleOccurrence]:
        return self.store.list_all_occurrences(limit=limit)

    def create_task(self, task: ScheduledTask) -> ScheduledTask:
        if self.store.get_task(task.task_id) is not None:
            raise StudioError(
                "SCHEDULE_ALREADY_EXISTS",
                "该定时任务已存在",
                status_code=409,
                details={"id": task.task_id},
            )
        prepared = self._prepare(task)
        self.store.put_task(prepared, generation=1)
        return prepared

    def update_task(self, task_id: str, task: ScheduledTask) -> ScheduledTask:
        if task.task_id != task_id:
            raise StudioError(
                "SCHEDULE_ID_IMMUTABLE",
                "定时任务 ID 不可修改",
                status_code=422,
                field="taskId",
            )
        existing = self.store.get_task(task_id)
        if existing is None:
            raise not_found("schedule", task_id)
        old, generation = existing
        prepared = self._prepare(
            task.model_copy(update={"created_at": old.created_at}),
            reset_next_run=True,
        )
        self.store.put_task(prepared, generation=generation + 1)
        return prepared

    def delete_task(self, task_id: str) -> None:
        if not self.store.delete_task(task_id):
            raise not_found("schedule", task_id)

    async def run_now(self, task_id: str) -> ScheduleOccurrence:
        self._require_kernel_route()
        try:
            return await self.engine.run_now(task_id)
        except KeyError as exc:
            raise not_found("schedule", task_id) from exc
        except SchedulerDispatchError as exc:
            raise StudioError(
                exc.code,
                exc.detail,
                status_code=409 if exc.code in {"TASK_DISABLED", "CONCURRENCY_FORBID"} else 422,
            ) from exc

    async def tick(self) -> list[ScheduleOccurrence]:
        self._require_kernel_route()
        return await self.engine.tick()

    async def start_if_available(self) -> bool:
        # The monitor is process-local and cheap. Starting it unconditionally
        # is what lets a Runtime launched after Studio startup begin scheduling
        # without a daemon restart. The engine's tick_guard is the fail-closed
        # admission boundary while no Kernel route exists.
        if self.runtime_registry is not None:
            await self.runtime_registry.start()
        await self.engine.start()
        return self._runtime_available()

    async def stop(self) -> None:
        try:
            await self.engine.stop()
        finally:
            if self.runtime_registry is not None:
                await self.runtime_registry.close()

    def _prepare(self, task: ScheduledTask, *, reset_next_run: bool = False) -> ScheduledTask:
        now = datetime.now(timezone.utc)
        next_run_at = task.next_run_at
        if reset_next_run or next_run_at is None:
            next_run_at = next_schedule_time(
                task.schedule,
                after=now,
                anchor_at=task.created_at,
            )
        if task.enabled and next_run_at is None:
            raise StudioError(
                "SCHEDULE_NOT_FUTURE",
                "启用的单次任务必须指定未来的触发时间",
                status_code=422,
                field="schedule.at",
            )
        return task.model_copy(update={"next_run_at": next_run_at, "updated_at": now})

    def _runtime_available(self) -> bool:
        if self.runtime_registry is not None:
            return self.runtime_registry.started
        return kernel_route_active()

    def _require_kernel_route(self) -> None:
        if not self._runtime_available():
            raise StudioError(
                "SCHEDULER_RUNTIME_UNAVAILABLE",
                "本地 Agent Kernel 未启用，定时任务不会伪装为已运行",
                status_code=409,
            )

    async def _prepare_target(self, target: ScheduledTaskTarget) -> object:
        """Resolve the build-pinned Kernel target, preserving typed failures.

        StudioScheduledKernelRegistry.ensure_target raises
        StudioSchedulerRuntimeError (for example SCHEDULER_BUILD_UNAVAILABLE
        when agent_version_ref is not a real immutable Build id).  Without this
        adapter the SchedulerEngine generic except Exception collapses every such
        failure into an opaque DISPATCH_FAILED occurrence, hiding the real reason.
        Re-raise it as a SchedulerDispatchError so the durable occurrence keeps
        the actionable error code instead of the catch-all.
        """

        from ksadk.studio.scheduler_runtime import StudioSchedulerRuntimeError

        try:
            return await self.runtime_registry.ensure_target(target)  # type: ignore[union-attr]
        except StudioSchedulerRuntimeError as error:
            raise SchedulerDispatchError(error.code, str(error)) from error


__all__ = ["StudioSchedulerService"]
