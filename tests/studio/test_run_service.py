from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

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
    RuntimeExecutor,
    RuntimeLaunchContext,
    RuntimeRegistry,
    StartRequest,
)
from ksadk.studio.run_service import StudioRunService, StudioRunSpec
from ksadk.studio.workspace import Workspace


class _FixtureRuntime(BaseRuntime):
    def __init__(self, runtime_type: str) -> None:
        self.runtime_type = runtime_type

    def native_capabilities(self) -> dict[str, Any]:
        return {"Framework": self.runtime_type}


class _RecordingAdapter(RuntimeAdapter):
    def __init__(
        self,
        calls: list[tuple[str, Any]],
        runtime_type: str = "langgraph",
    ) -> None:
        super().__init__(_FixtureRuntime(runtime_type))
        self.calls = calls
        self.runtime_type = runtime_type

    async def start(self, request: StartRequest) -> RunHandle:
        self.calls.append(("start", request))
        return RunHandle(
            run_id=f"native-{self.runtime_type}",
            session_id=request.session_id,
            runtime_type=self.runtime_type,
            native_ref={"thread_id": "thread-1"},
        )

    async def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        self.calls.append(("stream", handle))
        yield RuntimeEvent.create(
            EventType.RUN_STARTED,
            agent_id="review-helper",
            user_id="local-user",
            session_id=handle.session_id,
            invocation_id=handle.run_id,
            seq_id=1,
            payload={"status": "in_progress"},
        )
        yield RuntimeEvent.create(
            EventType.TEXT_COMPLETED,
            agent_id="review-helper",
            user_id="local-user",
            session_id=handle.session_id,
            invocation_id=handle.run_id,
            seq_id=2,
            phase="final_answer",
            payload={"text": f"{self.runtime_type} answer"},
        )
        yield RuntimeEvent.create(
            EventType.RUN_COMPLETED,
            agent_id="review-helper",
            user_id="local-user",
            session_id=handle.session_id,
            invocation_id=handle.run_id,
            seq_id=3,
            payload={"status": "completed", "duration_ms": 42},
        )

    async def cancel(self, handle: RunHandle) -> CancelResult:
        self.calls.append(("cancel", handle))
        return CancelResult.INTERRUPTED_ACTIVE_TURN

    async def resume(
        self,
        handle: RunHandle,
        target: ResumeTarget,
        payload: ResumePayload | None,
    ) -> RunHandle:
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
        self.calls.append(("close", handle))


@pytest.mark.asyncio
async def test_studio_run_service_uses_core_executor_and_persists_runtime_events(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, Any]] = []
    registry = RuntimeRegistry()
    registry.register("langgraph", lambda _context: _RecordingAdapter(calls))
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(registry))

    record = await service.run(
        StudioRunSpec(
            launch_context=RuntimeLaunchContext(
                runtime_type="langgraph",
                project_dir=tmp_path,
            ),
            build_id="build-lg",
            agent_id="review-helper",
            model="glm-5.2",
            request_config={"entrypoint": "agent:graph"},
        ),
        "review src/demo.py",
        session_id="ses-lg",
    )

    assert record.output == "langgraph answer"
    assert record.runtime_type == "langgraph"
    assert record.duration_ms == 42
    assert record.duration_source == "runtime"
    assert record.runtime_handle == {
        "run_id": "native-langgraph",
        "session_id": "ses-lg",
        "runtime_type": "langgraph",
        "native_ref": {"thread_id": "thread-1"},
    }
    assert [name for name, _value in calls] == ["start", "stream", "close"]
    request = calls[0][1]
    assert request.model == "glm-5.2"
    assert request.config == {"entrypoint": "agent:graph"}
    assert [event.type for event in service.event_store.events(record.id)] == [
        "run.created",
        "run.started",
        "message.completed",
        "run.completed",
    ]


@pytest.mark.asyncio
async def test_second_turn_receives_transport_neutral_session_history(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, Any]] = []
    registry = RuntimeRegistry()
    registry.register("codex", lambda _context: _RecordingAdapter(calls, "codex"))
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(registry))
    spec = StudioRunSpec(
        launch_context=RuntimeLaunchContext(
            runtime_type="codex",
            project_dir=tmp_path,
        ),
        build_id="build-codex",
        agent_id="review-helper",
    )

    await service.run(spec, "第一轮", session_id="ses-codex")
    await service.run(spec, "第二轮", session_id="ses-codex")

    starts = [value for name, value in calls if name == "start"]
    conversation = starts[1].conversation_preprocessing()
    assert conversation is not None
    assert conversation.messages == [
        {"role": "user", "content": "第一轮"},
        {"role": "assistant", "content": "codex answer"},
        {"role": "user", "content": "第二轮"},
    ]


@pytest.mark.asyncio
async def test_runtime_reported_usage_and_duration_are_authoritative(
    tmp_path: Path,
) -> None:
    class _MetricsAdapter(_RecordingAdapter):
        async def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
            yield RuntimeEvent.create(
                EventType.USAGE_REPORTED,
                agent_id="review-helper",
                user_id="local-user",
                session_id=handle.session_id,
                invocation_id=handle.run_id,
                seq_id=1,
                payload={
                    "input_tokens": 128,
                    "cached_tokens": 16,
                    "output_tokens": 32,
                    "reasoning_tokens": 8,
                    "total_tokens": 160,
                    "source": "codex",
                },
            )
            yield RuntimeEvent.create(
                EventType.RUN_COMPLETED,
                agent_id="review-helper",
                user_id="local-user",
                session_id=handle.session_id,
                invocation_id=handle.run_id,
                seq_id=2,
                payload={"status": "completed", "duration_ms": 1340},
            )

    registry = RuntimeRegistry()
    registry.register("codex", lambda _context: _MetricsAdapter([], "codex"))
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(registry))

    record = await service.run(
        StudioRunSpec(
            launch_context=RuntimeLaunchContext(
                runtime_type="codex",
                project_dir=tmp_path,
            ),
            build_id="build-codex",
            agent_id="review-helper",
            model="glm-5.2",
        ),
        "review",
        session_id="ses-metrics",
    )

    assert record.usage.model_dump(by_alias=True) == {
        "inputTokens": 128,
        "outputTokens": 32,
        "totalTokens": 160,
        "cachedInputTokens": 16,
        "reasoningOutputTokens": 8,
        "reported": True,
        "source": "codex",
    }
    assert record.duration_ms == 1340
    assert record.duration_source == "runtime"


@pytest.mark.asyncio
async def test_explicit_operation_cancellation_calls_executor_cancel_and_persists_terminal(
    tmp_path: Path,
) -> None:
    class _BlockingAdapter(_RecordingAdapter):
        async def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
            yield RuntimeEvent.create(
                EventType.RUN_STARTED,
                agent_id="review-helper",
                user_id="local-user",
                session_id=handle.session_id,
                invocation_id=handle.run_id,
                seq_id=1,
                payload={"status": "in_progress"},
            )
            await asyncio.Event().wait()

    calls: list[tuple[str, Any]] = []
    registry = RuntimeRegistry()
    registry.register("codex", lambda _context: _BlockingAdapter(calls, "codex"))
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(registry))
    task = asyncio.create_task(
        service.run(
            StudioRunSpec(
                launch_context=RuntimeLaunchContext(
                    runtime_type="codex",
                    project_dir=tmp_path,
                ),
                build_id="build-codex",
                agent_id="review-helper",
            ),
            "分析代码库",
            session_id="ses-cancel",
        )
    )
    await asyncio.sleep(0.02)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record = service.event_store.list_runs(session_id="ses-cancel")[0]
    assert record.status == "CANCELLED"
    assert record.completed_at is not None
    assert service.event_store.events(record.id)[-1].type == "run.cancelled"
    assert [name for name, _value in calls][-2:] == ["cancel", "close"]


@pytest.mark.asyncio
async def test_runtime_interruption_is_not_reported_as_user_cancellation(
    tmp_path: Path,
) -> None:
    class _InterruptedAdapter(_RecordingAdapter):
        async def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
            yield RuntimeEvent.create(
                EventType.RUN_INTERRUPTED,
                agent_id="review-helper",
                user_id="local-user",
                session_id=handle.session_id,
                invocation_id=handle.run_id,
                seq_id=1,
                payload={"status": "attach_unavailable"},
            )

    calls: list[tuple[str, Any]] = []
    registry = RuntimeRegistry()
    registry.register("codex", lambda _context: _InterruptedAdapter(calls, "codex"))
    workspace = Workspace(tmp_path)
    workspace.initialize()

    record = await StudioRunService(workspace, RuntimeExecutor(registry)).run(
        StudioRunSpec(
            launch_context=RuntimeLaunchContext(
                runtime_type="codex",
                project_dir=tmp_path,
            ),
            build_id="build-codex",
            agent_id="review-helper",
        ),
        "继续运行",
        session_id="ses-interrupted",
    )

    assert record.status == "INTERRUPTED"
    assert record.error == {
        "code": "RUN_INTERRUPTED",
        "message": "attach_unavailable",
    }
    assert service_event_types(workspace, record.id)[-1] == "run.interrupted"


def service_event_types(workspace: Workspace, run_id: str) -> list[str]:
    return [event.type for event in StudioRunService(
        workspace,
        RuntimeExecutor(RuntimeRegistry()),
    ).event_store.events(run_id)]
