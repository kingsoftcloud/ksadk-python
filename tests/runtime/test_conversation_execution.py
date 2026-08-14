from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.runtime import (
    CONVERSATION_PREPROCESSING_METADATA_KEY,
    BaseRuntime,
    CancelResult,
    ResumePayload,
    ResumeTarget,
    RunHandle,
    RuntimeAdapter,
    RuntimeExecutor,
    RuntimeLaunchContext,
    RuntimeRegistry,
    StartRequest,
)
from ksadk.runtime.conversation_execution import (
    _resolve_runtime_prompt_config,
    invoke_runtime_conversation_once,
    iter_runtime_conversation_events,
)
from ksadk.sessions.in_memory import InMemorySessionService


class _Runtime(BaseRuntime):
    runtime_type = "fixture"

    def native_capabilities(self) -> dict[str, object]:
        return {}


class _Adapter(RuntimeAdapter):
    def __init__(self) -> None:
        super().__init__(_Runtime())
        self.requests: list[StartRequest] = []
        self.closed: list[RunHandle] = []
        self.resumes: list[tuple[RunHandle, ResumeTarget, ResumePayload | None]] = []

    async def start(self, request: StartRequest) -> RunHandle:
        self.requests.append(request)
        return RunHandle(
            run_id=str(request.metadata["invocation_id"]),
            session_id=request.session_id,
            runtime_type="fixture",
        )

    async def stream(self, handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        common = {
            "agent_id": "agent-1",
            "user_id": "user-1",
            "session_id": handle.session_id,
            "invocation_id": handle.run_id,
        }
        yield RuntimeEvent.create(
            EventType.RUN_STARTED,
            seq_id=1,
            payload={"status": "in_progress"},
            **common,
        )
        yield RuntimeEvent.create(
            EventType.TEXT_COMPLETED,
            seq_id=2,
            phase="final_answer",
            payload={"text": "answer"},
            **common,
        )
        yield RuntimeEvent.create(
            EventType.RUN_COMPLETED,
            seq_id=3,
            payload={"status": "completed", "duration_ms": 12},
            **common,
        )

    async def cancel(self, _handle: RunHandle) -> CancelResult:
        return CancelResult.NOT_RUNNING

    async def resume(self, handle, target, payload):
        self.resumes.append((handle, target, payload))
        return handle

    async def checkpoint(self, _handle):
        raise NotImplementedError

    async def close(self, handle: RunHandle) -> None:
        self.closed.append(handle)


@pytest.mark.asyncio
async def test_runtime_conversation_prepares_once_persists_and_closes_terminal_run() -> None:
    service = InMemorySessionService()
    adapter = _Adapter()
    registry = RuntimeRegistry()
    registry.register("fixture", lambda _context: adapter)
    executor = RuntimeExecutor(registry)
    context = RuntimeLaunchContext(runtime_type="fixture", project_dir=".")

    events = [
        event
        async for event in iter_runtime_conversation_events(
            executor=executor,
            launch_context=context,
            agent_id="agent-1",
            user_id="user-1",
            messages=[{"role": "user", "content": "hello"}],
            session_id=None,
            model=None,
            session_service_provider=lambda: service,
        )
    ]

    request = adapter.requests[0]
    prepared = request.metadata[CONVERSATION_PREPROCESSING_METADATA_KEY]["prepared_turn"]
    stored = await service.get_events(request.session_id)
    session = await service.get_session(request.session_id)

    assert prepared["session_id"] == request.session_id
    assert prepared["user_input"] == "hello"
    assert [event.event_type for event in events] == [
        EventType.RUN_STARTED,
        EventType.TEXT_COMPLETED,
        EventType.RUN_COMPLETED,
    ]
    assert [event.event_type for event in stored].count("user_message") == 1
    assert any(event.event_type == EventType.TEXT_COMPLETED for event in stored)
    assert session is not None
    assert session.summary == "answer"
    assert session.state["active_run"] == {
        "invocation_id": request.metadata["invocation_id"],
        "status": "completed",
        "run_mode": "foreground",
        "run_trigger": "new_run",
    }
    assert adapter.closed == [
        RunHandle(
            run_id=request.metadata["invocation_id"],
            session_id=request.session_id,
            runtime_type="fixture",
        )
    ]
    assert (
        executor.find_handle("fixture", request.metadata["invocation_id"], request.session_id)
        is None
    )


@pytest.mark.asyncio
async def test_invoke_runtime_conversation_collects_canonical_result() -> None:
    service = InMemorySessionService()
    registry = RuntimeRegistry()
    registry.register("fixture", lambda _context: _Adapter())

    session_id, result = await invoke_runtime_conversation_once(
        executor=RuntimeExecutor(registry),
        launch_context=RuntimeLaunchContext(runtime_type="fixture", project_dir="."),
        agent_id="agent-1",
        user_id="user-1",
        messages=[{"role": "user", "content": "hello"}],
        session_id=None,
        model="fixture-model",
        session_service_provider=lambda: service,
    )

    assert session_id
    assert result["output_text"] == "answer"
    assert result["metadata"]["runtime"] == {
        "duration_ms": 12,
        "runtime_type": "fixture",
    }


@pytest.mark.asyncio
async def test_checkpoint_resume_reuses_owned_handle_and_calls_executor_resume() -> None:
    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", "session-1")
    adapter = _Adapter()
    registry = RuntimeRegistry()
    registry.register("fixture", lambda _context: adapter)
    executor = RuntimeExecutor(registry)
    context = RuntimeLaunchContext(runtime_type="fixture", project_dir=".")
    original = await executor.start(
        context,
        StartRequest(
            input="initial",
            user_id="user-1",
            session_id="session-1",
            agent_id="agent-1",
            metadata={"invocation_id": "original-run"},
        ),
    )

    events = [
        event
        async for event in iter_runtime_conversation_events(
            executor=executor,
            launch_context=context,
            agent_id="agent-1",
            user_id="user-1",
            messages=[],
            session_id="session-1",
            model=None,
            resume_input={
                "type": "agentengine.resume_checkpoint",
                "run_id": "original-run",
                "checkpoint_id": "checkpoint-1",
                "resume_attempt_id": "resume-1",
                "framework": "langgraph",
                "framework_ref": {
                    "langgraph": {
                        "checkpoint_id": "checkpoint-1",
                        "thread_id": "session-1",
                    }
                },
            },
            invocation_id="resume-1",
            session_service_provider=lambda: service,
        )
    ]

    assert adapter.requests == [
        StartRequest(
            input="initial",
            user_id="user-1",
            session_id="session-1",
            agent_id="agent-1",
            metadata={"invocation_id": "original-run"},
        )
    ]
    assert adapter.resumes == [
        (
            original,
            ResumeTarget(kind="checkpoint_id", id="checkpoint-1"),
            None,
        )
    ]
    assert {event.invocation_id for event in events} == {"original-run"}


@pytest.mark.asyncio
async def test_canonical_path_attaches_single_shadow_plan_with_correct_ownership() -> None:
    """Shadow 基线验收：canonical 路径 prepared_turn 携带且仅携带一份 shadow plan，
    ownership 由 launch_context.runtime_type 解析（非 opaque），不重复规划。"""

    class _LangGraphAdapter(_Adapter):
        async def start(self, request: StartRequest) -> RunHandle:
            self.requests.append(request)
            return RunHandle(
                run_id=str(request.metadata["invocation_id"]),
                session_id=request.session_id,
                runtime_type="langgraph",
            )

    service = InMemorySessionService()
    adapter = _LangGraphAdapter()
    registry = RuntimeRegistry()
    registry.register("langgraph", lambda _context: adapter)
    executor = RuntimeExecutor(registry)
    context = RuntimeLaunchContext(runtime_type="langgraph", project_dir=".")

    events = [
        event
        async for event in iter_runtime_conversation_events(
            executor=executor,
            launch_context=context,
            agent_id="agent-1",
            user_id="user-1",
            messages=[{"role": "user", "content": "帮我做个总结"}],
            session_id=None,
            model=None,
            instructions="你是助手",
            session_service_provider=lambda: service,
        )
    ]
    assert [event.event_type for event in events] == [
        EventType.RUN_STARTED,
        EventType.TEXT_COMPLETED,
        EventType.RUN_COMPLETED,
    ]

    request = adapter.requests[0]
    prepared = request.metadata[CONVERSATION_PREPROCESSING_METADATA_KEY]["prepared_turn"]
    plan = prepared["shadow_context_plan"]
    # 单一 plan，且 ownership 来自 runtime_type（langgraph → estimated，非 opaque）。
    assert plan is not None
    assert plan["accounting_accuracy"] == "estimated"
    assert plan["runtime_type"] == "langgraph"
    assert plan["capability_hash"].startswith("sha256:")
    # prepared_turn 经 asdict 序列化进 metadata 后仍是同一份 plan（未重复生成）。
    assert plan["plan_id"].startswith("ctxplan_")
    # shadow plan 未泄漏进 runner 实际消费的 payload 字段。
    assert "shadow_context_plan" not in request.input
    assert request.input == "帮我做个总结"


def test_resolve_runtime_prompt_config_prefers_nested_build_contract() -> None:
    resolved = _resolve_runtime_prompt_config(
        {
            "prompt": "legacy system",
            "prompt_integration_mode": "framework_assisted",
            "context": {
                "prompt_ownership": "ksadk",
                "agent_system": "cloud system",
                "agent_task": "cloud task",
            },
        }
    )

    assert resolved == {
        "agent_system": "cloud system",
        "agent_task": "cloud task",
        "prompt_integration_mode": "ksadk_hosted",
    }


@pytest.mark.asyncio
async def test_canonical_cloud_path_propagates_prompt_and_deployment_contract() -> None:
    class _LangGraphAdapter(_Adapter):
        async def start(self, request: StartRequest) -> RunHandle:
            self.requests.append(request)
            return RunHandle(
                run_id=str(request.metadata["invocation_id"]),
                session_id=request.session_id,
                runtime_type="langgraph",
            )

    service = InMemorySessionService()
    adapter = _LangGraphAdapter()
    registry = RuntimeRegistry()
    registry.register("langgraph", lambda _context: adapter)
    context = RuntimeLaunchContext(
        runtime_type="langgraph",
        project_dir=".",
        deployment_mode="ksadk_managed_cloud",
        config={
            "context": {
                "prompt_ownership": "ksadk",
                "agent_system": "You are the cloud canary.",
                "agent_task": "Answer deployment checks.",
            }
        },
    )

    _ = [
        event
        async for event in iter_runtime_conversation_events(
            executor=RuntimeExecutor(registry),
            launch_context=context,
            agent_id="agent-1",
            user_id="user-1",
            messages=[{"role": "user", "content": "health check"}],
            session_id=None,
            model=None,
            session_service_provider=lambda: service,
        )
    ]

    request = adapter.requests[0]
    prepared = request.metadata[CONVERSATION_PREPROCESSING_METADATA_KEY]["prepared_turn"]
    assert request.config["context"]["prompt_ownership"] == "ksadk"
    assert prepared["user_id"] == "user-1"
    assert prepared["agent_id"] == "agent-1"
    assert prepared["prompt_integration_mode"] == "ksadk_hosted"
    compiled_content = prepared["compiled_prompt"]["prompt_content"]
    assert "<agent_identity>\nYou are the cloud canary." in compiled_content
    assert "<agent_policy>\nAnswer deployment checks." in compiled_content
    assert prepared["shadow_context_plan"]["deployment_mode"] == ("ksadk_managed_cloud")


@pytest.mark.asyncio
async def test_turn_memory_is_written_to_user_scope_for_cross_session_recall(
    tmp_path, monkeypatch
) -> None:
    from ksadk.memory.models import MemorySearchRequest
    from ksadk.memory.providers.local_sqlite import SqliteMemoryProvider

    memory_path = tmp_path / "memory.db"
    monkeypatch.setenv("KSADK_MEMORY_FLUSH_ENABLED", "true")
    monkeypatch.setenv("KSADK_MEMORY_DB_PATH", str(memory_path))
    service = InMemorySessionService()
    registry = RuntimeRegistry()
    registry.register("fixture", lambda _context: _Adapter())

    session_id, _ = await invoke_runtime_conversation_once(
        executor=RuntimeExecutor(registry),
        launch_context=RuntimeLaunchContext(runtime_type="fixture", project_dir="."),
        agent_id="agent-1",
        user_id="user-1",
        messages=[{"role": "user", "content": "记住：默认使用 Python 3.12"}],
        session_id=None,
        model=None,
        session_service_provider=lambda: service,
    )

    provider = SqliteMemoryProvider(db_path=memory_path)
    from ksadk.memory.coordinator import agent_user_scope_id

    result = provider.search(
        MemorySearchRequest(
            query="Python",
            scopes=[
                (
                    "user",
                    agent_user_scope_id(agent_id="agent-1", user_id="user-1"),
                )
            ],
            memory_types=["profile", "fact", "episode"],
        )
    )
    assert result.status == "ok"
    assert any("Python 3.12" in record.content for record in result.records)
    assert all(record.scope_id != session_id for record in result.records)


@pytest.mark.asyncio
async def test_checkpoint_resume_keeps_single_plan_and_preserves_run_target() -> None:
    """Checkpoint resume 只保留一份 shadow plan，并调用 executor.resume。"""

    class _LangGraphAdapter(_Adapter):
        async def start(self, request: StartRequest) -> RunHandle:
            self.requests.append(request)
            return RunHandle(
                run_id=str(request.metadata["invocation_id"]),
                session_id=request.session_id,
                runtime_type="langgraph",
            )

    service = InMemorySessionService()
    await service.create_session("agent-1", "user-1", "session-resume-plan")
    adapter = _LangGraphAdapter()
    registry = RuntimeRegistry()
    registry.register("langgraph", lambda _context: adapter)
    executor = RuntimeExecutor(registry)
    context = RuntimeLaunchContext(runtime_type="langgraph", project_dir=".")
    original = await executor.start(
        context,
        StartRequest(
            input="initial",
            user_id="user-1",
            session_id="session-resume-plan",
            agent_id="agent-1",
            metadata={"invocation_id": "original-run"},
        ),
    )

    events = [
        event
        async for event in iter_runtime_conversation_events(
            executor=executor,
            launch_context=context,
            agent_id="agent-1",
            user_id="user-1",
            messages=[],
            session_id="session-resume-plan",
            model=None,
            resume_input={
                "type": "agentengine.resume_checkpoint",
                "run_id": "original-run",
                "checkpoint_id": "checkpoint-1",
                "resume_attempt_id": "resume-1",
                "framework": "langgraph",
                "framework_ref": {
                    "langgraph": {
                        "checkpoint_id": "checkpoint-1",
                        "thread_id": "session-resume-plan",
                    }
                },
            },
            invocation_id="resume-1",
            session_service_provider=lambda: service,
        )
    ]
    # resume 走 executor.resume（保留 checkpoint 恢复目标），不再 start 第二份 run。
    assert adapter.resumes == [
        (original, ResumeTarget(kind="checkpoint_id", id="checkpoint-1"), None)
    ]
    assert {event.invocation_id for event in events} == {"original-run"}
