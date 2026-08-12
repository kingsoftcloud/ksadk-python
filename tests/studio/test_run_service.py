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
    PauseResult,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
    RuntimeExecutor,
    RuntimeLaunchContext,
    RuntimeRegistry,
    StartRequest,
)
from ksadk.studio.contracts import RunStatus
from ksadk.studio.run_service import (
    StudioRunService,
    StudioRunSpec,
    project_runtime_event,
)
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

    async def pause(self, handle: RunHandle) -> PauseResult:
        self.calls.append(("pause", handle))
        return PauseResult.PAUSED_ACTIVE_TURN

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
    assert starts[1].metadata["thread_id"] == "thread-1"


@pytest.mark.asyncio
async def test_structured_runtime_input_is_separate_from_persisted_display_text(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, Any]] = []
    registry = RuntimeRegistry()
    registry.register("codex", lambda _context: _RecordingAdapter(calls, "codex"))
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(registry))
    spec = StudioRunSpec(
        launch_context=RuntimeLaunchContext(runtime_type="codex", project_dir=tmp_path),
        build_id="build-codex",
        agent_id="review-helper",
    )
    structured = [
        {"type": "text", "text": "分析图片"},
        {"type": "image", "url": "data:image/png;base64,AAAA"},
    ]

    record = await service.run(
        spec,
        "分析图片",
        runtime_input=structured,
        session_id="ses-attachment",
    )

    request = next(value for name, value in calls if name == "start")
    assert record.input == "分析图片"
    assert request.input == structured


@pytest.mark.asyncio
async def test_run_persists_plan_and_goal_context_for_refresh_recovery(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, Any]] = []
    registry = RuntimeRegistry()
    registry.register("codex", lambda _context: _RecordingAdapter(calls, "codex"))
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(registry))

    record = await service.run(
        StudioRunSpec(
            launch_context=RuntimeLaunchContext(runtime_type="codex", project_dir=tmp_path),
            build_id="build-codex",
            agent_id="review-helper",
            request_config={
                "collaboration_mode": "plan",
                "goal_objective": "完成 Studio 交互重构",
            },
        ),
        "完成 Studio 交互重构",
        session_id="ses-goal",
    )

    assert record.collaboration_mode == "plan"
    assert record.goal_objective == "完成 Studio 交互重构"
    persisted = service.event_store.get(record.id)
    assert persisted.collaboration_mode == "plan"
    assert persisted.goal_objective == "完成 Studio 交互重构"


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
async def test_cancel_request_is_bounded_when_runtime_interrupt_stalls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ksadk.studio.run_service as run_service_module

    class _StalledCancelAdapter(_RecordingAdapter):
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

        async def cancel(self, handle: RunHandle) -> CancelResult:
            await asyncio.Event().wait()
            return CancelResult.INTERRUPTED_ACTIVE_TURN

    monkeypatch.setattr(run_service_module, "_CANCEL_TIMEOUT_SECONDS", 0.02)
    calls: list[tuple[str, Any]] = []
    registry = RuntimeRegistry()
    registry.register("codex", lambda _context: _StalledCancelAdapter(calls, "codex"))
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(registry))
    task = asyncio.create_task(
        service.run(
            StudioRunSpec(
                launch_context=RuntimeLaunchContext(runtime_type="codex", project_dir=tmp_path),
                build_id="build-codex",
                agent_id="review-helper",
            ),
            "等待输入",
            session_id="ses-stalled-cancel",
        )
    )
    await asyncio.sleep(0.02)
    run_id = service.event_store.list_runs(session_id="ses-stalled-cancel")[0].id

    result = await asyncio.wait_for(service.cancel_run(run_id), timeout=0.2)

    assert result == {"runId": run_id, "status": "cancelling"}
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


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


@pytest.mark.asyncio
async def test_pause_preserves_run_and_resume_continues_same_runtime_handle(
    tmp_path: Path,
) -> None:
    class _PausableAdapter(_RecordingAdapter):
        def __init__(self, calls: list[tuple[str, Any]]) -> None:
            super().__init__(calls, "codex")
            self.turn = 0
            self.interrupt = asyncio.Event()

        async def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
            self.turn += 1
            yield RuntimeEvent.create(
                EventType.RUN_STARTED,
                agent_id="review-helper",
                user_id="local-user",
                session_id=handle.session_id,
                invocation_id=handle.run_id,
                seq_id=self.turn * 10,
                payload={"status": "in_progress"},
            )
            if self.turn == 1:
                await self.interrupt.wait()
                yield RuntimeEvent.create(
                    EventType.RUN_INTERRUPTED,
                    agent_id="review-helper",
                    user_id="local-user",
                    session_id=handle.session_id,
                    invocation_id=handle.run_id,
                    seq_id=self.turn * 10 + 1,
                    payload={"status": "paused", "reason": "user_pause"},
                )
                return
            yield RuntimeEvent.create(
                EventType.TEXT_COMPLETED,
                agent_id="review-helper",
                user_id="local-user",
                session_id=handle.session_id,
                invocation_id=handle.run_id,
                seq_id=self.turn * 10 + 1,
                phase="final_answer",
                payload={"text": "resumed answer"},
            )
            yield RuntimeEvent.create(
                EventType.RUN_COMPLETED,
                agent_id="review-helper",
                user_id="local-user",
                session_id=handle.session_id,
                invocation_id=handle.run_id,
                seq_id=self.turn * 10 + 2,
                payload={"status": "completed"},
            )

        async def pause(self, handle: RunHandle) -> PauseResult:
            self.calls.append(("pause", handle))
            self.interrupt.set()
            return PauseResult.PAUSED_ACTIVE_TURN

        async def resume(self, handle, target, payload):
            self.calls.append(("resume", (handle, target, payload)))
            return handle

    calls: list[tuple[str, Any]] = []
    registry = RuntimeRegistry()
    registry.register("codex", lambda _context: _PausableAdapter(calls))
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(registry))
    task = asyncio.create_task(
        service.run(
            StudioRunSpec(
                launch_context=RuntimeLaunchContext(runtime_type="codex", project_dir=tmp_path),
                build_id="build-codex",
                agent_id="review-helper",
            ),
            "分析代码库",
            session_id="ses-pause",
        )
    )
    await asyncio.sleep(0.02)
    run = service.event_store.list_runs(session_id="ses-pause")[0]

    assert await service.pause_run(run.id) == {"runId": run.id, "status": "pausing"}
    for _ in range(20):
        if service.event_store.get(run.id).status == RunStatus.PAUSED:
            break
        await asyncio.sleep(0.01)
    assert service.event_store.get(run.id).status == RunStatus.PAUSED
    assert not task.done()

    assert await service.resume_run(run.id) == {"runId": run.id, "status": "resuming"}
    record = await asyncio.wait_for(task, timeout=2)
    assert record.status == RunStatus.COMPLETED
    assert record.output == "resumed answer"
    assert [event.type for event in service.event_store.events(record.id)].count("run.paused") == 1
    assert "run.resumed" in [event.type for event in service.event_store.events(record.id)]
    assert [name for name, _ in calls] == ["start", "pause", "resume", "close"]


@pytest.mark.asyncio
async def test_live_a2ui_interaction_submits_structured_answer_and_continues(
    tmp_path: Path,
) -> None:
    class _InteractiveAdapter(_RecordingAdapter):
        def __init__(self, calls: list[tuple[str, Any]]) -> None:
            super().__init__(calls, "codex")
            self.answered = asyncio.Event()

        async def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
            yield RuntimeEvent.create(
                EventType.A2UI_SURFACE_BEGIN,
                agent_id="review-helper",
                user_id="local-user",
                session_id=handle.session_id,
                invocation_id=handle.run_id,
                seq_id=1,
                payload={
                    "surface_id": "surface-1",
                    "surface": {
                        "components": [
                            {
                                "id": "scope",
                                "component": "CheckboxGroup",
                                "name": "scope",
                                "options": ["前端", "服务端"],
                            }
                        ]
                    },
                },
            )
            yield RuntimeEvent.create(
                EventType.A2UI_INTERACTION,
                agent_id="review-helper",
                user_id="local-user",
                session_id=handle.session_id,
                invocation_id=handle.run_id,
                seq_id=2,
                payload={
                    "surface_id": "surface-1",
                    "interaction_id": "question-1",
                    "kind": "form",
                    "input_schema": {},
                },
            )
            await self.answered.wait()
            yield RuntimeEvent.create(
                EventType.TEXT_COMPLETED,
                agent_id="review-helper",
                user_id="local-user",
                session_id=handle.session_id,
                invocation_id=handle.run_id,
                seq_id=3,
                phase="final_answer",
                payload={"text": "已按选择继续"},
            )
            yield RuntimeEvent.create(
                EventType.RUN_COMPLETED,
                agent_id="review-helper",
                user_id="local-user",
                session_id=handle.session_id,
                invocation_id=handle.run_id,
                seq_id=4,
                payload={"status": "completed"},
            )

        async def submit(self, handle: RunHandle, payload: ResumePayload) -> None:
            self.calls.append(("submit", payload))
            self.answered.set()

    calls: list[tuple[str, Any]] = []
    registry = RuntimeRegistry()
    registry.register("codex", lambda _context: _InteractiveAdapter(calls))
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(registry))
    task = asyncio.create_task(
        service.run(
            StudioRunSpec(
                launch_context=RuntimeLaunchContext(runtime_type="codex", project_dir=tmp_path),
                build_id="build-codex",
                agent_id="review-helper",
            ),
            "检查项目",
            session_id="ses-interaction",
        )
    )
    for _ in range(30):
        await asyncio.sleep(0.01)
        run = service.event_store.list_runs(session_id="ses-interaction")[0]
        if run.status == RunStatus.WAITING_INPUT:
            break
    assert run.status == RunStatus.WAITING_INPUT

    result = await service.submit_interaction(
        run.id,
        "question-1",
        name="submit",
        data={"scope": ["前端", "服务端"], "note": "忽略生成文件"},
    )
    assert result["status"] == "resolved"
    completed = await asyncio.wait_for(task, timeout=2)
    assert completed.status == RunStatus.COMPLETED
    assert completed.output == "已按选择继续"
    submitted = next(value for name, value in calls if name == "submit")
    assert submitted.kind == "hitl_answer"
    assert submitted.call_id == "question-1"
    assert submitted.data == {
        "decision": "submit",
        "scope": ["前端", "服务端"],
        "note": "忽略生成文件",
    }
    assert "a2ui.action" in service_event_types(workspace, run.id)


def test_a2ui_runtime_events_are_persisted_as_official_operations() -> None:
    event_type, payload = project_runtime_event(
        _runtime_event(
            EventType.A2UI_SURFACE_BEGIN,
            {
                "surface_id": "surface-1",
                "catalog_id": "catalog-1",
                "surface": {
                    "components": [
                        {"component_id": "root", "type": "Text", "props": {"text": "Hello"}}
                    ],
                    "data_model": {"ready": True},
                },
            },
        )
    )
    assert event_type == "a2ui.surface.begin"
    assert payload["surfaceId"] == "surface-1"
    assert [
        next(iter(operation.keys() - {"version"})) for operation in payload["a2uiOperations"]
    ] == [
        "createSurface",
        "updateComponents",
        "updateDataModel",
    ]


def service_event_types(workspace: Workspace, run_id: str) -> list[str]:
    return [
        event.type
        for event in StudioRunService(
            workspace,
            RuntimeExecutor(RuntimeRegistry()),
        ).event_store.events(run_id)
    ]


def _runtime_event(event_type: str, payload: dict[str, Any]) -> RuntimeEvent:
    return RuntimeEvent.create(
        event_type,
        agent_id="codex",
        user_id="u",
        session_id="s",
        invocation_id="run-1",
        seq_id=1,
        payload=payload,
    )


def test_generic_tool_events_project_normalized_fields() -> None:
    """MCP 等非命令工具事件必须投影出 tool/args/output,trace 层才能成 span。"""
    begin_type, begin_payload = project_runtime_event(
        _runtime_event(
            EventType.TOOL_CALL_BEGIN,
            {
                "call_id": "mcp-1",
                "name": "mcp.metaso-inner.metaso_web_search",
                "args": {
                    "server": "metaso-inner",
                    "tool": "metaso_web_search",
                    "arguments": {"q": "x"},
                },
            },
        )
    )
    assert begin_type == "tool.started"
    assert begin_payload["callId"] == "mcp-1"
    assert begin_payload["tool"] == "mcp.metaso-inner.metaso_web_search"
    assert begin_payload["args"]["arguments"] == {"q": "x"}
    assert "runtimeEvent" in begin_payload

    end_type, end_payload = project_runtime_event(
        _runtime_event(
            EventType.TOOL_CALL_END,
            {
                "call_id": "mcp-1",
                "name": "mcp.metaso-inner.metaso_web_search",
                "result": {"status": "completed", "duration_ms": 12, "output": "结果"},
            },
        )
    )
    assert end_type == "tool.completed"
    assert end_payload["callId"] == "mcp-1"
    assert end_payload["tool"] == "mcp.metaso-inner.metaso_web_search"
    assert end_payload["status"] == "completed"
    assert end_payload["durationMs"] == 12
    assert end_payload["output"] == "结果"


def test_generic_tool_error_marks_completed_event_failed() -> None:
    _, payload = project_runtime_event(
        _runtime_event(
            EventType.TOOL_CALL_END,
            {
                "call_id": "mcp-2",
                "name": "mcp.metaso-inner.metaso_web_search",
                "result": {"status": "failed", "error": "boom", "output": "boom"},
            },
        )
    )
    assert payload["status"] == "failed"
    assert payload["error"] == "boom"
