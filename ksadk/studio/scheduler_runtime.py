"""Scheduler-specific target, title and event adapters for the Build registry."""

from __future__ import annotations

from ksadk.kernel.contracts import (
    AgentControlCommand,
    AgentControlPermit,
    AgentControlReceipt,
    SessionEventSubscription,
)
from ksadk.kernel.ingress import trusted_context
from ksadk.scheduler.contracts import ScheduledTaskTarget, ScheduleOccurrence
from ksadk.studio.kernel_registry import (
    ResolveAdapterProvider,
    ResolveBuild,
    StudioBuildKernelError,
    StudioBuildKernelRegistry,
    StudioBuildRuntimeTarget,
)


class StudioSchedulerRuntimeError(StudioBuildKernelError):
    """Stable local scheduling failure safe to persist on an occurrence."""


StudioScheduledRuntimeTarget = StudioBuildRuntimeTarget


class StudioScheduledKernelRegistry(StudioBuildKernelRegistry):
    """Add Scheduler references and session presentation to shared Build Kernels."""

    @classmethod
    def _error(cls, code: str, message: str) -> StudioSchedulerRuntimeError:
        return StudioSchedulerRuntimeError(f"SCHEDULER_{code}", message)

    async def ensure_target(
        self, target: ScheduledTaskTarget
    ) -> StudioScheduledRuntimeTarget:
        build_id = str(target.agent_version_ref or "").strip()
        exact = await self.ensure_build(
            build_id,
            expected_agent_id=target.agent_id,
        )
        if (
            target.tenant_id != exact.tenant_id
            or target.agent_instance_id != exact.agent_instance_id
        ):
            raise StudioSchedulerRuntimeError(
                "SCHEDULER_TARGET_MISMATCH",
                "定时任务目标与不可变 Build 的 Kernel 身份不一致",
            )
        return exact

    async def submit(
        self,
        command: AgentControlCommand,
        permit: AgentControlPermit,
    ) -> AgentControlReceipt:
        entry = self._entry_for_instance(command.agent_instance_id)
        if command.tenant_id != entry.target.tenant_id:
            raise self._error("TARGET_MISMATCH", "AgentControl tenant 与 Build Kernel 不一致")
        prompt = command.payload.get("content")
        has_prompt = isinstance(prompt, str) and bool(prompt.strip())
        await self.ensure_session(
            entry.target.build_id,
            command.session_id,
            # Studio sessions are owned by the authenticated local principal;
            # the scheduler tenant is an authority scope, not a user id.
            "local-user",
            title=f"定时任务 · {prompt[:40]}" if has_prompt else None,
            metadata={
                "title_source": "scheduler",
                "first_prompt": prompt,
                "last_prompt": prompt,
            } if has_prompt else None,
        )
        return await self.submit_control(command, permit)

    async def read_events(
        self, occurrence: ScheduleOccurrence
    ) -> tuple[tuple[int, object], ...]:
        target = occurrence.target
        if target is None:
            return ()
        await self.ensure_target(target)
        entry = self._entry_for_instance(target.agent_instance_id)
        trusted = trusted_context(
            source_kind="scheduler",
            source_ref=occurrence.occurrence_id,
            tenant_id=target.tenant_id,
            agent_instance_id=target.agent_instance_id,
            session_id=occurrence.session_id,
            operations=("subscribe_events",),
        )
        cursor = occurrence.last_event_seq
        if cursor is None:
            cursor = occurrence.accepted_seq or 0
        subscription = SessionEventSubscription(
            tenant_id=trusted.tenant_id,
            agent_instance_id=trusted.agent_instance_id,
            session_id=occurrence.session_id,
            authorization_ref=trusted.permit.permit_id,
            after_seq=cursor,
        )
        result: list[tuple[int, object]] = []
        stream = entry.runtime.kernel.subscribe(
            subscription,
            permit=trusted.permit,
            timeout=0.05,
        )
        try:
            async for envelope in stream:
                result.append((int(envelope.seq), envelope))
                if len(result) >= 100:
                    break
        finally:
            await stream.aclose()
        return tuple(result)

__all__ = [
    "ResolveAdapterProvider",
    "ResolveBuild",
    "StudioScheduledKernelRegistry",
    "StudioScheduledRuntimeTarget",
    "StudioSchedulerRuntimeError",
]
