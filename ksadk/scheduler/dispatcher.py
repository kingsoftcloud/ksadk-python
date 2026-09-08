"""Local Scheduler Lite's explicit AgentControl dispatcher."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

from ksadk.kernel.contracts import (
    AgentControlCommand,
    AgentControlPermit,
    AgentControlReceipt,
)
from ksadk.kernel.ingress import (
    map_scheduler_request,
    submit_command,
    subscribe_projected,
    trusted_context,
)
from ksadk.scheduler.contracts import ScheduledTask, ScheduledTaskTarget, ScheduleOccurrence
from ksadk.scheduler.engine import SchedulerDispatchError, SchedulerDispatchReceipt

SubmitCommand = Callable[[AgentControlCommand, AgentControlPermit], Awaitable[AgentControlReceipt]]
ReadOccurrenceEvents = Callable[
    [ScheduleOccurrence], Awaitable[Sequence[tuple[int, object]]]
]
PrepareTarget = Callable[[ScheduledTaskTarget], Awaitable[object]]


class AgentControlSchedulerDispatcher:
    """Translate one occurrence into a single durable AgentControl enqueue.

    This dispatcher is intentionally local-runtime only.  A hosted scheduler
    must obtain a Server-issued permit at its admission boundary in Phase 3;
    this class never makes local permits look valid against hosted JWKS.
    """

    def __init__(
        self,
        submitter: SubmitCommand | None = None,
        *,
        event_reader: ReadOccurrenceEvents | None = None,
        target_preparer: PrepareTarget | None = None,
    ) -> None:
        self._submitter = submitter or _submit_via_ingress
        self._event_reader = event_reader
        self._target_preparer = target_preparer

    async def dispatch(
        self, task: ScheduledTask, occurrence: ScheduleOccurrence
    ) -> SchedulerDispatchReceipt:
        target = task.target
        if self._target_preparer is not None:
            await self._target_preparer(target)
        trusted = trusted_context(
            source_kind="scheduler",
            source_ref=occurrence.occurrence_id,
            tenant_id=target.tenant_id,
            agent_instance_id=target.agent_instance_id,
            session_id=occurrence.session_id,
            operations=("enqueue",),
        )
        command = map_scheduler_request(
            session_id=occurrence.session_id,
            idempotency_key=occurrence.occurrence_id,
            content=task.command.payload["content"],
            occurrence_id=occurrence.occurrence_id,
            trusted=trusted,
        )
        receipt = await self._submitter(command, trusted.permit)
        if receipt.status not in {"accepted", "duplicate"}:
            error = receipt.error
            raise SchedulerDispatchError(
                error.code if error else f"KERNEL_{receipt.status.upper()}",
                error.message if error else f"kernel rejected occurrence: {receipt.status}",
            )
        return SchedulerDispatchReceipt(str(receipt.command_id), accepted_seq=receipt.accepted_seq)

    async def read_events(self, occurrence: ScheduleOccurrence) -> tuple[tuple[int, object], ...]:
        """Read currently available canonical facts without holding an SSE open.

        The local scheduler wakes frequently; each read resumes from the
        durable session cursor persisted on the occurrence.  This avoids a
        hidden second event log and lets a Studio restart continue settling an
        already-accepted run.
        """

        if self._event_reader is not None:
            return tuple(await self._event_reader(occurrence))

        target = occurrence.target
        if target is None:
            return ()
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
        stream = subscribe_projected(
            occurrence.session_id,
            trusted=trusted,
            after_seq=cursor,
            projector=lambda event: event,
        )
        events: list[tuple[int, object]] = []
        try:
            while len(events) < 100:
                try:
                    value = await asyncio.wait_for(anext(stream), timeout=0.05)
                except TimeoutError:
                    break
                events.append(value)
        finally:
            await stream.aclose()
        return tuple(events)


async def _submit_via_ingress(
    command: AgentControlCommand, permit: AgentControlPermit
) -> AgentControlReceipt:
    return await submit_command(command, permit=permit)


__all__ = ["AgentControlSchedulerDispatcher"]
