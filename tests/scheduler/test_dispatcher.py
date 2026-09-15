from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ksadk.kernel.contracts import AgentControlReceipt
from ksadk.scheduler.contracts import (
    ScheduleCommandTemplate,
    ScheduledTask,
    ScheduledTaskTarget,
    ScheduleOccurrence,
    ScheduleSpec,
)
from ksadk.scheduler.dispatcher import AgentControlSchedulerDispatcher
from ksadk.scheduler.engine import SchedulerDispatchError


def _scheduled_task() -> ScheduledTask:
    now = datetime.now(timezone.utc)
    return ScheduledTask(
        task_id="daily-report",
        target=ScheduledTaskTarget(
            tenant_id="local",
            agent_instance_id="agent-local",
            agent_version_ref="build_123",
            session_id="session-123",
            authorization_ref="credential://scheduler-local",
        ),
        schedule=ScheduleSpec(kind="once", at=now),
        command=ScheduleCommandTemplate(payload={"content": "write report"}),
        next_run_at=now,
    )


def _occurrence() -> ScheduleOccurrence:
    return ScheduleOccurrence(
        occurrence_id="occ_12345678",
        task_id="daily-report",
        scheduled_for=datetime.now(timezone.utc),
        session_id="sched-occ_12345678",
        state="claimed",
    )


def test_continue_session_requires_a_stable_session_target() -> None:
    payload = _scheduled_task().model_dump(mode="json", by_alias=True)
    payload["continuity"] = "continue_session"
    payload["target"]["sessionId"] = None
    with pytest.raises(ValidationError, match="requires target.sessionId"):
        ScheduledTask.model_validate(payload)


@pytest.mark.asyncio
async def test_dispatcher_uses_scheduler_source_and_occurrence_idempotency() -> None:
    captured = {}

    async def submit(command, permit):
        captured["command"] = command
        captured["permit"] = permit
        return AgentControlReceipt(
            command_id=command.command_id,
            status="accepted",
            message_id=uuid4(),
        )

    occurrence = _occurrence()
    command_id = await AgentControlSchedulerDispatcher(submit).dispatch(
        _scheduled_task(), occurrence
    )

    command = captured["command"]
    assert command_id == str(command.command_id)
    assert command.source.kind == "scheduler"
    assert command.source.ref == occurrence.occurrence_id
    assert command.correlation_id == occurrence.occurrence_id
    assert command.idempotency_key == occurrence.occurrence_id
    assert command.payload == {"content": "write report"}
    assert command.authorization_ref == captured["permit"].permit_id


@pytest.mark.asyncio
async def test_dispatcher_preserves_kernel_rejection_as_typed_failure() -> None:
    async def submit(command, _permit):
        return AgentControlReceipt(
            command_id=command.command_id,
            status="queue_full",
            error={"code": "QUEUE_FULL", "message": "full", "retryable": True},
        )

    with pytest.raises(SchedulerDispatchError, match="full") as captured:
        await AgentControlSchedulerDispatcher(submit).dispatch(_scheduled_task(), _occurrence())
    assert captured.value.code == "QUEUE_FULL"
