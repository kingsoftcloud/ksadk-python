from __future__ import annotations

import asyncio
import json
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
        framework = "ksadk" if self.runtime_type == "harness" else self.runtime_type
        source = SourceRef(framework=framework)
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
                framework=framework,
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
async def test_harness_runtime_keeps_private_deltas_out_of_run_timeline(tmp_path: Path) -> None:
    class _HarnessDeltaAdapter(_RecordingAdapter):
        async def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
            async for event in super().stream(handle):
                yield event
                if isinstance(event, RunStarted):
                    yield ItemCompleted(
                        schema_version=2,
                        event_id="private-reasoning-delta",
                        seq=0,
                        timestamp=1.0,
                        run_id=handle.run_id,
                        scope_id=f"scope-{handle.run_id}",
                        item_id="private-status",
                        item_kind="status",
                        snapshot=ContentSnapshot(
                            parts=(
                                TextContent(
                                    part_id="status-0",
                                    text=(
                                        '{"event":"reasoning.delta",'
                                        '"details":{"text":"private"}}'
                                    ),
                                ),
                            )
                        ),
                        source=SourceRef(
                            framework="ksadk",
                            metadata={"native_event_type": "reasoning.delta"},
                        ),
                    )

    registry = RuntimeRegistry()
    registry.register("harness", lambda _context: _HarnessDeltaAdapter([], "harness"))
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(registry))

    record = await service.run(
        StudioRunSpec(
            launch_context=RuntimeLaunchContext(runtime_type="harness", project_dir=tmp_path),
            build_id="build-harness-delta",
            agent_id="harness-delta-agent",
        ),
        "run",
    )

    assert record.status == RunStatus.COMPLETED
    public_event_ids = {
        event.data.get("runtimeEvent", {}).get("event_id")
        for event in service.event_store.events(record.id)
    }
    assert "private-reasoning-delta" not in public_event_ids
    canonical = await service.runtime_events.list(record.session_id)
    assert "private-reasoning-delta" not in {event.event_id for event in canonical}


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
async def test_harness_followup_receives_verified_child_lifecycle_summary(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, Any]] = []
    registry = RuntimeRegistry()
    registry.register("harness", lambda _context: _RecordingAdapter(calls, "harness"))
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(registry))
    spec = StudioRunSpec(
        launch_context=RuntimeLaunchContext(runtime_type="harness", project_dir=tmp_path),
        build_id="build-harness",
        agent_id="research-helper",
    )

    first = await service.run(spec, "并行调研", session_id="ses-harness")
    assert first.status == RunStatus.COMPLETED, first.error
    assert first.runtime_type == "harness"
    service.event_store.append(
        first.id,
        "item.completed",
        {
            "runtimeEvent": {
                "event_id": "child-terminal",
                "run_id": first.runtime_handle["run_id"],
                "item_kind": "status",
                "source": {
                    "native_run_id": first.runtime_handle["run_id"],
                    "metadata": {"session_id": "ses-harness"},
                },
                "snapshot": {
                    "parts": [
                        {
                            "type": "text",
                            "part_id": "text-0",
                            "text": json.dumps(
                                {
                                    "event": "run.progress",
                                    "details": {
                                        "kind": "subagent.event",
                                        "call_id": "research-one",
                                        "label": "调研主题一",
                                        "status": "succeeded",
                                    },
                                },
                                ensure_ascii=False,
                            ),
                        }
                    ]
                },
            }
        },
    )

    await service.run(spec, "进展如何？", session_id="ses-harness")

    starts = [value for name, value in calls if name == "start"]
    conversation = starts[1].conversation_preprocessing()
    assert conversation is not None
    assert conversation.messages[-3:] == [
        {"role": "user", "content": "并行调研"},
        {
            "role": "assistant",
            "content": (
                "harness answer\n\n"
                "[Harness 执行记录] 子智能体“调研主题一”已完成。"
            ),
        },
        {"role": "user", "content": "进展如何？"},
    ]


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
@pytest.mark.parametrize(
    ("runtime_type", "pause_reason"),
    [("codex", "user_pause"), ("harness", "paused")],
)
async def test_pause_preserves_run_and_resume_continues_same_runtime_handle(
    tmp_path: Path,
    runtime_type: str,
    pause_reason: str,
) -> None:
    class _PausableAdapter(_RecordingAdapter):
        def __init__(self, calls: list[tuple[str, Any]]) -> None:
            super().__init__(calls, runtime_type)
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
            source = SourceRef(
                framework="codex" if runtime_type == "codex" else "ksadk",
                metadata={"engine_kind": "native"} if runtime_type == "harness" else {},
            )
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
                    reason=pause_reason,
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
    registry.register(runtime_type, lambda _context: _PausableAdapter(calls))
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(registry))
    task = asyncio.create_task(
        service.run(
            StudioRunSpec(
                launch_context=RuntimeLaunchContext(
                    runtime_type=runtime_type, project_dir=tmp_path
                ),
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
    resumed = next(value for name, value in calls if name == "resume")
    assert resumed[0].run_id == run.runtime_handle["run_id"]
    assert resumed[2] is None  # Continue the paused task without a synthetic user message.


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


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_first", [False, True])
async def test_same_session_queued_followups_are_ordered_and_cancelable(
    tmp_path, cancel_first
):
    calls = []
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingAdapter(_RecordingAdapter):
        async def stream(self, handle):
            if not entered.is_set():
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
    canceled = asyncio.create_task(
        service.run(spec, "cancel-me", session_id="same", queue_if_active=True)
    )
    second = asyncio.create_task(
        service.run(spec, "second", session_id="same", queue_if_active=True)
    )
    third = asyncio.create_task(
        service.run(spec, "third", session_id="same", queue_if_active=True)
    )
    await asyncio.sleep(0)
    assert len([call for call in calls if call[0] == "start"]) == 1
    canceled.cancel()
    canceled_result = (await asyncio.gather(canceled, return_exceptions=True))[0]
    assert isinstance(canceled_result, asyncio.CancelledError)
    with pytest.raises(StudioError) as caught:
        await service.run(spec, "jump-ahead", session_id="same")
    assert caught.value.code == "SESSION_RUN_ACTIVE"
    if cancel_first:
        first.cancel()
        first_result = (await asyncio.gather(first, return_exceptions=True))[0]
        assert isinstance(first_result, asyncio.CancelledError)
        assert service.event_store.list_runs(session_id="same")[0].status == RunStatus.CANCELLED
    else:
        release.set()
    results = await asyncio.wait_for(asyncio.gather(second, third), 5)
    if not cancel_first:
        results.insert(0, await asyncio.wait_for(first, 5))
    assert all(result.status == RunStatus.COMPLETED for result in results)
    assert [call[1].input for call in calls if call[0] == "start"] == [
        "first",
        "second",
        "third",
    ]
    assert not service._session_turn_users
    assert not service._session_turn_locks


@pytest.mark.asyncio
async def test_same_session_followup_waits_while_tool_call_is_in_flight(tmp_path):
    calls = []
    tool_entered = asyncio.Event()
    tool_release = asyncio.Event()

    class ToolBlockedAdapter(_RecordingAdapter):
        async def stream(self, handle):
            parent = super().stream(handle)
            yield await anext(parent)
            blocked = not tool_entered.is_set()
            if blocked:
                common = {
                    "schema_version": 2,
                    "timestamp": 1.0,
                    "run_id": handle.run_id,
                    "scope_id": f"scope-{handle.run_id}",
                    "source": SourceRef(framework="langgraph"),
                }
                yield ItemStarted(
                    event_id="tool-started",
                    seq=2,
                    item_id="tool-1",
                    item_kind="tool_call",
                    initial=ContentSnapshot(
                        parts=(
                            ToolCallContent(
                                part_id="tool-0",
                                call_id="call-1",
                                name="fixture.read",
                                arguments={},
                            ),
                        )
                    ),
                    **common,
                )
                tool_entered.set()
                await tool_release.wait()
                yield ItemCompleted(
                    event_id="tool-completed",
                    seq=3,
                    item_id="tool-1",
                    item_kind="tool_call",
                    snapshot=ContentSnapshot(
                        parts=(
                            ToolCallContent(
                                part_id="tool-0",
                                call_id="call-1",
                                name="fixture.read",
                                arguments={},
                            ),
                            ToolResultContent(
                                part_id="result-0",
                                call_id="call-1",
                                result={"ok": True},
                            ),
                        )
                    ),
                    **common,
                )
            async for event in parent:
                yield event.model_copy(update={"seq": event.seq + 2}) if blocked else event

    registry = RuntimeRegistry()
    registry.register("langgraph", lambda _: ToolBlockedAdapter(calls))
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(registry))
    spec = StudioRunSpec(
        launch_context=RuntimeLaunchContext(runtime_type="langgraph", project_dir=tmp_path),
        build_id="build",
        agent_id="agent",
    )
    first = asyncio.create_task(service.run(spec, "first", session_id="same"))
    await asyncio.wait_for(tool_entered.wait(), 2)
    second = asyncio.create_task(
        service.run(spec, "second", session_id="same", queue_if_active=True)
    )
    await asyncio.sleep(0)
    records = service.event_store.list_runs(session_id="same")
    assert len(records) == 1
    assert "tool.started" in service_event_types(workspace, records[0].id)
    assert len([call for call in calls if call[0] == "start"]) == 1
    tool_release.set()
    completed = await asyncio.wait_for(asyncio.gather(first, second), 5)
    assert all(record.status == RunStatus.COMPLETED for record in completed)
    assert [call[1].input for call in calls if call[0] == "start"] == ["first", "second"]


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


@pytest.mark.asyncio
async def test_plugin_run_streams_session_store_events_live_without_duplicates(
    tmp_path: Path,
) -> None:
    """_plugin_run 必须在 execute 进行中转发 canonical 增量，且终局零重复。"""

    gate = asyncio.Event()
    service_holder: dict[str, Any] = {}

    class _LiveBufferedPluginRuntime:
        async def execute(self, _spec: StudioRunSpec, request: dict[str, Any], *, session_id: str):
            run_id = str(request["invocation_id"])
            store = service_holder["service"].runtime_events
            common = {
                "schema_version": 2,
                "timestamp": 1.0,
                "run_id": run_id,
                "scope_id": f"scope-{run_id}",
                "source": SourceRef(framework="ksadk"),
            }
            await store.append_one(
                session_id,
                RunStarted(event_id="live-e1", seq=0, status="running", **common),
            )
            await store.append_one(
                session_id,
                ItemStarted(
                    event_id="live-e2",
                    seq=0,
                    item_id="msg-1",
                    item_kind="message",
                    phase="final_answer",
                    initial=ContentSnapshot(parts=(TextContent(part_id="text-0", text="hel"),)),
                    **common,
                ),
            )
            await store.append_one(
                session_id,
                ItemUpdated(
                    event_id="live-e3",
                    seq=0,
                    item_id="msg-1",
                    item_kind="message",
                    op="append",
                    update=TextContent(part_id="text-0", text="lo"),
                    **common,
                ),
            )
            await store.append_one(
                session_id,
                ItemCompleted(
                    event_id="live-private-child-delta",
                    seq=0,
                    item_id="private-status-1",
                    item_kind="status",
                    snapshot=ContentSnapshot(
                        parts=(
                            TextContent(
                                part_id="status-0",
                                text='{"event":"text.delta","details":{"text":"private"}}',
                            ),
                        )
                    ),
                    **{
                        **common,
                        "source": SourceRef(
                            framework="ksadk",
                            native_run_id=f"{run_id}:sub:child-1:child-1",
                            metadata={
                                "native_event_type": "text.delta",
                                "parent_run_id": run_id,
                            },
                        ),
                    },
                ),
            )
            await asyncio.wait_for(gate.wait(), timeout=2)
            await store.append_one(
                session_id,
                ItemUpdated(
                    event_id="live-e4",
                    seq=0,
                    item_id="msg-1",
                    item_kind="message",
                    op="append",
                    update=TextContent(part_id="text-0", text="!"),
                    **common,
                ),
            )
            await store.append_one(
                session_id,
                RunCompleted(
                    event_id="live-e5",
                    seq=0,
                    status="completed",
                    output_refs=(),
                    **common,
                ),
            )
            return SimpleNamespace(output_text="hello!", session_id=session_id, usage={})

    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(
        workspace,
        RuntimeExecutor(RuntimeRegistry()),
        plugin_runtime=_LiveBufferedPluginRuntime(),
    )
    service_holder["service"] = service
    observed: list[tuple[str, str]] = []

    task = asyncio.create_task(
        service.run(
            StudioRunSpec(
                launch_context=RuntimeLaunchContext(
                    runtime_type="harness",
                    project_dir=tmp_path,
                ),
                build_id="build-harness-live",
                agent_id="harness-live-agent",
                model="fixture-model",
                plugin_bundle_root=tmp_path,
            ),
            "stream this live",
            session_id="ses-plugin-live",
            on_event=lambda event: observed.append(
                (
                    event.type,
                    str(event.data.get("text") or event.data.get("delta") or ""),
                )
            ),
        )
    )

    # execute 完成前，session store 中已有的增量必须已经到达订阅端。
    # 等到含 "lo" 的增量出现（ItemStarted 首块 "hel" 可能先到/被合并）。
    for _ in range(200):
        if any(
            event_type == "message.delta" and "lo" in text
            for event_type, text in observed
        ):
            break
        await asyncio.sleep(0.01)
    assert any(
        event_type == "message.delta" and "lo" in text
        for event_type, text in observed
    )
    assert not task.done()

    gate.set()
    record = await asyncio.wait_for(task, timeout=2)

    assert record.status == RunStatus.COMPLETED
    assert record.output == "hello!"
    # 终局：最后一个 delta 与 run.completed 恰好各出现一次（live + 补发去重）。
    deltas = [text for event_type, text in observed if event_type == "message.delta"]
    assert deltas.count("!") == 1
    assert [event_type for event_type, _ in observed].count("run.completed") == 1
    assert "private" not in [text for _, text in observed]
    assert all(
        event.data.get("runtimeEvent", {}).get("event_id") != "live-private-child-delta"
        for event in await service.events(record.id)
    )


@pytest.mark.asyncio
async def test_run_progress_watchdog_dumps_task_stacks_when_no_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """无进展看门狗必须把"当时在干什么"写进日志。

    依据：2026-09-18 一次 run 静默 437s、进程 CPU ~20%，日志里没有任何可归因
    记录；macOS 上 py-spy 需要 root，只能靠进程内 dump。
    """

    import logging

    monkeypatch.setenv("KSADK_RUN_STALL_WARN_SECONDS", "0.05")
    monkeypatch.setenv("KSADK_RUN_STALL_DUMP_SECONDS", "0.1")
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(RuntimeRegistry()))

    watchdog = asyncio.create_task(service._watch_run_progress())
    try:
        with caplog.at_level(logging.WARNING, logger="ksadk.studio.run_service"):
            await asyncio.sleep(0.5)
    finally:
        watchdog.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watchdog

    messages = [record.getMessage() for record in caplog.records]
    assert any("没有新事件" in message for message in messages)
    assert any("asyncio 任务现场" in message for message in messages)


@pytest.mark.asyncio
async def test_run_progress_watchdog_disabled_by_non_positive_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    monkeypatch.setenv("KSADK_RUN_STALL_WARN_SECONDS", "0")
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(RuntimeRegistry()))

    with caplog.at_level(logging.WARNING, logger="ksadk.studio.run_service"):
        await service._watch_run_progress()

    assert [record for record in caplog.records] == []


def test_progress_marker_counts_non_persisted_runtime_chunks(tmp_path: Path) -> None:
    """私有 reasoning delta 不落盘，但必须让无进展看门狗续期。"""

    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(RuntimeRegistry()))
    before = service._progress_marker()

    service._runtime_progress_seq += 1

    after = service._progress_marker()
    assert after[0] == before[0]
    assert after[1] == before[1]
    assert after[2] == before[2] + 1


@pytest.mark.asyncio
async def test_task_stack_dump_walks_nested_await_chain() -> None:
    """dump 必须穿透 ``await`` 链（上一版只给当前协程一帧，run 本体只剩一帧）。"""

    import ksadk.studio.run_service as rs

    async def inner() -> None:
        await asyncio.Future()  # 永不解锁的边界

    async def outer() -> None:
        await inner()

    task = asyncio.create_task(outer())
    try:
        await asyncio.sleep(0)  # 让任务跑到 inner 里的 await
        dump = rs._format_task_stacks()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # 链上两层协程的帧都在：外层的 "await inner()" 与内层的 "await asyncio.Future()"
    assert "await inner()" in dump
    assert "await asyncio.Future()" in dump
    # 撞到 Future 边界时被显式记录（fut_waiter/await-boundary）
    assert "Future" in dump


@pytest.mark.asyncio
async def test_watchdog_dumps_repeatedly_while_stall_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """持续卡住必须反复 dump，直到次数上限。

    依据：一次性 dump 区分不了"卡住"和"只是慢"——2026-09-18 那次静默 10 分钟，
    仅存的一份 dump 早在 6 分钟前，说明不了当时状态。
    """

    import logging

    monkeypatch.setenv("KSADK_RUN_STALL_WARN_SECONDS", "0.05")
    monkeypatch.setenv("KSADK_RUN_STALL_DUMP_SECONDS", "0.1")
    monkeypatch.setenv("KSADK_RUN_STALL_MAX_DUMPS", "3")
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(RuntimeRegistry()))

    watchdog = asyncio.create_task(service._watch_run_progress())
    try:
        with caplog.at_level(logging.WARNING, logger="ksadk.studio.run_service"):
            await asyncio.sleep(1.0)
    finally:
        watchdog.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watchdog

    dumps = [r for r in caplog.records if "次现场" in r.getMessage()]
    assert len(dumps) == 3
    assert all("事件循环最近的定时器" in r.getMessage() for r in dumps)


@pytest.mark.asyncio
async def test_loop_timer_dump_reveals_pending_long_sleep() -> None:
    """dump 必须能看出"有一个很久以后才到期的 sleep"——这正是本次要判定的分叉。"""

    import ksadk.studio.run_service as rs

    async def long_sleep() -> None:
        await asyncio.sleep(30)

    task = asyncio.create_task(long_sleep())
    try:
        await asyncio.sleep(0)  # 让 sleep 定时器挂进事件循环
        dump = rs._format_loop_timers()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert "没有待触发的定时器" not in dump
    first_line = dump.splitlines()[0]
    seconds = float(first_line.strip().split("s 后")[0])
    assert seconds > 20


def test_require_delivery_rejects_empty_output() -> None:
    """空交付必须抛 EMPTY_DELIVERY，不得判成功。

    依据：2026-09-18 实测——一次 harness run（runtimeType=harness）以
    COMPLETED + output 长度 0 + 产物 0 收场；第一版守卫被错放在
    ``_schedule_run``，真正的完成点 ``_plugin_run`` 没有闸门。
    """

    from ksadk.studio.errors import StudioError
    from ksadk.studio.run_service import _require_delivery

    _require_delivery("有交付内容")  # 非空不抛

    for empty in ("", "   ", None):
        with pytest.raises(StudioError) as excinfo:
            _require_delivery(empty)
        assert excinfo.value.code == "EMPTY_DELIVERY"


def test_consumed_model_output_narrows_empty_delivery_guard() -> None:
    """空交付只在"跑过模型、烧了输出 token"时才成立；合法空输出（审批流）不受影响。"""

    from types import SimpleNamespace

    from ksadk.studio.run_service import _consumed_model_output

    assert _consumed_model_output(SimpleNamespace(usage=SimpleNamespace(output_tokens=96)))
    assert not _consumed_model_output(SimpleNamespace(usage=SimpleNamespace(output_tokens=0)))
    assert not _consumed_model_output(SimpleNamespace(usage=None))


@pytest.mark.asyncio
async def test_empty_delivery_publishes_only_failed_terminal_event(tmp_path: Path) -> None:
    """Provider completion is provisional until Studio accepts the delivery."""

    class EmptyDeliveryAdapter(_RecordingAdapter):
        async def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
            common = {
                "schema_version": 2,
                "timestamp": 1.0,
                "run_id": handle.run_id,
                "scope_id": f"scope-{handle.run_id}",
                "source": SourceRef(framework="ksadk"),
            }
            yield RunStarted(event_id="empty-start", seq=1, status="running", **common)
            yield UsageReported(
                event_id="empty-usage",
                seq=2,
                input_tokens=10,
                output_tokens=20,
                total_tokens=30,
                **common,
            )
            yield RunCompleted(
                event_id="empty-completed",
                seq=3,
                status="completed",
                output_refs=(),
                **common,
            )

    registry = RuntimeRegistry()
    registry.register("harness", lambda _context: EmptyDeliveryAdapter([], "harness"))
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(registry))

    record = await service.run(
        StudioRunSpec(
            launch_context=RuntimeLaunchContext(runtime_type="harness", project_dir=tmp_path),
            build_id="build-empty-delivery",
            agent_id="empty-delivery-agent",
        ),
        "执行任务",
        session_id="ses-empty-delivery",
    )

    assert record.status == RunStatus.FAILED
    canonical = await service.runtime_events.list("ses-empty-delivery")
    terminal_types = [
        event.event_type
        for event in canonical
        if event.event_type in {"run.completed", "run.failed", "run.canceled"}
    ]
    assert terminal_types == ["run.failed"]
