from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ksadk.events.canonical import (
    ContentSnapshot,
    ItemCompleted,
    ItemStarted,
    OutputRef,
    RunCompleted,
    RunStarted,
    RuntimeEvent,
    SourceRef,
)
from ksadk.events.content import TextContent
from ksadk.events.identity import stable_event_id, stable_item_id, stable_scope_id
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
        framework = "ksadk"
        run_id = handle.run_id
        scope_id = stable_scope_id(framework, run_id)
        run_item_id = stable_item_id(framework, run_id, "$run")
        message_item_id = stable_item_id(framework, run_id, "message", "final_answer")
        source = SourceRef(
            framework=framework,
            native_run_id=run_id,
            metadata={
                "agent_id": "agent-1",
                "user_id": "user-1",
                "session_id": handle.session_id,
                "invocation_id": run_id,
            },
        )
        yield RunStarted(
            schema_version=2,
            event_id=stable_event_id(
                framework, scope_id, run_item_id, "run.started", "run", run_id, 0
            ),
            seq=1,
            timestamp=1.0,
            run_id=run_id,
            scope_id=scope_id,
            source=source,
            status="running",
        )
        # ItemStarted must precede ItemCompleted for the reducer invariant.
        yield ItemStarted(
            schema_version=2,
            event_id=stable_event_id(
                framework, scope_id, message_item_id, "item.started", "text-0", run_id, 0
            ),
            seq=2,
            timestamp=2.0,
            run_id=run_id,
            scope_id=scope_id,
            source=source,
            item_id=message_item_id,
            item_kind="message",
            phase="final_answer",
            initial=None,
        )
        yield ItemCompleted(
            schema_version=2,
            event_id=stable_event_id(
                framework, scope_id, message_item_id, "item.completed", "text-0", run_id, 0
            ),
            seq=3,
            timestamp=3.0,
            run_id=run_id,
            scope_id=scope_id,
            source=source,
            item_id=message_item_id,
            item_kind="message",
            snapshot=ContentSnapshot(parts=(TextContent(part_id="text-0", text="answer"),)),
        )
        yield RunCompleted(
            schema_version=2,
            event_id=stable_event_id(
                framework, scope_id, run_item_id, "run.completed", "run", run_id, 0
            ),
            seq=4,
            timestamp=4.0,
            run_id=run_id,
            scope_id=scope_id,
            source=source.model_copy(
                update={"metadata": {**source.metadata, "duration_ms": 12}}
            ),
            status="completed",
            output_refs=(
                OutputRef(
                    scope_id=scope_id, item_id=message_item_id, part_id="text-0"
                ),
            ),
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
        "run.started",
        "item.started",
        "item.completed",
        "run.completed",
    ]
    assert [event.event_type for event in stored].count("user_message") == 1
    assert any(event.event_type == "item.completed" for event in stored)
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
    assert executor.find_handle(
        "fixture", request.metadata["invocation_id"], request.session_id
    ) is None


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
    assert {event.run_id for event in events} == {"original-run"}
