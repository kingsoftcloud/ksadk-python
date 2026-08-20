from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.events.store import RuntimeEventStore
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
from ksadk.sessions.local_service import LocalSessionService
from ksadk.studio.contracts import RunRecord, RunStatus
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
        self.invocation_id = ""
        self.trace_id = ""

    async def start(self, request: StartRequest) -> RunHandle:
        self.calls.append(("start", request))
        self.invocation_id = str(request.metadata["invocation_id"])
        self.trace_id = str(request.metadata["trace_id"])
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
            invocation_id=self.invocation_id,
            seq_id=1,
            trace_id=self.trace_id,
            payload={"status": "in_progress"},
        )
        yield RuntimeEvent.create(
            EventType.TEXT_COMPLETED,
            agent_id="review-helper",
            user_id="local-user",
            session_id=handle.session_id,
            invocation_id=self.invocation_id,
            seq_id=2,
            trace_id=self.trace_id,
            phase="final_answer",
            payload={"text": f"{self.runtime_type} answer"},
        )
        yield RuntimeEvent.create(
            EventType.RUN_COMPLETED,
            agent_id="review-helper",
            user_id="local-user",
            session_id=handle.session_id,
            invocation_id=self.invocation_id,
            seq_id=3,
            trace_id=self.trace_id,
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
    events = await service.runtime_events.list(record.session_id, invocation_id=record.id)
    assert [event.event_type for event in events] == [
        EventType.RUN_PROGRESS,
        EventType.USER_MESSAGE,
        EventType.RUN_STARTED,
        EventType.TEXT_COMPLETED,
        EventType.RUN_COMPLETED,
    ]
    assert {event.trace_id for event in events} == {record.trace_id}


@pytest.mark.asyncio
async def test_studio_appends_runtime_events_without_rewriting_run_or_trace_files(
    tmp_path: Path,
) -> None:
    class _DeltaAdapter(_RecordingAdapter):
        async def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
            trace_path = tmp_path / ".agentkit/traces" / f"{self.trace_id}.otlp.json"
            for seq_id in range(1, 101):
                assert not trace_path.exists()
                yield RuntimeEvent.create(
                    EventType.TEXT_DELTA,
                    agent_id="review-helper",
                    user_id="local-user",
                    session_id=handle.session_id,
                    invocation_id=self.invocation_id,
                    seq_id=seq_id,
                    trace_id=self.trace_id,
                    phase="final_answer",
                    payload={"text": "x"},
                )
            yield RuntimeEvent.create(
                EventType.RUN_COMPLETED,
                agent_id="review-helper",
                user_id="local-user",
                session_id=handle.session_id,
                invocation_id=self.invocation_id,
                seq_id=101,
                trace_id=self.trace_id,
                payload={"status": "completed"},
            )

    registry = RuntimeRegistry()
    registry.register("langgraph", lambda _context: _DeltaAdapter([]))
    workspace = Workspace(tmp_path)
    workspace.initialize()
    sessions = LocalSessionService(tmp_path / "sessions.sqlite")
    runtime_events = RuntimeEventStore(sessions)
    service = StudioRunService(
        workspace,
        RuntimeExecutor(registry),
        session_service=sessions,
        runtime_events=runtime_events,
    )

    record = await service.run(
        StudioRunSpec(
            launch_context=RuntimeLaunchContext(runtime_type="langgraph", project_dir=tmp_path),
            build_id="build-lg",
            agent_id="review-helper",
        ),
        "stream",
        session_id="ses-stream",
    )

    events = await runtime_events.list(record.session_id, invocation_id=record.id)
    assert [event.seq_id for event in events] == list(range(1, 104))
    run_payload = json.loads((tmp_path / ".agentkit/runs" / f"{record.id}.json").read_text())
    assert set(run_payload) == {"record"}
    assert (tmp_path / ".agentkit/traces" / f"{record.trace_id}.otlp.json").is_file()


@pytest.mark.asyncio
async def test_studio_binds_missing_runtime_trace_id_to_the_run(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(RuntimeRegistry()))
    record = RunRecord(
        id="run-bind-trace",
        build_id="build-1",
        agent_id="agent-1",
        session_id="session-bind-trace",
        trace_id="a" * 32,
        input="test",
    )
    service.event_store.create(record)
    await service.session_service.create_session(record.agent_id, "local-user", record.session_id)
    source = RuntimeEvent.create(
        EventType.RUN_STARTED,
        agent_id=record.agent_id,
        user_id="local-user",
        session_id=record.session_id,
        invocation_id=record.id,
        seq_id=1,
        payload={"status": "in_progress"},
    )

    await service._persist_event(record, source)

    stored = await service.runtime_events.list(record.session_id, invocation_id=record.id)
    assert stored[0].trace_id == record.trace_id


@pytest.mark.asyncio
async def test_recovery_uses_canonical_terminal_event(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(RuntimeRegistry()))
    record = RunRecord(
        id="run_recover_cancelled",
        build_id="build-1",
        agent_id="review-helper",
        session_id="ses-recover-cancelled",
        trace_id="1" * 32,
        status=RunStatus.RUNNING,
        input="resume",
        started_at=datetime.now(timezone.utc),
    )
    service.event_store.create(record)
    await service.session_service.create_session(
        record.agent_id,
        "local-user",
        record.session_id,
    )
    await service.runtime_events.append_one(
        RuntimeEvent.create(
            EventType.RUN_CANCELED,
            agent_id=record.agent_id,
            user_id="local-user",
            session_id=record.session_id,
            invocation_id=record.id,
            seq_id=0,
            trace_id=record.trace_id,
            payload={"status": "cancelled"},
        )
    )

    assert await service.recover_interrupted() == 1
    recovered = service.event_store.get(record.id)
    assert recovered.status == RunStatus.CANCELLED
    assert recovered.error == {"code": "RUN_CANCELLED", "message": "运行已取消"}


@pytest.mark.asyncio
async def test_recovery_creates_missing_session_before_marking_interrupted(
    tmp_path: Path,
) -> None:
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(RuntimeRegistry()))
    record = RunRecord(
        id="run_recover_orphan",
        build_id="build-1",
        agent_id="review-helper",
        session_id="ses-recover-orphan",
        trace_id="2" * 32,
        status=RunStatus.RUNNING,
        input="resume",
        started_at=datetime.now(timezone.utc),
    )
    service.event_store.create(record)

    assert await service.recover_interrupted() == 1
    recovered = service.event_store.get(record.id)
    assert recovered.status == RunStatus.INTERRUPTED
    events = await service.runtime_events.list(record.session_id, invocation_id=record.id)
    assert events[-1].event_type == EventType.RUN_INTERRUPTED
    assert events[-1].payload["reason"] == "studio_restarted"


@pytest.mark.asyncio
async def test_studio_reuses_evidence_prepared_turn_for_runtime_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KSADK_PROMPT_COMPILER_ENABLED", "1")
    monkeypatch.setenv("KSADK_CONTEXT_ENGINE_V2_ENABLED", "1")
    calls: list[tuple[str, Any]] = []

    class _HostedMetricsAdapter(_RecordingAdapter):
        async def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
            async for event in super().stream(handle):
                if event.event_type == EventType.RUN_COMPLETED:
                    yield RuntimeEvent.create(
                        EventType.USAGE_REPORTED,
                        agent_id="review-helper",
                        user_id="local-user",
                        session_id=handle.session_id,
                        invocation_id=handle.run_id,
                        seq_id=3,
                        payload={
                            "input_tokens": 11,
                            "output_tokens": 4,
                            "total_tokens": 15,
                            "source": "fixture",
                        },
                    )
                yield event

    registry = RuntimeRegistry()
    registry.register("langgraph", lambda _context: _HostedMetricsAdapter(calls))
    workspace = Workspace(tmp_path)
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(registry))

    record = await service.run(
        StudioRunSpec(
            launch_context=RuntimeLaunchContext(
                runtime_type="langgraph",
                project_dir=tmp_path,
            ),
            build_id="build-hosted",
            agent_id="review-helper",
            model="glm-5.2",
            request_config={
                "agent_system": "你是部署助手。",
                "agent_task": "只操作预发环境。",
                "prompt_integration_mode": "ksadk_hosted",
            },
        ),
        "检查部署计划",
        session_id="ses-single-prepare",
    )

    request = next(value for name, value in calls if name == "start")
    conversation = request.conversation_preprocessing()
    assert conversation is not None
    prepared = (conversation.model_extra or {})["prepared_turn"]
    assert prepared["invocation_id"] == record.id
    finalized_plan = dict(record.context_plan)
    finalized_plan["runtime_reported_input_tokens"] = None
    assert prepared["context_plan"] == finalized_plan
    assert (
        prepared["compiled_prompt"]["prompt_content_hash"]
        == (record.prompt_evidence["contentHash"])
    )
    assert record.usage.reported is True
    assert record.context_plan["runtime_reported_input_tokens"] == 11


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
                invocation_id=self.invocation_id,
                seq_id=1,
                trace_id=self.trace_id,
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
                invocation_id=self.invocation_id,
                seq_id=2,
                trace_id=self.trace_id,
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
async def test_completed_hosted_studio_turn_flushes_explicit_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_db = tmp_path / "studio-memory.db"
    monkeypatch.setenv("KSADK_MEMORY_FLUSH_ENABLED", "1")
    monkeypatch.setenv("KSADK_MEMORY_DB_PATH", str(memory_db))
    calls: list[tuple[str, Any]] = []
    registry = RuntimeRegistry()
    registry.register("langgraph", lambda _context: _RecordingAdapter(calls))
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(registry))

    await service.run(
        StudioRunSpec(
            launch_context=RuntimeLaunchContext(
                runtime_type="langgraph",
                project_dir=tmp_path,
            ),
            build_id="build-hosted",
            agent_id="memory-agent",
            request_config={
                "prompt_integration_mode": "ksadk_hosted",
                "memory_write_rollout": "enabled",
                "memory_enabled": True,
                "memory_write_mode": "candidate",
                "memory_recall_enabled": True,
                "flush_before_compaction": True,
                "provider_ref": "local-default",
            },
        ),
        "请记住我的部署偏好：始终先执行 dry-run",
        session_id="ses-memory",
    )

    with sqlite3.connect(memory_db) as conn:
        contents = [row[0] for row in conn.execute("SELECT content FROM memory_records")]
    assert contents == ["我的部署偏好：始终先执行 dry-run"]


@pytest.mark.asyncio
async def test_codex_platform_memory_is_recalled_and_projected_across_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex native thread 也必须消费 AgentVersion 指定的平台 Memory Provider。"""
    memory_db = tmp_path / "codex-memory.db"
    monkeypatch.setenv("KSADK_MEMORY_FLUSH_ENABLED", "1")
    monkeypatch.setenv("KSADK_MEMORY_DB_PATH", str(memory_db))
    calls: list[tuple[str, Any]] = []
    registry = RuntimeRegistry()
    registry.register("codex", lambda _context: _RecordingAdapter(calls, "codex"))
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(registry))
    spec = StudioRunSpec(
        launch_context=RuntimeLaunchContext(runtime_type="codex", project_dir=tmp_path),
        build_id="build-codex-memory",
        agent_id="memory-agent",
        request_config={
            "base_instructions": "你是编程助手。",
            "memory_write_rollout": "enabled",
            "memory_enabled": True,
            "memory_recall_enabled": True,
            "memory_write_mode": "candidate",
            "flush_before_compaction": True,
            "provider_ref": "local-default",
        },
    )

    first = await service.run(spec, "记住我喜欢吃大蒜", session_id="ses-memory-write")
    second = await service.run(spec, "我喜欢吃什么", session_id="ses-memory-recall")

    first_events = [event.type for event in service.event_store.events(first.id)]
    second_events = [event.type for event in service.event_store.events(second.id)]
    assert "memory.flush.completed" in first_events
    assert "memory.recall.completed" in second_events
    assert "memory.recall.projected" in second_events
    start_requests = [value for name, value in calls if name == "start"]
    assert len(start_requests) == 2
    projected = start_requests[1].config["base_instructions"]
    assert "KsADK 平台长期记忆已启用" in projected
    assert '<recalled_memory trust="untrusted">' in projected
    assert "我喜欢吃大蒜" in projected


@pytest.mark.asyncio
async def test_adk_platform_memory_is_projected_via_request_instructions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADK 不消费独立 memory_context，平台记忆必须投影到本轮 instructions。"""
    memory_db = tmp_path / "adk-memory.db"
    monkeypatch.setenv("KSADK_MEMORY_FLUSH_ENABLED", "1")
    monkeypatch.setenv("KSADK_MEMORY_DB_PATH", str(memory_db))
    calls: list[tuple[str, Any]] = []
    registry = RuntimeRegistry()
    registry.register("adk", lambda _context: _RecordingAdapter(calls, "adk"))
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(registry))
    spec = StudioRunSpec(
        launch_context=RuntimeLaunchContext(runtime_type="adk", project_dir=tmp_path),
        build_id="build-adk-memory",
        agent_id="adk-memory-agent",
        request_config={
            "instructions": "遵守 Agent 的既有行为规则。",
            "memory_write_rollout": "enabled",
            "memory_enabled": True,
            "memory_recall_enabled": True,
            "memory_write_mode": "candidate",
            "flush_before_compaction": True,
            "provider_ref": "local-default",
        },
    )

    first = await service.run(spec, "记住我喜欢吃大蒜", session_id="ses-adk-write")
    second = await service.run(spec, "我喜欢吃什么", session_id="ses-adk-recall")

    assert "memory.flush.completed" in [
        event.type for event in service.event_store.events(first.id)
    ]
    second_events = [event.type for event in service.event_store.events(second.id)]
    assert "memory.recall.completed" in second_events
    assert "memory.recall.projected" in second_events
    start_requests = [value for name, value in calls if name == "start"]
    assert len(start_requests) == 2
    projected = start_requests[1].config["instructions"]
    assert "遵守 Agent 的既有行为规则" in projected
    assert '<recalled_memory trust="untrusted">' in projected
    assert "我喜欢吃大蒜" in projected


@pytest.mark.asyncio
async def test_platform_memory_is_isolated_by_agent_and_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一 Studio 用户创建的新 Agent 默认不能召回其他 Agent 的长期记忆。"""
    memory_db = tmp_path / "isolated-memory.db"
    monkeypatch.setenv("KSADK_MEMORY_FLUSH_ENABLED", "1")
    monkeypatch.setenv("KSADK_MEMORY_DB_PATH", str(memory_db))
    calls: list[tuple[str, Any]] = []
    registry = RuntimeRegistry()
    registry.register("adk", lambda _context: _RecordingAdapter(calls, "adk"))
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()
    service = StudioRunService(workspace, RuntimeExecutor(registry))

    def spec(agent_id: str) -> StudioRunSpec:
        return StudioRunSpec(
            launch_context=RuntimeLaunchContext(runtime_type="adk", project_dir=tmp_path),
            build_id=f"build-{agent_id}",
            agent_id=agent_id,
            request_config={
                "memory_write_rollout": "enabled",
                "memory_enabled": True,
                "memory_recall_enabled": True,
                "memory_write_mode": "candidate",
                "flush_before_compaction": True,
                "provider_ref": "local-default",
            },
        )

    await service.run(spec("agent-a"), "记住我喜欢吃大蒜", session_id="ses-a-write")
    other = await service.run(spec("agent-b"), "我喜欢吃什么", session_id="ses-b-recall")
    own = await service.run(spec("agent-a"), "我喜欢吃什么", session_id="ses-a-recall")

    other_types = [event.type for event in service.event_store.events(other.id)]
    own_types = [event.type for event in service.event_store.events(own.id)]
    assert "memory.recall.empty" in other_types
    assert "memory.recall.projected" not in other_types
    assert "memory.recall.completed" in own_types
    assert "memory.recall.projected" in own_types
    start_requests = [value for name, value in calls if name == "start"]
    assert '<recalled_memory trust="untrusted">' not in start_requests[1].config["instructions"]
    assert "我喜欢吃大蒜" in start_requests[2].config["instructions"]

    with sqlite3.connect(memory_db) as connection:
        rows = connection.execute("SELECT scope, scope_id FROM memory_records").fetchall()
    assert rows == [("user", "agent:agent-a:user:local-user")]


@pytest.mark.asyncio
async def test_framework_owned_studio_turn_does_not_flush_platform_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_db = tmp_path / "framework-memory.db"
    # framework 路径不设 KSADK_MEMORY_FLUSH_ENABLED，不设 memory_write_rollout=enabled
    monkeypatch.setenv("KSADK_MEMORY_DB_PATH", str(memory_db))
    registry = RuntimeRegistry()
    registry.register("langgraph", lambda _context: _RecordingAdapter([]))
    workspace = Workspace(tmp_path / "workspace")
    workspace.initialize()

    await StudioRunService(workspace, RuntimeExecutor(registry)).run(
        StudioRunSpec(
            launch_context=RuntimeLaunchContext(
                runtime_type="langgraph",
                project_dir=tmp_path,
            ),
            build_id="build-framework",
            agent_id="memory-agent",
        ),
        "请记住我的部署偏好：始终先执行 dry-run",
        session_id="ses-framework",
    )

    # framework 路径不 flush：DB 可能被 ambient recall 创建，但无新写入
    import sqlite3 as _sqlite3

    if memory_db.exists():
        conn = _sqlite3.connect(memory_db)
        try:
            rows = list(
                conn.execute("SELECT content FROM memory_records WHERE content LIKE '%dry-run%'")
            )
            assert len(rows) == 0, f"framework 路径不应写入记忆: {rows}"
        except _sqlite3.OperationalError:
            pass  # no table = no flush
        finally:
            conn.close()


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
                invocation_id=self.invocation_id,
                seq_id=1,
                trace_id=self.trace_id,
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
    assert (await service.events(record.id))[-1].type == "run.cancelled"
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
                invocation_id=self.invocation_id,
                seq_id=1,
                trace_id=self.trace_id,
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
                invocation_id=self.invocation_id,
                seq_id=1,
                trace_id=self.trace_id,
                payload={"status": "attach_unavailable"},
            )

    calls: list[tuple[str, Any]] = []
    registry = RuntimeRegistry()
    registry.register("codex", lambda _context: _InterruptedAdapter(calls, "codex"))
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
        ),
        "继续运行",
        session_id="ses-interrupted",
    )

    assert record.status == "INTERRUPTED"
    assert record.error == {
        "code": "RUN_INTERRUPTED",
        "message": "attach_unavailable",
    }
    assert (await service_event_types(service, record.id))[-1] == "run.interrupted"


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
                invocation_id=self.invocation_id,
                seq_id=self.turn * 10,
                trace_id=self.trace_id,
                payload={"status": "in_progress"},
            )
            if self.turn == 1:
                await self.interrupt.wait()
                yield RuntimeEvent.create(
                    EventType.RUN_INTERRUPTED,
                    agent_id="review-helper",
                    user_id="local-user",
                    session_id=handle.session_id,
                    invocation_id=self.invocation_id,
                    seq_id=self.turn * 10 + 1,
                    trace_id=self.trace_id,
                    payload={"status": "paused", "reason": "user_pause"},
                )
                return
            yield RuntimeEvent.create(
                EventType.TEXT_COMPLETED,
                agent_id="review-helper",
                user_id="local-user",
                session_id=handle.session_id,
                invocation_id=self.invocation_id,
                seq_id=self.turn * 10 + 1,
                trace_id=self.trace_id,
                phase="final_answer",
                payload={"text": "resumed answer"},
            )
            yield RuntimeEvent.create(
                EventType.RUN_COMPLETED,
                agent_id="review-helper",
                user_id="local-user",
                session_id=handle.session_id,
                invocation_id=self.invocation_id,
                seq_id=self.turn * 10 + 2,
                trace_id=self.trace_id,
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
    event_types = await service_event_types(service, record.id)
    assert event_types.count("run.paused") == 1
    assert "run.resumed" in event_types
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
                invocation_id=self.invocation_id,
                seq_id=1,
                trace_id=self.trace_id,
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
                invocation_id=self.invocation_id,
                seq_id=2,
                trace_id=self.trace_id,
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
                invocation_id=self.invocation_id,
                seq_id=3,
                trace_id=self.trace_id,
                phase="final_answer",
                payload={"text": "已按选择继续"},
            )
            yield RuntimeEvent.create(
                EventType.RUN_COMPLETED,
                agent_id="review-helper",
                user_id="local-user",
                session_id=handle.session_id,
                invocation_id=self.invocation_id,
                seq_id=4,
                trace_id=self.trace_id,
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
    assert "a2ui.action" in await service_event_types(service, run.id)


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


async def service_event_types(service: StudioRunService, run_id: str) -> list[str]:
    return [event.type for event in await service.events(run_id)]


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
