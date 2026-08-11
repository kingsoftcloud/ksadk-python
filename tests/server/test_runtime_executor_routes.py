from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

from ksadk.conversations.runtime_persistence import append_run_checkpoint_event
from ksadk.events.runtime_event import EventType, RuntimeEvent
from ksadk.runtime import (
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
from ksadk.server.composition import configure_runtime_app
from ksadk.server.factory import RuntimeAppConfig, create_runtime_app
from ksadk.sessions.in_memory import InMemorySessionService


class _Runtime(BaseRuntime):
    runtime_type = "fixture"

    def native_capabilities(self) -> dict[str, object]:
        return {
            "CancelRun": {"Supported": True},
            "ResumeRun": {"Supported": True},
            "Checkpoint": {"Supported": True, "Granularity": "snapshot"},
        }


class _Adapter(RuntimeAdapter):
    def __init__(self) -> None:
        super().__init__(_Runtime())
        self.requests: list[StartRequest] = []
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
            "agent_id": "fixture-agent",
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
            payload={"text": "real adapter answer"},
            **common,
        )
        yield RuntimeEvent.create(
            EventType.RUN_COMPLETED,
            seq_id=3,
            payload={"status": "completed", "duration_ms": 3},
            **common,
        )

    async def cancel(self, _handle):
        return CancelResult.NOT_RUNNING

    async def resume(self, handle, target, payload):
        self.resumes.append((handle, target, payload))
        return handle

    async def checkpoint(self, _handle):
        raise NotImplementedError

    async def close(self, _handle):
        return None


class _CancellableAdapter(_Adapter):
    def __init__(self) -> None:
        super().__init__()
        self.cancelled_run_ids: list[str] = []

    async def cancel(self, handle: RunHandle) -> CancelResult:
        self.cancelled_run_ids.append(handle.run_id)
        return CancelResult.INTERRUPTED_ACTIVE_TURN


def test_openai_responses_route_executes_runtime_adapter_without_runner() -> None:
    adapter = _Adapter()
    registry = RuntimeRegistry()
    registry.register("fixture", lambda _context: adapter)
    service = InMemorySessionService()
    context = RuntimeLaunchContext(
        runtime_type="fixture",
        project_dir=".",
        detection=type("Detection", (), {"name": "fixture-agent"})(),
    )
    app = create_runtime_app(
        RuntimeAppConfig(
            runtime_executor=RuntimeExecutor(registry),
            launch_context=context,
            route_groups={"openai_compat"},
            session_service_provider=lambda: service,
        ),
        configure_runtime_app,
    )

    response = TestClient(app).post(
        "/v1/responses",
        json={
            "input": "hello",
            "user": "user-1",
            "model": "fixture-model",
            "stream": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["output_text"] == "real adapter answer"
    assert adapter.requests[0].agent_id == "fixture-agent"
    assert adapter.requests[0].model == "fixture-model"


def test_runtime_app_state_owns_executor_and_has_no_runner_slot() -> None:
    app = _runtime_only_run_app(_Adapter())

    assert isinstance(app.state.runtime.executor, RuntimeExecutor)
    assert not hasattr(app.state.runtime, "runner")
    assert not hasattr(app.state.runtime, "runner_loaded")


def test_ui_bootstrap_uses_launch_context_and_runtime_capabilities() -> None:
    registry = RuntimeRegistry()
    registry.register("fixture", lambda _context: _Adapter())
    app = create_runtime_app(
        RuntimeAppConfig(
            runtime_executor=RuntimeExecutor(registry),
            launch_context=RuntimeLaunchContext(
                runtime_type="fixture",
                project_dir=".",
                detection=type(
                    "Detection",
                    (),
                    {"name": "fixture-agent", "description": "Fixture runtime"},
                )(),
            ),
            route_groups={"ui_bootstrap"},
            session_service_provider=InMemorySessionService,
        ),
        configure_runtime_app,
    )

    response = TestClient(app).post(
        "/agentengine/api/v1/GetAgentUiBootstrap",
        json={},
    )

    assert response.status_code == 200
    data = response.json()["Data"]
    assert data["Agent"] == {
        "AgentId": "fixture-agent",
        "Name": "fixture-agent",
        "Description": "Fixture runtime",
        "Framework": "fixture",
    }
    assert data["Capabilities"]["StopRun"] is True
    assert data["Capabilities"]["ResumeRun"] is True


def test_openai_responses_stream_stays_attached_to_app_owned_runtime() -> None:
    registry = RuntimeRegistry()
    registry.register("fixture", lambda _context: _Adapter())
    service = InMemorySessionService()
    app = create_runtime_app(
        RuntimeAppConfig(
            runtime_executor=RuntimeExecutor(registry),
            launch_context=RuntimeLaunchContext(
                runtime_type="fixture",
                project_dir=".",
                detection=type("Detection", (), {"name": "fixture-agent"})(),
            ),
            route_groups={"openai_compat"},
            session_service_provider=lambda: service,
        ),
        configure_runtime_app,
    )

    response = TestClient(app).post(
        "/v1/responses",
        json={"input": "hello", "user": "user-1", "stream": True},
    )

    assert response.status_code == 200
    assert "event: response.created" in response.text
    assert "event: response.completed" in response.text
    assert "real adapter answer" in response.text


def _runtime_only_run_app(adapter: RuntimeAdapter):
    registry = RuntimeRegistry()
    registry.register("fixture", lambda _context: adapter)
    return create_runtime_app(
        RuntimeAppConfig(
            runtime_executor=RuntimeExecutor(registry),
            launch_context=RuntimeLaunchContext(
                runtime_type="fixture",
                project_dir=".",
                detection=type("Detection", (), {"name": "fixture-agent"})(),
            ),
            route_groups={"run"},
            session_service_provider=InMemorySessionService,
        ),
        configure_runtime_app,
    )


def test_run_agent_action_executes_runtime_adapter_without_runner() -> None:
    adapter = _Adapter()
    app = _runtime_only_run_app(adapter)

    response = TestClient(app).post(
        "/agentengine/api/v1/RunAgent",
        json={
            "AgentId": "fixture-agent",
            "UserId": "user-1",
            "Messages": [{"role": "user", "content": "hello"}],
            "Model": "fixture-model",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["Code"] == 0
    assert payload["Data"]["output_text"] == "real adapter answer"
    assert adapter.requests[0].agent_id == "fixture-agent"
    assert adapter.requests[0].model == "fixture-model"


def test_run_agent_action_streams_standard_responses_from_runtime_adapter() -> None:
    app = _runtime_only_run_app(_Adapter())

    response = TestClient(app).post(
        "/agentengine/api/v1/RunAgent",
        json={
            "AgentId": "fixture-agent",
            "UserId": "user-1",
            "Messages": [{"role": "user", "content": "hello"}],
            "Stream": True,
        },
    )

    assert response.status_code == 200
    assert "event: response.created" in response.text
    assert "event: response.completed" in response.text
    assert "real adapter answer" in response.text


def test_adk_run_sse_projects_runtime_adapter_result_without_runner() -> None:
    adapter = _Adapter()
    app = _runtime_only_run_app(adapter)

    response = TestClient(app).post(
        "/run_sse",
        json={
            "appName": "fixture-agent",
            "userId": "user-1",
            "newMessage": {"role": "user", "parts": [{"text": "hello"}]},
            "streaming": False,
            "model": "fixture-model",
        },
    )

    assert response.status_code == 200
    data_line = next(line for line in response.text.splitlines() if line.startswith("data: "))
    payload = json.loads(data_line.removeprefix("data: "))
    assert payload["author"] == "fixture-agent"
    assert payload["content"] == {
        "role": "model",
        "parts": [{"text": "real adapter answer"}],
    }
    assert adapter.requests[0].model == "fixture-model"


def test_cancel_run_targets_the_executor_owned_runtime_handle() -> None:
    adapter = _CancellableAdapter()
    registry = RuntimeRegistry()
    registry.register("fixture", lambda _context: adapter)
    executor = RuntimeExecutor(registry)
    context = RuntimeLaunchContext(
        runtime_type="fixture",
        project_dir=".",
        detection=type("Detection", (), {"name": "fixture-agent"})(),
    )
    service = InMemorySessionService()
    asyncio.run(service.create_session("fixture-agent", "user-1", "session-1"))
    asyncio.run(
        executor.start(
            context,
            StartRequest(
                input="hello",
                user_id="user-1",
                session_id="session-1",
                agent_id="fixture-agent",
                metadata={"invocation_id": "run-1"},
            ),
        )
    )
    app = create_runtime_app(
        RuntimeAppConfig(
            runtime_executor=executor,
            launch_context=context,
            route_groups={"control"},
            session_service_provider=lambda: service,
        ),
        configure_runtime_app,
    )

    response = TestClient(app).post(
        "/agentengine/api/v1/CancelRun",
        json={
            "AgentId": "fixture-agent",
            "UserId": "user-1",
            "SessionId": "session-1",
            "InvocationId": "run-1",
        },
    )

    assert response.status_code == 200
    assert response.json()["Data"] == {
        "Cancelled": True,
        "Found": True,
        "Status": "cancelling",
        "RuntimeCancelStatus": "interrupted_active_turn",
    }
    assert adapter.cancelled_run_ids == ["run-1"]


def test_resume_run_routes_checkpoint_to_executor_resume() -> None:
    adapter = _Adapter()
    registry = RuntimeRegistry()
    registry.register("fixture", lambda _context: adapter)
    executor = RuntimeExecutor(registry)
    context = RuntimeLaunchContext(
        runtime_type="fixture",
        project_dir=".",
        detection=type("Detection", (), {"name": "fixture-agent"})(),
    )
    service = InMemorySessionService()
    asyncio.run(service.create_session("fixture-agent", "user-1", "session-1"))
    original = asyncio.run(
        executor.start(
            context,
            StartRequest(
                input="initial",
                user_id="user-1",
                session_id="session-1",
                agent_id="fixture-agent",
                metadata={"invocation_id": "run-1"},
            ),
        )
    )
    asyncio.run(
        append_run_checkpoint_event(
            session_id="session-1",
            author="fixture-agent",
            run_id="run-1",
            checkpoint_id="checkpoint-1",
            framework="langgraph",
            framework_ref={
                "langgraph": {
                    "checkpoint_id": "checkpoint-1",
                    "thread_id": "session-1",
                }
            },
            invocation_id="run-1",
            metadata={
                "is_resumable": True,
                "backend": "postgres",
                "scope": "shared",
                "durable": True,
            },
            session_service_provider=lambda: service,
        )
    )
    app = create_runtime_app(
        RuntimeAppConfig(
            runtime_executor=executor,
            launch_context=context,
            route_groups={"control"},
            session_service_provider=lambda: service,
        ),
        configure_runtime_app,
    )

    response = TestClient(app).post(
        "/agentengine/api/v1/ResumeRun",
        json={
            "AgentId": "fixture-agent",
            "UserId": "user-1",
            "SessionId": "session-1",
            "RunId": "run-1",
            "CheckpointId": "checkpoint-1",
        },
    )

    assert response.status_code == 200
    assert response.json()["Data"]["output_text"] == "real adapter answer"
    assert adapter.resumes == [
        (
            original,
            ResumeTarget(kind="checkpoint_id", id="checkpoint-1"),
            None,
        )
    ]
