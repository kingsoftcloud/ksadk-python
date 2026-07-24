# -*- coding: utf-8 -*-
"""executor.cancel 必须尊重 CancelResult(goal-05 review 修复)。

§7.4:有 RuntimeAdapter 时,只有底层真取消(INTERRUPTED_ACTIVE_TURN /
PENDING_CANCEL_RECORDED)才把协议 Task 置 canceled;NOT_RUNNING/FAILED 必须抛
TaskNotCancelableError,不得伪造 canceled 终态。无 adapter 时同样拒绝。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from a2a.types import Task, TaskState, TaskStatus
from a2a.utils.errors import TaskNotCancelableError

from ksadk.a2a.executor import A2ARuntimeExecutor
from ksadk.a2a.task_adapter import A2ARuntimeTaskAdapter
from ksadk.events import EventType, RuntimeEvent
from ksadk.runtime import CancelResult, RunHandle


class _FakeEventQueue:
    def __init__(self) -> None:
        self.events: list = []

    async def enqueue_event(self, event) -> None:  # noqa: ANN001
        self.events.append(event)


class _FakeContext:
    def __init__(self, task_id: str = "t1", context_id: str = "c1") -> None:
        self.task_id = task_id
        self.context_id = context_id
        self.current_task: Any = None
        self.metadata: dict[str, Any] = {}
        self.call_context = SimpleNamespace(tenant="tenant-1")

    def get_user_input(self) -> str:
        return "hello"


class _StubTaskAdapter:
    """返回固定 CancelResult 的 task_adapter stub。"""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.calls: list[str] = []

    async def cancel_task(self, task_id: str, context) -> Any:  # noqa: ANN001
        self.calls.append(task_id)
        return self._result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "accepted",
    [CancelResult.INTERRUPTED_ACTIVE_TURN, CancelResult.PENDING_CANCEL_RECORDED],
)
async def test_cancel_marks_canceled_when_underlying_accepted(accepted: CancelResult) -> None:
    adapter = _StubTaskAdapter(accepted)
    executor = A2ARuntimeExecutor(runner=object(), task_adapter=adapter)
    queue = _FakeEventQueue()
    await executor.cancel(_FakeContext(), queue)  # type: ignore[arg-type]
    assert adapter.calls == ["t1"]
    # updater.cancel() 入队了一个 canceled 状态更新
    assert len(queue.events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rejected",
    [CancelResult.NOT_RUNNING, CancelResult.FAILED, "unsupported"],
)
async def test_cancel_rejected_when_underlying_not_cancelled(rejected: Any) -> None:
    adapter = _StubTaskAdapter(rejected)
    executor = A2ARuntimeExecutor(runner=object(), task_adapter=adapter)
    queue = _FakeEventQueue()
    with pytest.raises(TaskNotCancelableError):
        await executor.cancel(_FakeContext(), queue)  # type: ignore[arg-type]
    # 不得伪造 canceled:没有入队任何 canceled 状态
    assert queue.events == []


@pytest.mark.asyncio
async def test_cancel_without_adapter_is_rejected() -> None:
    executor = A2ARuntimeExecutor(runner=object(), task_adapter=None)
    queue = _FakeEventQueue()
    with pytest.raises(TaskNotCancelableError, match="runtime task adapter"):
        await executor.cancel(_FakeContext(), queue)  # type: ignore[arg-type]
    assert queue.events == []


class _CancelableRuntimeAdapter:
    def __init__(self, *, emit_canceled_event: bool = False) -> None:
        self.handle = RunHandle(
            run_id="runtime-run-1",
            session_id="c1",
            runtime_type="test",
            native_ref={"process_id": 4312, "thread_id": "thread-9"},
        )
        self.emit_canceled_event = emit_canceled_event
        self.stream_started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.cancel_handles: list[RunHandle] = []
        self.attach_handles: list[RunHandle] = []

    async def start(self, request: Any) -> RunHandle:
        return self.handle

    def stream(self, handle: RunHandle):  # noqa: ANN201
        async def _events():
            self.stream_started.set()
            await self.cancelled.wait()
            if self.emit_canceled_event:
                yield RuntimeEvent.create(
                    EventType.RUN_CANCELED,
                    agent_id="agent-1",
                    user_id="tenant-1",
                    session_id=handle.session_id,
                    invocation_id=handle.run_id,
                    seq_id=1,
                    payload={"status": "canceled"},
                )

        return _events()

    async def cancel(self, handle: RunHandle) -> CancelResult:
        self.cancel_handles.append(handle)
        self.cancelled.set()
        return CancelResult.INTERRUPTED_ACTIVE_TURN

    async def attach(self, handle: RunHandle) -> RunHandle:
        self.attach_handles.append(handle)
        return handle


def _states(queue: _FakeEventQueue) -> list[Any]:
    return [
        getattr(getattr(event, "status", None), "state", None)
        for event in queue.events
        if getattr(event, "status", None) is not None
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("emit_canceled_event", [False, True])
async def test_accepted_runtime_cancel_never_completes_execution(
    emit_canceled_event: bool,
) -> None:
    runtime = _CancelableRuntimeAdapter(emit_canceled_event=emit_canceled_event)
    task_adapter = A2ARuntimeTaskAdapter(runtime, runtime_type="test")  # type: ignore[arg-type]
    executor = A2ARuntimeExecutor(runner=object(), task_adapter=task_adapter)
    context = _FakeContext()
    execute_queue = _FakeEventQueue()
    cancel_queue = _FakeEventQueue()

    execute_task = asyncio.create_task(executor.execute(context, execute_queue))  # type: ignore[arg-type]
    await runtime.stream_started.wait()
    await executor.cancel(context, cancel_queue)  # type: ignore[arg-type]
    await execute_task

    assert runtime.cancel_handles == [runtime.handle]
    assert runtime.cancel_handles[0] is runtime.handle
    assert runtime.cancel_handles[0].native_ref == {
        "process_id": 4312,
        "thread_id": "thread-9",
    }
    assert TaskState.TASK_STATE_COMPLETED not in _states(execute_queue)
    assert _states(cancel_queue) == [TaskState.TASK_STATE_CANCELED]


@pytest.mark.asyncio
async def test_cancel_without_real_handle_does_not_call_runtime() -> None:
    runtime = _CancelableRuntimeAdapter()
    task_adapter = A2ARuntimeTaskAdapter(runtime, runtime_type="test")  # type: ignore[arg-type]
    executor = A2ARuntimeExecutor(runner=object(), task_adapter=task_adapter)
    queue = _FakeEventQueue()

    with pytest.raises(TaskNotCancelableError, match="not_running"):
        await executor.cancel(_FakeContext(), queue)  # type: ignore[arg-type]

    assert runtime.cancel_handles == []
    assert queue.events == []


@pytest.mark.asyncio
async def test_cancel_after_restart_uses_persisted_runtime_handle() -> None:
    runtime = _CancelableRuntimeAdapter()
    task_adapter = A2ARuntimeTaskAdapter(runtime, runtime_type="test")  # type: ignore[arg-type]
    executor = A2ARuntimeExecutor(runner=object(), task_adapter=task_adapter)
    context = _FakeContext()
    context.current_task = Task(
        id=context.task_id,
        context_id=context.context_id,
        status=TaskStatus(state=TaskState.TASK_STATE_INPUT_REQUIRED),
        metadata={"run_handle": runtime.handle.model_dump(mode="json")},
    )
    queue = _FakeEventQueue()

    await executor.cancel(context, queue)  # type: ignore[arg-type]

    assert runtime.attach_handles == [runtime.handle]
    assert runtime.cancel_handles == [runtime.handle]
    assert runtime.cancel_handles[0].native_ref == {
        "process_id": 4312,
        "thread_id": "thread-9",
    }
    assert _states(queue) == [TaskState.TASK_STATE_CANCELED]


@pytest.mark.asyncio
async def test_runtime_canceled_event_is_terminal_and_never_completed() -> None:
    class _NaturalCanceledRuntimeAdapter(_CancelableRuntimeAdapter):
        def stream(self, handle: RunHandle):  # noqa: ANN201
            async def _events():
                yield RuntimeEvent.create(
                    EventType.RUN_CANCELED,
                    agent_id="agent-1",
                    user_id="tenant-1",
                    session_id=handle.session_id,
                    invocation_id=handle.run_id,
                    seq_id=1,
                    payload={"status": "canceled"},
                )

            return _events()

    runtime = _NaturalCanceledRuntimeAdapter()
    task_adapter = A2ARuntimeTaskAdapter(runtime, runtime_type="test")  # type: ignore[arg-type]
    executor = A2ARuntimeExecutor(runner=object(), task_adapter=task_adapter)
    queue = _FakeEventQueue()

    await executor.execute(_FakeContext(), queue)  # type: ignore[arg-type]

    assert TaskState.TASK_STATE_CANCELED in _states(queue)
    assert TaskState.TASK_STATE_COMPLETED not in _states(queue)
