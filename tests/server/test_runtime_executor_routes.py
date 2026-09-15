from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from ksadk.conversations.runtime_persistence import append_run_checkpoint_event
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
from ksadk.kernel.contracts import RuntimeCapability, RuntimeCapabilityMatrix
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
from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter
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
        framework = "ksadk"
        run_id = handle.run_id
        scope_id = stable_scope_id(framework, run_id)
        run_item_id = stable_item_id(framework, run_id, "$run")
        message_item_id = stable_item_id(framework, run_id, "message", "final_answer")
        source = SourceRef(
            framework=framework,
            native_run_id=run_id,
            metadata={
                "agent_id": "fixture-agent",
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
        # ItemStarted must precede ItemCompleted for the reducer's open-item
        # invariant; otherwise the pipeline would recover as run.failed.
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
            snapshot=ContentSnapshot(
                parts=(TextContent(part_id="text-0", text="real adapter answer"),)
            ),
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
                update={"metadata": {**source.metadata, "duration_ms": 3}}
            ),
            status="completed",
            # Canonical RunCompleted resolves output_text via output_refs;
            # the conversation pipeline's _selected_output_text walks these to
            # the final_answer message item's snapshot part "text-0".
            output_refs=(
                OutputRef(
                    scope_id=scope_id, item_id=message_item_id, part_id="text-0"
                ),
            ),
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


def test_capability_adapter_rejects_stale_entry_from_reused_context_id() -> None:
    created: list[_Adapter] = []
    registry = RuntimeRegistry()

    def factory(_context: RuntimeLaunchContext) -> RuntimeAdapter:
        adapter = _Adapter()
        created.append(adapter)
        return adapter

    registry.register("fixture", factory)
    executor = RuntimeExecutor(registry)
    first_context = RuntimeLaunchContext(runtime_type="fixture", project_dir=".")
    second_context = RuntimeLaunchContext(runtime_type="fixture", project_dir=".")

    first_adapter = executor.capability_adapter(first_context)
    # Deterministically model CPython reusing first_context's released id for
    # second_context without relying on allocator timing in the test process.
    executor._capability_adapters[id(second_context)] = (first_context, first_adapter)

    second_adapter = executor.capability_adapter(second_context)

    assert second_adapter is not first_adapter
    assert second_adapter is created[-1]


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
    assert data["Capabilities"]["RuntimeCapabilityMatrix"] == {
        "schema_version": 1,
        "cancel": {
            "supported": False,
            "mode": "unavailable",
            "reason": "not_implemented",
        },
        "pause": {
            "supported": False,
            "mode": "unavailable",
            "reason": "not_implemented",
        },
        "resume": {
            "supported": False,
            "mode": "unavailable",
            "reason": "not_implemented",
        },
        "submit_interaction": {
            "supported": False,
            "mode": "unavailable",
            "reason": "not_implemented",
        },
        "attach": {
            "supported": False,
            "mode": "unavailable",
            "reason": "not_implemented",
        },
        "steer": {
            "supported": False,
            "mode": "unavailable",
            "reason": "runtime_no_native_steer",
        },
        "inject": {
            "supported": False,
            "mode": "unavailable",
            "reason": "runtime_no_native_inject",
        },
        "checkpoint": {
            "supported": False,
            "mode": "unavailable",
            "reason": "not_implemented",
        },
        "durable_restore": {
            "supported": False,
            "mode": "unavailable",
            "reason": "not_implemented",
        },
        "goal": None,
        "loop": None,
                "plan": None,
                "execution_policy": None,
                "interaction_mode": "unavailable",
        }


def test_ui_bootstrap_reuses_capability_runner_and_exposes_persistence_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[_CapabilityRunner] = []

    class _CapabilityRunner:
        detection_result = type(
            "Detection", (), {"name": "probe-agent", "type": type("Type", (), {"value": "adk"})()}
        )()

        def __init__(self) -> None:
            self.prepare_calls = 0
            self.load_calls = 0
            self.close_calls = 0

        def load_agent(self) -> None:
            self.load_calls += 1

        async def prepare_runtime_capabilities(self) -> None:
            self.prepare_calls += 1

        async def close(self) -> None:
            self.close_calls += 1

        def describe_checkpoint_capability(self) -> dict[str, object]:
            return dict(self.get_runtime_capabilities()["Checkpoint"])

        def get_runtime_capabilities(self) -> dict[str, object]:
            prepared = self.prepare_calls > 0
            return {
                "Framework": "adk",
                "CancelRun": {"Supported": True},
                "Checkpoint": {
                    "Supported": prepared,
                    "Backend": "adk_invocation+postgres",
                    "Scope": "invocation",
                    "Durable": True,
                    "SharedAcrossPods": True,
                    "ResumeMode": "invocation_id",
                    "Reason": "",
                },
                "ResumeRun": {
                    "Supported": prepared,
                    "ResumeMode": "invocation_id",
                    "Reason": "",
                },
            }

    def adapter_factory(_context: RuntimeLaunchContext) -> RunnerRuntimeAdapter:
        runner = _CapabilityRunner()
        created.append(runner)
        return RunnerRuntimeAdapter(runner, runtime_type="adk")

    async def ready_status(*, framework=None, use_cache=True):
        assert framework == "adk"
        return {
            "Session": {
                "Configured": True,
                "Status": "ready",
                "Ready": True,
                "Backend": "postgres",
                "SharedAcrossPods": True,
                "Source": "explicit",
                "ReasonCode": "READY",
                "Reason": "",
            },
            "Checkpoint": {
                "Configured": True,
                "Status": "ready",
                "Ready": True,
                "Backend": "postgres",
                "SharedAcrossPods": True,
                "Source": "explicit",
                "ReasonCode": "READY",
                "Reason": "",
            },
        }

    monkeypatch.setattr(
        "ksadk.sessions.persistence.get_persistence_status", ready_status
    )
    registry = RuntimeRegistry()
    registry.register("adk", adapter_factory)
    app = create_runtime_app(
        RuntimeAppConfig(
            runtime_executor=RuntimeExecutor(registry),
            launch_context=RuntimeLaunchContext(
                runtime_type="adk",
                project_dir=".",
                detection=_CapabilityRunner.detection_result,
            ),
            route_groups={"ui_bootstrap"},
            session_service_provider=InMemorySessionService,
        ),
        configure_runtime_app,
    )

    with TestClient(app) as client:
        first = client.post("/agentengine/api/v1/GetAgentUiBootstrap", json={})
        second = client.post(
            "/agentengine/api/v1/GetAgentUiBootstrap",
            json={"SessionId": "session-without-history"},
        )

    assert first.status_code == second.status_code == 200
    for response in (first, second):
        capabilities = response.json()["Data"]["Capabilities"]
        assert capabilities["ResumeRun"] is True
        assert capabilities["Persistence"]["Ready"] is True
        assert capabilities["CheckpointPersistence"]["Ready"] is True
        assert capabilities["RuntimeCapabilityMatrix"]["resume"]["supported"] is True
    assert len(created) == 1
    assert created[0].prepare_calls == 1
    assert created[0].load_calls == 0
    assert created[0].close_calls == 1


def test_ui_bootstrap_projects_native_runtime_matrix_to_legacy_resume_fields() -> None:
    class _NativeRuntime(BaseRuntime):
        runtime_type = "codex"

        def native_capabilities(self) -> dict[str, object]:
            return {
                "Framework": "codex",
                "resume": "thread_id",
                "checkpoint": "thread_snapshot",
            }

    class _NativeAdapter(_Adapter):
        def __init__(self) -> None:
            RuntimeAdapter.__init__(self, _NativeRuntime())

        def capabilities(self) -> RuntimeCapabilityMatrix:
            unavailable = RuntimeCapability(
                supported=False, mode="unavailable", reason="not_supported"
            )
            return RuntimeCapabilityMatrix(
                cancel=RuntimeCapability(supported=True, mode="native"),
                pause=RuntimeCapability(supported=True, mode="native"),
                resume=RuntimeCapability(supported=True, mode="native"),
                submit_interaction=unavailable,
                attach=unavailable,
                steer=unavailable,
                inject=unavailable,
                checkpoint=RuntimeCapability(supported=True, mode="native"),
                durable_restore=unavailable,
                interaction_mode="live_submit",
            )

    adapter = _NativeAdapter()
    registry = RuntimeRegistry()
    registry.register("codex", lambda _context: adapter)
    app = create_runtime_app(
        RuntimeAppConfig(
            runtime_executor=RuntimeExecutor(registry),
            launch_context=RuntimeLaunchContext(
                runtime_type="codex",
                project_dir=".",
                detection=type("Detection", (), {"name": "codex-agent"})(),
            ),
            route_groups={"ui_bootstrap"},
            session_service_provider=InMemorySessionService,
        ),
        configure_runtime_app,
    )

    response = TestClient(app).post(
        "/agentengine/api/v1/GetAgentUiBootstrap", json={}
    )

    assert response.status_code == 200
    capabilities = response.json()["Data"]["Capabilities"]
    assert capabilities["ResumeRun"] is True
    assert capabilities["CheckpointResumeCapability"]["ResumeRun"] == {
        "Supported": True,
        "ResumeMode": "native",
        "Reason": "",
    }
    assert capabilities["RuntimeCapabilityMatrix"]["durable_restore"]["supported"] is False


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
