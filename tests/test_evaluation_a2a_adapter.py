import asyncio
from types import SimpleNamespace

import pytest
from a2a.types import SendMessageRequest, TaskState

from ksadk.evaluation import A2ATargetAdapter, TargetRunStatus


def _response(*, task_id: str, state: TaskState):
    task = SimpleNamespace(
        id=task_id,
        context_id="context-1",
        status=SimpleNamespace(state=state, message=None),
        artifacts=[],
    )
    return SimpleNamespace(
        task=task,
        status_update=None,
        artifact_update=None,
        message=None,
    )


class _FailedClient:
    async def send_message(self, _request: SendMessageRequest):
        yield _response(task_id="task-failed", state=TaskState.TASK_STATE_FAILED)


class _CancellableClient:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled: list[str] = []

    async def send_message(self, _request: SendMessageRequest):
        self.started.set()
        yield _response(task_id="task-cancelled", state=TaskState.TASK_STATE_WORKING)
        await asyncio.Future()

    async def cancel_task(self, request):
        self.cancelled.append(request.id)


@pytest.mark.asyncio
async def test_failed_task_is_normalized():
    result = await A2ATargetAdapter(timeout_seconds=5)._run_turn(
        _FailedClient(), "hello", None
    )

    assert result.status is TargetRunStatus.FAILED
    assert result.error_code == "A2A_TASK_FAILED"


@pytest.mark.asyncio
async def test_cancelled_turn_requests_remote_task_cancel():
    client = _CancellableClient()
    task = asyncio.create_task(
        A2ATargetAdapter(timeout_seconds=5)._run_turn(client, "hello", None)
    )
    await client.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.cancelled == ["task-cancelled"]
