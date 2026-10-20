"""Studio Agent schedule management and continuation ownership checks."""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from ksadk.scheduler.contracts import (
    ScheduleCommandTemplate, ScheduledTask, ScheduledTaskTarget, ScheduleSpec,
)
from ksadk.studio.errors import StudioError
from ksadk.studio.scheduler_runtime import StudioSchedulerRuntimeError


class StudioSchedulesMixin:
    """Resolve schedule authority through the Studio service's owned runtime."""

    async def create_agent_schedule(
        self,
        agent_id: str,
        *,
        display_name: str,
        prompt: str,
        schedule: ScheduleSpec,
        enabled: bool = True,
        continuity: Literal["new_session", "continue_session"] = "new_session",
        session_id: str | None = None,
    ) -> ScheduledTask:
        """Create a local-only schedule for the Kernel Runtime that owns it.

        The browser provides only human intent (prompt/calendar/continuity).
        The trusted Studio process resolves the immutable Build and concrete
        Kernel instance.  Thus a task cannot be pointed at another Agent
        Instance, smuggle a permit reference, or claim cloud scheduling.
        """

        await self.start()
        build = await self.ensure_current_build(agent_id)
        spec = self.resolve_run_spec(build.id)
        await self.scheduler_runtimes.start()
        try:
            target = await self.scheduler_runtimes.ensure_build(
                build.id,
                expected_agent_id=agent_id,
            )
            runtime = self.scheduler_runtimes.runtime_for_build(build.id)
        except StudioSchedulerRuntimeError as error:
            raise StudioError(
                error.code,
                str(error),
                status_code=409,
                details={"agentId": agent_id, "buildId": build.id},
            ) from error
        if continuity == "continue_session" and not session_id:
            raise StudioError(
                "SCHEDULE_SESSION_REQUIRED",
                "继续会话的定时任务必须选择一个已有会话。",
                status_code=422,
                field="sessionId",
            )
        if continuity == "continue_session":
            await self._require_schedule_session_owner(agent_id, session_id)
            self._require_schedule_continuation(runtime, spec.launch_context.runtime_type)
        task = ScheduledTask(
            task_id=f"sched-{uuid4().hex[:20]}",
            display_name=display_name,
            target=ScheduledTaskTarget(
                agent_id=agent_id,
                tenant_id=target.tenant_id,
                agent_instance_id=target.agent_instance_id,
                agent_version_ref=str(build.id),
                session_id=session_id,
                # This is an opaque local authority marker, never a browser
                # supplied credential. The dispatcher obtains a short-lived
                # in-process permit at the AgentControl ingress boundary.
                authorization_ref="runtime://local-kernel-ingress",
            ),
            schedule=schedule,
            command=ScheduleCommandTemplate(payload={"content": prompt}),
            enabled=enabled,
            continuity=continuity,
        )
        return self.scheduler.create_task(task)

    async def _require_schedule_session_owner(self, agent_id: str, session_id: str | None) -> None:
        session = (
            await self.session_service.get_session_metadata(session_id) if session_id else None
        )
        if session is None or session.agent_id != agent_id:
            raise StudioError(
                "SCHEDULE_SESSION_INVALID",
                "请选择该智能体已有的会话。",
                status_code=422,
                field="sessionId",
            )

    async def validate_schedule_session(
        self, agent_id: str, continuity: str, session_id: str | None, build_id: str | None
    ) -> None:
        if continuity != "continue_session":
            return
        await self._require_schedule_session_owner(agent_id, session_id)
        await self.scheduler_runtimes.ensure_build(build_id, expected_agent_id=agent_id)
        runtime = self.scheduler_runtimes.runtime_for_build(build_id)
        spec = self.resolve_run_spec(build_id)
        self._require_schedule_continuation(runtime, spec.launch_context.runtime_type)

    @staticmethod
    def _require_schedule_continuation(runtime: Any, runtime_type: str) -> None:
        """Fail closed unless the bound Provider can preserve one conversation.

        Harness owns process-local Session history without exposing the generic
        checkpoint ``resume`` verb. Codex and external adapters must declare
        the typed resume capability. An older Runtime that lacks either proof
        remains usable for normal conversations and ``new_session`` schedules;
        only the optional follow-up mode is rejected.
        """

        if runtime_type.strip().lower() == "harness":
            return
        kernel = getattr(runtime, "kernel", None)
        describe = getattr(kernel, "capabilities", None)
        matrix = describe() if callable(describe) else None
        resume = getattr(matrix, "resume", None)
        if not bool(getattr(resume, "supported", False)):
            raise StudioError(
                "SCHEDULE_CONTINUATION_UNAVAILABLE",
                "当前 Runtime 未声明可恢复的会话续接能力；可改用新会话定时任务。",
                status_code=409,
                field="continuity",
                details={"runtimeType": runtime_type},
            )

    def list_agent_schedules(self, agent_id: str) -> list[ScheduledTask]:
        return [task for task in self.scheduler.list_tasks() if task.target.agent_id == agent_id]

    def get_agent_schedule(self, agent_id: str, task_id: str) -> ScheduledTask:
        task = self.scheduler.get_task(task_id)
        if task.target.agent_id != agent_id:
            from ksadk.studio.errors import not_found

            raise not_found("schedule", task_id)
        return task

    def update_agent_schedule(
        self,
        agent_id: str,
        task_id: str,
        *,
        display_name: str,
        prompt: str,
        schedule: ScheduleSpec,
        enabled: bool,
        continuity: Literal["new_session", "continue_session"],
        session_id: str | None,
    ) -> ScheduledTask:
        existing = self.get_agent_schedule(agent_id, task_id)
        if continuity == "continue_session" and not session_id:
            raise StudioError(
                "SCHEDULE_SESSION_REQUIRED",
                "继续会话的定时任务必须选择一个已有会话。",
                status_code=422,
                field="sessionId",
            )
        task = existing.model_copy(
            update={
                "display_name": display_name,
                "schedule": schedule,
                "command": ScheduleCommandTemplate(payload={"content": prompt}),
                "enabled": enabled,
                "continuity": continuity,
                "target": existing.target.model_copy(update={"session_id": session_id}),
            }
        )
        return self.scheduler.update_task(task_id, task)

    async def run_agent_schedule_now(self, agent_id: str, task_id: str):
        self.get_agent_schedule(agent_id, task_id)
        return await self.scheduler.run_now(task_id)
