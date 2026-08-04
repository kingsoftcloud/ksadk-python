"""RuntimeExecutor 生命周期和 Handle 所有权合同。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

import ksadk.runtime as runtime_api
from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.runtime import (
    BaseRuntime,
    CancelResult,
    CheckpointCapability,
    CheckpointDescriptor,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
    RuntimeLaunchContext,
    RuntimeRegistry,
    StartRequest,
)


class _ExecutorRuntime(BaseRuntime):
    runtime_type = "fixture"

    def native_capabilities(self) -> dict[str, object]:
        return {}


class _RecordingAdapter(RuntimeAdapter):
    def __init__(self, *, returned_runtime_type: str = "fixture") -> None:
        super().__init__(_ExecutorRuntime())
        self.returned_runtime_type = returned_runtime_type
        self.cancelled: list[RunHandle] = []
        self.closed: list[RunHandle] = []
        self.attached: list[RunHandle] = []
        self.resume_handle: RunHandle | None = None

    async def start(self, request: StartRequest) -> RunHandle:
        return RunHandle(
            run_id=str(request.metadata.get("run_id") or "run-1"),
            session_id=request.session_id,
            runtime_type=self.returned_runtime_type,
        )

    async def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        yield RuntimeEvent.create(
            EventType.RUN_STARTED,
            agent_id="agent",
            user_id="user",
            session_id=handle.session_id,
            invocation_id=handle.run_id,
            seq_id=1,
            payload={"status": "in_progress"},
        )

    async def cancel(self, handle: RunHandle) -> CancelResult:
        self.cancelled.append(handle)
        return CancelResult.NOT_RUNNING

    async def resume(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: ResumePayload | None,
    ) -> RunHandle:
        return self.resume_handle or handle

    async def attach(self, handle: RunHandle) -> RunHandle:
        self.attached.append(handle)
        return handle

    async def checkpoint(self, handle: RunHandle) -> CheckpointDescriptor:
        return CheckpointDescriptor(
            checkpoint_id="checkpoint-1",
            invocation_id=handle.run_id,
            capability=CheckpointCapability(
                supported=False,
                granularity="none",
                rollback_scope="none",
                fork_supported=False,
                durable=False,
                shared_across_pods=False,
            ),
        )

    async def close(self, handle: RunHandle) -> None:
        self.closed.append(handle)


def _context(tmp_path: Path) -> RuntimeLaunchContext:
    return RuntimeLaunchContext(runtime_type="fixture", project_dir=tmp_path)


def _request(session_id: str = "session-1", *, run_id: str = "run-1") -> StartRequest:
    return StartRequest(
        input="hello",
        user_id="user",
        session_id=session_id,
        metadata={"run_id": run_id},
    )


def _registry(adapters: list[_RecordingAdapter]) -> RuntimeRegistry:
    registry = RuntimeRegistry()

    def factory(_context: RuntimeLaunchContext) -> RuntimeAdapter:
        adapter = _RecordingAdapter()
        adapters.append(adapter)
        return adapter

    registry.register("fixture", factory)
    return registry


@pytest.mark.asyncio
async def test_executor_routes_handle_to_owning_adapter_and_closes_it(tmp_path: Path) -> None:
    """防止 stream/cancel/close 被路由到另一个 Runtime 实例。"""

    adapters: list[_RecordingAdapter] = []
    executor = runtime_api.RuntimeExecutor(_registry(adapters))

    handle = await executor.start(_context(tmp_path), _request())
    events = [event async for event in executor.stream(handle)]
    result = await executor.cancel(handle)
    await executor.close(handle)

    assert [event.event_type for event in events] == [EventType.RUN_STARTED]
    assert result is CancelResult.NOT_RUNNING
    assert adapters[0].cancelled == [handle]
    assert adapters[0].closed == [handle]
    assert executor.is_attached(handle) is False


@pytest.mark.asyncio
async def test_executor_rejects_unknown_or_spoofed_handle(tmp_path: Path) -> None:
    """防止仅凭客户端提交的 Handle 操作不存在的 Runtime。"""

    executor = runtime_api.RuntimeExecutor(_registry([]))
    handle = RunHandle(
        run_id="forged",
        session_id="session-1",
        runtime_type="fixture",
    )

    with pytest.raises(KeyError, match="not attached"):
        await executor.cancel(handle)


@pytest.mark.asyncio
async def test_executor_rejects_adapter_returning_wrong_runtime_type(tmp_path: Path) -> None:
    """防止错误 Factory 把 Handle 注册到另一个 Runtime 类型。"""

    adapters: list[_RecordingAdapter] = []
    registry = RuntimeRegistry()

    def factory(_context: RuntimeLaunchContext) -> RuntimeAdapter:
        adapter = _RecordingAdapter(returned_runtime_type="other")
        adapters.append(adapter)
        return adapter

    registry.register("fixture", factory)
    executor = runtime_api.RuntimeExecutor(registry)

    with pytest.raises(ValueError, match="runtime type"):
        await executor.start(_context(tmp_path), _request())

    assert len(adapters[0].closed) == 1


@pytest.mark.asyncio
async def test_executor_attach_requires_adapter_restore_and_records_owner(tmp_path: Path) -> None:
    """防止刷新后把反序列化 Handle 误当成仍在运行的进程内对象。"""

    adapters: list[_RecordingAdapter] = []
    executor = runtime_api.RuntimeExecutor(_registry(adapters))
    persisted = RunHandle(
        run_id="persisted-run",
        session_id="session-1",
        runtime_type="fixture",
    )

    restored = await executor.attach(_context(tmp_path), persisted)

    assert restored == persisted
    assert adapters[0].attached == [persisted]
    assert executor.is_attached(persisted) is True


@pytest.mark.asyncio
async def test_executor_moves_ownership_when_resume_returns_new_handle(tmp_path: Path) -> None:
    """防止 Runtime 原生恢复生成新 run id 后仍只能操作旧 Handle。"""

    adapters: list[_RecordingAdapter] = []
    executor = runtime_api.RuntimeExecutor(_registry(adapters))
    handle = await executor.start(_context(tmp_path), _request())
    resumed = handle.model_copy(update={"run_id": "run-2"})
    adapters[0].resume_handle = resumed

    result = await executor.resume(
        handle,
        ResumeTarget(kind="invocation_id", id=handle.run_id),
        None,
    )

    assert result == resumed
    assert executor.is_attached(handle) is False
    assert executor.is_attached(resumed) is True


@pytest.mark.asyncio
async def test_same_runtime_run_id_is_isolated_by_session(tmp_path: Path) -> None:
    """防止两个 Session 使用相同底层 run id 时互相覆盖所有权。"""

    adapters: list[_RecordingAdapter] = []
    executor = runtime_api.RuntimeExecutor(_registry(adapters))
    first = await executor.start(_context(tmp_path), _request("session-1"))
    second = await executor.start(_context(tmp_path), _request("session-2"))

    await executor.close(first)

    assert executor.is_attached(first) is False
    assert executor.is_attached(second) is True
    assert adapters[0].closed == [first]
    assert adapters[1].closed == []


@pytest.mark.asyncio
async def test_duplicate_handle_does_not_replace_owner_and_closes_new_adapter(
    tmp_path: Path,
) -> None:
    """防止重复 run id 覆盖仍在运行的 Adapter 并泄漏新建资源。"""

    adapters: list[_RecordingAdapter] = []
    executor = runtime_api.RuntimeExecutor(_registry(adapters))
    first = await executor.start(_context(tmp_path), _request())

    with pytest.raises(RuntimeError, match="already attached"):
        await executor.start(_context(tmp_path), _request())

    assert executor.is_attached(first) is True
    assert adapters[0].closed == []
    assert len(adapters[1].closed) == 1
