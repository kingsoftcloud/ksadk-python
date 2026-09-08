from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ksadk.events.canonical import (
    ApprovalRequest,
    ContentSnapshot,
    InteractionRequested,
    ItemCompleted,
    ItemStarted,
    ItemUpdated,
    OutputRef,
    RunCompleted,
    RunFailed,
    RunInterrupted,
    RunStarted,
    RuntimeEvent,
    SourceRef,
    StructuredInputRequest,
    UsageReported,
)
from ksadk.events.content import (
    DataContent,
    TextContent,
    ToolCallContent,
    ToolResultContent,
)
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
from ksadk.studio.errors import StudioError
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
        common = {
            "schema_version": 2,
            "timestamp": 1.0,
            "run_id": handle.run_id,
            "scope_id": f"scope-{handle.run_id}",
        }
        source = SourceRef(framework=self.runtime_type)
        yield RunStarted(
            event_id="e1",
            seq=1,
            status="running",
            source=source,
            **common,
        )
        yield ItemCompleted(
            event_id="e2",
            seq=2,
            item_id="msg-1",
            item_kind="message",
            snapshot=ContentSnapshot(
                parts=(
                    TextContent(
                        part_id="text-0",
                        text=f"{self.runtime_type} answer",
                    ),
                )
            ),
            source=source,
            **common,
        )
        yield RunCompleted(
            event_id="e3",
            seq=3,
            status="completed",
            output_refs=(
                OutputRef(
                    scope_id=common["scope_id"],
                    item_id="msg-1",
                    part_id="text-0",
                ),
            ),
            source=SourceRef(
                framework=self.runtime_type,
                metadata={"duration_ms": 42},
            ),
            **common,
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


class _MultiMessageAdapter(_RecordingAdapter):
    async def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        common = {
            "schema_version": 2,
            "timestamp": 1.0,
            "run_id": handle.run_id,
            "scope_id": f"scope-{handle.run_id}",
        }
        source = SourceRef(framework=self.runtime_type)
        yield RunStarted(event_id="e1", seq=1, status="running", source=source, **common)
        for seq, item_id in ((2, "msg-a"), (3, "msg-b")):
            yield ItemCompleted(
                event_id=f"e{seq}",
                seq=seq,
                item_id=item_id,
                item_kind="message",
                snapshot=ContentSnapshot(
                    parts=(TextContent(part_id=f"{item_id}-text", text="same"),)
                ),
                source=source,
                **common,
            )
        yield RunCompleted(
            event_id="e4",
            seq=4,
            status="completed",
            output_refs=(
                OutputRef(
                    scope_id=common["scope_id"],
                    item_id="msg-a",
                    part_id="msg-a-text",
                ),
                OutputRef(
                    scope_id=common["scope_id"],
                    item_id="msg-b",
                    part_id="msg-b-text",
                ),
            ),
            source=source,
            **common,
        )


class _GatedStreamingAdapter(_RecordingAdapter):
    def __init__(self, calls: list[tuple[str, Any]]) -> None:
        super().__init__(calls, "plugin")
        self.delta_consumed = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        common = {
            "schema_version": 2,
            "timestamp": 1.0,
            "run_id": handle.run_id,
            "scope_id": f"scope-{handle.run_id}",
        }
        source = SourceRef(framework="codex")
        yield RunStarted(event_id="e1", seq=1, status="running", source=source, **common)
        yield ItemStarted(
            event_id="e2",
            seq=2,
            item_id="msg-1",
            item_kind="message",
            phase="final_answer",
            source=source,
            **common,
        )
        yield ItemUpdated(
            event_id="e3",
            seq=3,
            item_id="msg-1",
            item_kind="message",
            op="append",
            update=TextContent(part_id="text-0", text="first chunk"),
            source=source,
            **common,
        )
        self.delta_consumed.set()
        await self.release.wait()
        yield ItemCompleted(
            event_id="e4",
            seq=4,
            item_id="msg-1",
            item_kind="message",
            snapshot=ContentSnapshot(
                parts=(TextContent(part_id="text-0", text="first chunk done"),)
            ),
            source=source,
            **common,
        )
        yield RunCompleted(
            event_id="e5",
            seq=5,
            status="completed",
            output_refs=(
                OutputRef(
                    scope_id=common["scope_id"],
                    item_id="msg-1",
                    part_id="text-0",
                ),
            ),
            source=source,
            **common,
        )


def test_kernel_route_is_used_only_for_its_bound_studio_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An enabled Kernel must not silently execute a different Studio Build."""

    from ksadk.kernel import bootstrap as kernel_bootstrap
    from ksadk.kernel import ingress as kernel_ingress

    active_context = RuntimeLaunchContext(runtime_type="codex", project_dir=tmp_path)
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            launch_context=active_context,
            agent_instance_id="instance-codex",
            start_request_defaults={"agent_id": "sales-helper"},
        )
    )
    monkeypatch.setattr(kernel_ingress, "kernel_route_active", lambda: True)
    monkeypatch.setattr(kernel_bootstrap, "get_agent_kernel_runtime", lambda: runtime)

    matching = StudioRunSpec(
        launch_context=RuntimeLaunchContext(runtime_type="codex", project_dir=tmp_path),
        build_id="build-codex",
        agent_id="sales-helper",
    )
    assert StudioRunService._kernel_runtime_for_spec(matching) is runtime

    other_agent = StudioRunSpec(
        launch_context=RuntimeLaunchContext(runtime_type="codex", project_dir=tmp_path),
        build_id="build-other",
        agent_id="research-helper",
    )
    assert StudioRunService._kernel_runtime_for_spec(other_agent) is None

    other_runtime = StudioRunSpec(
        launch_context=RuntimeLaunchContext(runtime_type="langgraph", project_dir=tmp_path),
        build_id="build-graph",
        agent_id="sales-helper",
    )
    assert StudioRunService._kernel_runtime_for_spec(other_runtime) is None


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
    canonical = await service.runtime_events.list("ses-lg")
    assert [event.event_type for event in canonical] == [
        "run.started",
        "item.completed",
        "run.completed",
    ]
    assert [event.seq for event in canonical] == [1, 2, 3]
    assert {event.run_id for event in canonical} == {"native-langgraph"}


@pytest.mark.asyncio
async def test_plugin_provider_runtime_adapter_publishes_deltas_before_completion(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, Any]] = []
    adapter = _GatedStreamingAdapter(calls)

    class PluginRuntime:
        def kernel_adapter_provider(self, _spec: StudioRunSpec):
            return lambda: adapter

        async def execute(self, *_args: Any, **_kwargs: Any):
            raise AssertionError("runtime-adapter providers must not use buffered execute()")

    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(
        workspace,
        RuntimeExecutor(RuntimeRegistry()),
        plugin_runtime=PluginRuntime(),
    )
    observed: list[str] = []
    task = asyncio.create_task(
        service.run(
            StudioRunSpec(
                launch_context=RuntimeLaunchContext(
                    runtime_type="plugin",
                    project_dir=tmp_path,
                ),
                build_id="build-plugin-codex",
                agent_id="plugin-codex-agent",
                model="fixture-model",
                request_config={"provider_runtime_adapter": True},
                plugin_bundle_root=tmp_path,
            ),
            "stream this",
            session_id="ses-plugin-stream",
            on_event=lambda event: observed.append(event.type),
        )
    )

    await asyncio.wait_for(adapter.delta_consumed.wait(), timeout=1)
    assert "message.delta" in observed
    assert not task.done()
    adapter.release.set()
    record = await asyncio.wait_for(task, timeout=1)

    assert record.status == RunStatus.COMPLETED
    assert record.output == "first chunk done"


@pytest.mark.asyncio
async def test_terminal_output_preserves_identity_distinct_message_items(
    tmp_path: Path,
) -> None:
    registry = RuntimeRegistry()
    registry.register("codex", lambda _context: _MultiMessageAdapter([], "codex"))
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(registry))

    record = await service.run(
        StudioRunSpec(
            launch_context=RuntimeLaunchContext(
                runtime_type="codex",
                project_dir=tmp_path,
            ),
            build_id="build-multi-message",
            agent_id="multi-message-agent",
        ),
        "keep both outputs",
    )

    assert record.output == "same\n\nsame"


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
@pytest.mark.parametrize("stale_resume", [False, True])
async def test_codex_build_change_starts_fresh_native_thread_with_session_history(
    tmp_path: Path,
    stale_resume: bool,
) -> None:
    calls: list[tuple[str, Any]] = []
    registry = RuntimeRegistry()
    registry.register("codex", lambda _context: _RecordingAdapter(calls, "codex"))
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(registry))
    for build_id, prompt in [("before-plugin", "第一轮"), ("with-plugin", "使用 Figma")]:
        if build_id == "with-plugin" and stale_resume:
            # Old Studio versions persisted the old thread under a new build
            # even when its rollout could not be resumed there.
            old = service.event_store.list_runs(session_id="ses-plugin-update")[0]
            failed = old.model_copy(
                update={
                    "id": "run_failed_migration",
                    "build_id": build_id,
                    "status": RunStatus.FAILED,
                    "started_at": datetime.now(timezone.utc),
                }
            )
            service.event_store.create(failed)
        await service.run(
            StudioRunSpec(
                launch_context=RuntimeLaunchContext(runtime_type="codex", project_dir=tmp_path),
                build_id=build_id,
                agent_id="review-helper",
            ),
            prompt,
            session_id="ses-plugin-update",
        )

    starts = [value for name, value in calls if name == "start"]
    # Build homes contain distinct plugin snapshots and native rollout stores.
    assert "thread_id" not in starts[1].metadata
    conversation = starts[1].conversation_preprocessing()
    assert conversation is not None
    assert conversation.messages == [
        {"role": "user", "content": "第一轮"},
        {"role": "assistant", "content": "codex answer"},
        {"role": "user", "content": "使用 Figma"},
    ]


@pytest.mark.asyncio
async def test_codex_feedback_turn_reuses_native_thread_after_cancelled_approval(
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

    cancelled = await service.run(spec, "执行原命令", session_id="ses-feedback")
    cancelled.status = RunStatus.CANCELLED
    service.event_store.save(cancelled)
    await service.run(spec, "请改成 echo 你好", session_id="ses-feedback")

    starts = [value for name, value in calls if name == "start"]
    assert starts[1].metadata["thread_id"] == "thread-1"
    conversation = starts[1].conversation_preprocessing()
    assert conversation is not None
    assert conversation.messages == [{"role": "user", "content": "请改成 echo 你好"}]


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
            common = {
                "schema_version": 2,
                "timestamp": 1.0,
                "run_id": handle.run_id,
                "scope_id": f"scope-{handle.run_id}",
            }
            source = SourceRef(framework="codex")
            yield UsageReported(
                event_id="e1",
                seq=1,
                input_tokens=128,
                cached_tokens=16,
                output_tokens=32,
                reasoning_tokens=8,
                total_tokens=160,
                source=source,
                **common,
            )
            yield RunCompleted(
                event_id="e2",
                seq=2,
                status="completed",
                output_refs=(),
                source=SourceRef(
                    framework="codex",
                    metadata={"duration_ms": 1340},
                ),
                **common,
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
            yield RunStarted(
                event_id="e1",
                seq=1,
                status="running",
                schema_version=2,
                timestamp=1.0,
                run_id=handle.run_id,
                scope_id=f"scope-{handle.run_id}",
                source=SourceRef(framework="codex"),
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
            yield RunStarted(
                event_id="e1",
                seq=1,
                status="running",
                schema_version=2,
                timestamp=1.0,
                run_id=handle.run_id,
                scope_id=f"scope-{handle.run_id}",
                source=SourceRef(framework="codex"),
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
            yield RunInterrupted(
                event_id="e1",
                seq=1,
                status="interrupted",
                reason="attach_unavailable",
                schema_version=2,
                timestamp=1.0,
                run_id=handle.run_id,
                scope_id=f"scope-{handle.run_id}",
                source=SourceRef(framework="codex"),
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
            common = {
                "schema_version": 2,
                "timestamp": 1.0,
                "run_id": handle.run_id,
                "scope_id": f"scope-{handle.run_id}",
            }
            source = SourceRef(framework="codex")
            yield RunStarted(
                event_id=f"e{self.turn}0",
                seq=self.turn * 10,
                status="running",
                source=source,
                **common,
            )
            if self.turn == 1:
                await self.interrupt.wait()
                yield RunInterrupted(
                    event_id=f"e{self.turn}1",
                    seq=self.turn * 10 + 1,
                    status="interrupted",
                    reason="user_pause",
                    source=source,
                    **common,
                )
                return
            yield ItemCompleted(
                event_id=f"e{self.turn}1",
                seq=self.turn * 10 + 1,
                item_id="msg-1",
                item_kind="message",
                snapshot=ContentSnapshot(
                    parts=(
                        TextContent(
                            part_id="text-0",
                            text="resumed answer",
                        ),
                    )
                ),
                source=source,
                **common,
            )
            yield RunCompleted(
                event_id=f"e{self.turn}2",
                seq=self.turn * 10 + 2,
                status="completed",
                output_refs=(
                    OutputRef(
                        scope_id=common["scope_id"],
                        item_id="msg-1",
                        part_id="text-0",
                    ),
                ),
                source=source,
                **common,
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
            common = {
                "schema_version": 2,
                "timestamp": 1.0,
                "run_id": handle.run_id,
                "scope_id": f"scope-{handle.run_id}",
            }
            source = SourceRef(framework="codex")
            yield ItemStarted(
                event_id="e1",
                seq=1,
                item_id="surface-1",
                item_kind="data",
                initial=ContentSnapshot(
                    parts=(
                        DataContent(
                            part_id="data-0",
                            data={
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
                        ),
                    )
                ),
                source=SourceRef(
                    framework="codex",
                    protocol="a2ui",
                    metadata={"surface_id": "surface-1"},
                ),
                **common,
            )
            yield InteractionRequested(
                event_id="e2",
                seq=2,
                interaction_id="question-1",
                interaction_kind="structured_input",
                request=StructuredInputRequest(
                    prompt="检查范围",
                    schema={
                        "type": "object",
                        "properties": {"scope": {"type": "array", "minItems": 1}},
                        "required": ["scope"],
                    },
                ),
                source=source,
                **common,
            )
            await self.answered.wait()
            yield ItemCompleted(
                event_id="e3",
                seq=3,
                item_id="msg-1",
                item_kind="message",
                snapshot=ContentSnapshot(
                    parts=(
                        TextContent(
                            part_id="text-0",
                            text="已按选择继续",
                        ),
                    )
                ),
                source=source,
                **common,
            )
            yield RunCompleted(
                event_id="e4",
                seq=4,
                status="completed",
                output_refs=(
                    OutputRef(
                        scope_id=common["scope_id"],
                        item_id="msg-1",
                        part_id="text-0",
                    ),
                ),
                source=source,
                **common,
            )

        async def submit(self, handle: RunHandle, payload: ResumePayload) -> None:
            self.calls.append(("submit", payload))
            if payload.data.get("note") == "provider-failure":
                raise RuntimeError("provider rejected interaction")
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

    with pytest.raises(StudioError) as stale:
        await service.submit_interaction(
            run.id,
            "question-1",
            name="submit",
            data={},
            expected_revision=2,
            idempotency_key="interaction:question-1:stale",
        )
    assert stale.value.code == "INTERACTION_REVISION_MISMATCH"

    with pytest.raises(StudioError) as empty_answer:
        await service.submit_interaction(
            run.id,
            "question-1",
            name="submit",
            data={},
            expected_revision=1,
            idempotency_key="empty-answer",
        )
    assert empty_answer.value.code == "INTERACTION_RESPONSE_INVALID"
    assert service.event_store.get(run.id).status == RunStatus.WAITING_INPUT

    with pytest.raises(StudioError) as provider_failure:
        await service.submit_interaction(
            run.id,
            "question-1",
            name="submit",
            data={"scope": ["前端"], "note": "provider-failure"},
            expected_revision=1,
            idempotency_key="interaction:question-1:provider-failure",
        )
    assert provider_failure.value.code == "INTERACTION_SUBMIT_FAILED"
    assert not any(event.type == "a2ui.action" for event in service.event_store.events(run.id))

    request = {
        "name": "submit",
        "data": {"scope": ["前端", "服务端"], "note": "忽略生成文件"},
        "expected_revision": 1,
        "idempotency_key": "interaction:question-1:revision-1",
    }
    first, replay = await asyncio.gather(
        service.submit_interaction(run.id, "question-1", **request),
        service.submit_interaction(run.id, "question-1", **request),
    )
    assert first == replay
    assert first["status"] == "resolved"
    assert first["revision"] == 2

    with pytest.raises(StudioError) as replay_conflict:
        await service.submit_interaction(
            run.id,
            "question-1",
            name="submit",
            data={"scope": ["后端"]},
            expected_revision=1,
            idempotency_key="interaction:question-1:revision-1",
        )
    assert replay_conflict.value.code == "INTERACTION_IDEMPOTENCY_CONFLICT"

    with pytest.raises(StudioError) as already_resolved:
        await service.submit_interaction(
            run.id,
            "question-1",
            name="submit",
            data=request["data"],
            expected_revision=1,
            idempotency_key="interaction:question-1:other-attempt",
        )
    assert already_resolved.value.code == "INTERACTION_ALREADY_RESOLVED"
    completed = await asyncio.wait_for(task, timeout=2)
    assert completed.status == RunStatus.COMPLETED
    assert completed.output == "已按选择继续"
    submitted = [value for name, value in calls if name == "submit"][-1]
    assert submitted.kind == "hitl_answer"
    assert submitted.call_id == "question-1"
    assert submitted.data == {
        "decision": "submit",
        "scope": ["前端", "服务端"],
        "note": "忽略生成文件",
    }
    assert [name for name, _ in calls].count("submit") == 2
    assert "a2ui.action" in service_event_types(workspace, run.id)


@pytest.mark.parametrize(
    ("action", "response", "expected_decision", "expected_outcome", "expected_summary"),
    [
        ("approve", {}, "approve", "approved", "已同意"),
        ("reject", {"decision": "reject"}, "deny", "rejected", "已拒绝"),
        (
            "cancel",
            {"feedback": "请改成 echo 你好"},
            "cancel",
            "cancelled",
            "已反馈给 Agent",
        ),
    ],
)
@pytest.mark.asyncio
async def test_live_approval_submits_with_native_provider_call_id(
    tmp_path: Path,
    action: str,
    response: dict[str, str],
    expected_decision: str,
    expected_outcome: str,
    expected_summary: str,
) -> None:
    class _ApprovalAdapter(_RecordingAdapter):
        def __init__(self, calls: list[tuple[str, Any]]) -> None:
            super().__init__(calls, "codex")
            self.answered = asyncio.Event()

        async def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
            common = {
                "schema_version": 2,
                "timestamp": 1.0,
                "run_id": handle.run_id,
                "scope_id": f"scope-{handle.run_id}",
                "source": SourceRef(framework="codex"),
            }
            yield InteractionRequested(
                event_id="approval-requested",
                seq=1,
                interaction_id="item-canonical-approval",
                interaction_kind="approval",
                request=ApprovalRequest(
                    call_id="call-native-tool",
                    kind="command_execution",
                    detail={"command": "echo approval-ok"},
                ),
                **common,
            )
            yield RunInterrupted(
                event_id="approval-waiting",
                seq=2,
                status="interrupted",
                reason="Codex requires user interaction",
                interaction_id="item-canonical-approval",
                continuation_id="thread-1",
                **common,
            )
            await self.answered.wait()
            yield RunCompleted(
                event_id="approval-completed",
                seq=3,
                status="completed",
                output_refs=(),
                **common,
            )

        async def submit(self, handle: RunHandle, payload: ResumePayload) -> None:
            self.calls.append(("submit", payload))
            self.answered.set()

    calls: list[tuple[str, Any]] = []
    registry = RuntimeRegistry()
    registry.register("codex", lambda _context: _ApprovalAdapter(calls))
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
                agent_id="approval-helper",
            ),
            "执行审批命令",
            session_id="ses-approval",
        )
    )
    for _ in range(30):
        await asyncio.sleep(0.01)
        run = service.event_store.list_runs(session_id="ses-approval")[0]
        if run.status == RunStatus.WAITING_INPUT:
            break
    assert run.status == RunStatus.WAITING_INPUT

    await service.submit_interaction(
        run.id,
        "item-canonical-approval",
        name=action,
        data=response,
        expected_revision=1,
        idempotency_key=f"interaction:item-canonical-approval:{action}:revision-1",
    )
    completed = await asyncio.wait_for(task, timeout=2)
    assert completed.status == RunStatus.COMPLETED

    submitted = [value for name, value in calls if name == "submit"][-1]
    assert submitted.kind == "approval_decision"
    assert submitted.call_id == "call-native-tool"
    assert submitted.data == {**response, "decision": expected_decision}
    resolved = next(
        event for event in service.event_store.events(run.id) if event.type == "approval.resolved"
    )
    assert resolved.data["interactionId"] == "item-canonical-approval"
    assert resolved.data["callId"] == "call-native-tool"
    assert resolved.data["action"] == action
    assert resolved.data["outcome"] == expected_outcome
    assert resolved.data["actor"] == "user"
    assert resolved.data["responseSummary"] == expected_summary
    assert [name for name, _ in calls].count("submit") == 1
    assert "a2ui.action" in service_event_types(workspace, run.id)
    assert "run.interrupted" not in service_event_types(workspace, run.id)
    assert service_event_types(workspace, run.id).count("approval.resolved") == 1


@pytest.mark.asyncio
async def test_terminal_run_expires_an_unresolved_approval_card(tmp_path: Path) -> None:
    class _FailingApprovalAdapter(_RecordingAdapter):
        async def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
            common = {
                "schema_version": 2,
                "timestamp": 1.0,
                "run_id": handle.run_id,
                "scope_id": f"scope-{handle.run_id}",
                "source": SourceRef(framework="codex"),
            }
            yield InteractionRequested(
                event_id="approval-requested",
                seq=1,
                interaction_id="approval-orphan",
                interaction_kind="approval",
                request=ApprovalRequest(
                    call_id="call-orphan",
                    kind="command_execution",
                    detail={"command": "echo never-ran"},
                ),
                **common,
            )
            yield RunFailed(
                event_id="approval-failed",
                seq=2,
                status="failed",
                error={
                    "code": "RUNTIME_RUN_FAILED",
                    "message": "transport closed",
                    "source": "codex",
                    "scope_id": handle.run_id,
                },
                **common,
            )

    registry = RuntimeRegistry()
    registry.register("codex", lambda _context: _FailingApprovalAdapter([], "codex"))
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(registry))

    record = await service.run(
        StudioRunSpec(
            launch_context=RuntimeLaunchContext(runtime_type="codex", project_dir=tmp_path),
            build_id="build-codex",
            agent_id="approval-helper",
        ),
        "执行审批命令",
        session_id="ses-failed-approval",
    )

    assert record.status == RunStatus.FAILED
    expired = [
        event
        for event in service.event_store.events(record.id)
        if event.type == "interaction.expired"
    ]
    assert len(expired) == 1
    assert expired[0].data["interactionId"] == "approval-orphan"
    assert expired[0].data["callId"] == "call-orphan"


def test_a2ui_runtime_events_are_persisted_as_official_operations() -> None:
    event_type, payload = project_runtime_event(
        ItemStarted(
            event_id="e1",
            seq=1,
            item_id="surface-1",
            item_kind="data",
            initial=ContentSnapshot(
                parts=(
                    DataContent(
                        part_id="data-0",
                        data=[
                            {
                                "version": "v0.9",
                                "createSurface": {
                                    "surfaceId": "surface-1",
                                    "catalogId": "catalog-1",
                                },
                            },
                            {
                                "version": "v0.9",
                                "updateComponents": {
                                    "surfaceId": "surface-1",
                                    "components": [
                                        {"id": "root", "component": "Text", "text": "Hello"}
                                    ],
                                },
                            },
                            {
                                "version": "v0.9",
                                "updateDataModel": {
                                    "surfaceId": "surface-1",
                                    "path": "/",
                                    "value": {"ready": True},
                                },
                            },
                        ],
                    ),
                )
            ),
            schema_version=2,
            timestamp=1.0,
            run_id="run-1",
            scope_id="scope-1",
            source=SourceRef(
                framework="ksadk",
                protocol="a2ui",
                metadata={"surface_id": "surface-1"},
            ),
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


def test_completed_a2ui_operation_batch_keeps_surface_visible() -> None:
    event_type, payload = project_runtime_event(
        ItemCompleted(
            event_id="e2",
            seq=2,
            item_id="surface-batch-1",
            item_kind="data",
            snapshot=ContentSnapshot(
                parts=(
                    DataContent(
                        part_id="a2ui-surface",
                        data={
                            "surface_id": "surface-1",
                            "components": [{"id": "root", "component": "Text", "text": "Hello"}],
                        },
                    ),
                )
            ),
            schema_version=2,
            timestamp=2.0,
            run_id="run-1",
            scope_id="scope-1",
            source=SourceRef(
                framework="codex",
                protocol="a2ui",
                metadata={
                    "surface_id": "surface-1",
                    "operation_batch": True,
                    "surface_lifecycle": "begin",
                },
            ),
        )
    )

    assert event_type == "a2ui.surface.begin"
    assert [
        next(iter(operation.keys() - {"version"})) for operation in payload["a2uiOperations"]
    ] == ["createSurface", "updateComponents"]


def service_event_types(workspace: Workspace, run_id: str) -> list[str]:
    return [
        event.type
        for event in StudioRunService(
            workspace,
            RuntimeExecutor(RuntimeRegistry()),
        ).event_store.events(run_id)
    ]


def test_generic_tool_events_project_normalized_fields() -> None:
    """MCP 等非命令工具事件必须投影出 tool/args/output,trace 层才能成 span。"""
    tool_args = {
        "server": "metaso-inner",
        "tool": "metaso_web_search",
        "arguments": {"q": "x"},
    }
    begin_type, begin_payload = project_runtime_event(
        ItemStarted(
            event_id="e1",
            seq=1,
            item_id="tool-mcp-1",
            item_kind="tool_call",
            initial=ContentSnapshot(
                parts=(
                    ToolCallContent(
                        part_id="tool-0",
                        call_id="mcp-1",
                        name="mcp.metaso-inner.metaso_web_search",
                        arguments=tool_args,
                    ),
                )
            ),
            schema_version=2,
            timestamp=1.0,
            run_id="run-1",
            scope_id="scope-1",
            source=SourceRef(framework="ksadk"),
        )
    )
    assert begin_type == "tool.started"
    assert begin_payload["callId"] == "mcp-1"
    assert begin_payload["tool"] == "mcp.metaso-inner.metaso_web_search"
    assert begin_payload["args"]["arguments"] == {"q": "x"}
    assert "runtimeEvent" in begin_payload

    end_type, end_payload = project_runtime_event(
        ItemCompleted(
            event_id="e2",
            seq=2,
            item_id="tool-mcp-1",
            item_kind="tool_call",
            snapshot=ContentSnapshot(
                parts=(
                    ToolCallContent(
                        part_id="tool-0",
                        call_id="mcp-1",
                        name="mcp.metaso-inner.metaso_web_search",
                        arguments=tool_args,
                    ),
                    ToolResultContent(
                        part_id="tool-0",
                        call_id="mcp-1",
                        result={"status": "completed", "duration_ms": 12, "output": "结果"},
                    ),
                )
            ),
            schema_version=2,
            timestamp=1.0,
            run_id="run-1",
            scope_id="scope-1",
            source=SourceRef(framework="ksadk"),
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
        ItemCompleted(
            event_id="e1",
            seq=1,
            item_id="tool-mcp-2",
            item_kind="tool_call",
            snapshot=ContentSnapshot(
                parts=(
                    ToolCallContent(
                        part_id="tool-0",
                        call_id="mcp-2",
                        name="mcp.metaso-inner.metaso_web_search",
                        arguments={},
                    ),
                    ToolResultContent(
                        part_id="tool-0",
                        call_id="mcp-2",
                        result={"status": "failed", "error": "boom", "output": "boom"},
                        is_error=True,
                    ),
                )
            ),
            schema_version=2,
            timestamp=1.0,
            run_id="run-1",
            scope_id="scope-1",
            source=SourceRef(framework="ksadk"),
        )
    )
    assert payload["status"] == "failed"
    assert payload["error"] == "boom"


@pytest.mark.asyncio
async def test_same_session_rejects_overlap_before_start_and_releases_on_cancel(tmp_path):
    calls = []
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingAdapter(_RecordingAdapter):
        async def stream(self, handle):
            entered.set()
            await release.wait()
            async for event in super().stream(handle):
                yield event

    registry = RuntimeRegistry()
    registry.register("langgraph", lambda _: BlockingAdapter(calls))
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(registry))
    spec = StudioRunSpec(
        launch_context=RuntimeLaunchContext(runtime_type="langgraph", project_dir=tmp_path),
        build_id="build",
        agent_id="agent",
    )
    first = asyncio.create_task(service.run(spec, "first", session_id="same"))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        with pytest.raises(StudioError) as caught:
            await service.run(spec, "overlap", session_id="same")
        assert caught.value.code == "SESSION_RUN_ACTIVE"
        assert caught.value.status_code == 409
        assert len([c for c in calls if c[0] == "start"]) == 1
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
    release.set()
    next_run = await service.run(spec, "next", session_id="same")
    assert next_run.status == RunStatus.COMPLETED


def test_native_question_projection_preserves_schema_and_submit_identity():
    event = InteractionRequested(
        schema_version=2,
        event_id="question",
        seq=1,
        timestamp=1,
        run_id="run",
        scope_id="scope",
        interaction_id="canonical-question",
        interaction_kind="structured_input",
        request=StructuredInputRequest(
            prompt="选择范围",
            schema={
                "type": "object",
                "properties": {"scope": {"type": "string"}},
                "required": ["scope"],
            },
        ),
        source=SourceRef(framework="codex", native_event_id="native-question"),
    )
    _, data = project_runtime_event(event)
    assert data["inputSchema"]["required"] == ["scope"]
    assert data["callId"] == "native-question"
    assert data["message"] == "选择范围"
