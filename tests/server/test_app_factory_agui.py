from uuid import uuid4

from fastapi.testclient import TestClient

from ksadk.agui.config import AGUIConfig
from ksadk.events.canonical import (
    ContentSnapshot,
    ItemCompleted,
    ItemStarted,
    OutputRef,
    RunCompleted,
    RuntimeEvent,
    SourceRef,
)
from ksadk.events.content import TextContent
from ksadk.events.identity import stable_event_id, stable_item_id, stable_scope_id
from ksadk.runtime.adapter import (
    BaseRuntime,
    CancelResult,
    RunHandle,
    RuntimeAdapter,
    RuntimeLaunchContext,
    RuntimeRegistry,
    StartRequest,
)
from ksadk.runtime.executor import RuntimeExecutor
from ksadk.server.composition import configure_runtime_app
from ksadk.server.factory import RuntimeAppConfig, create_runtime_app


class _Runtime(BaseRuntime):
    runtime_type = "fake"

    def native_capabilities(self):
        return {}


class _Adapter(RuntimeAdapter):
    def __init__(self):
        super().__init__(_Runtime())

    async def start(self, request: StartRequest) -> RunHandle:
        return RunHandle(
            run_id=str(request.metadata["invocation_id"]),
            session_id=request.session_id,
            runtime_type="fake",
            native_ref={
                "agent_id": request.agent_id or "agent",
                "user_id": request.user_id,
            },
        )

    def stream(self, handle):
        async def generate():
            framework = "ksadk"
            run_id = handle.run_id
            scope_id = stable_scope_id(framework, run_id)
            message_item_id = stable_item_id(framework, run_id, "message", "final_answer")
            run_item_id = stable_item_id(framework, run_id, "$run")
            source = SourceRef(
                framework=framework,
                native_run_id=run_id,
                metadata={
                    "agent_id": str(handle.native_ref["agent_id"]),
                    "user_id": str(handle.native_ref["user_id"]),
                    "session_id": handle.session_id,
                    "invocation_id": run_id,
                },
            )
            # ItemStarted must precede ItemCompleted for the reducer's
            # open-item invariant; otherwise the pipeline recovers as
            # run.failed and the assistant text is never surfaced.
            yield ItemStarted(
                schema_version=2,
                event_id=stable_event_id(
                    framework, scope_id, message_item_id, "item.started", "text-0", run_id, 0
                ),
                seq=1,
                timestamp=1.0,
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
                seq=2,
                timestamp=2.0,
                run_id=run_id,
                scope_id=scope_id,
                source=source,
                item_id=message_item_id,
                item_kind="message",
                snapshot=ContentSnapshot(parts=(TextContent(part_id="text-0", text="ok"),)),
            )
            yield RunCompleted(
                schema_version=2,
                event_id=stable_event_id(
                    framework, scope_id, run_item_id, "run.completed", "run", run_id, 0
                ),
                seq=3,
                timestamp=3.0,
                run_id=run_id,
                scope_id=scope_id,
                source=source,
                status="completed",
                output_refs=(
                    OutputRef(
                        scope_id=scope_id, item_id=message_item_id, part_id="text-0"
                    ),
                ),
            )

        return generate()

    async def cancel(self, _handle):
        return CancelResult.NOT_RUNNING

    async def resume(self, handle, _target, _payload):
        return handle

    async def checkpoint(self, _handle):
        raise NotImplementedError

    async def close(self, _handle):
        return None


def _runtime_execution():
    adapter = _Adapter()
    registry = RuntimeRegistry()
    registry.register("fake", lambda _context: adapter)
    return (
        RuntimeExecutor(registry),
        RuntimeLaunchContext(runtime_type="fake", project_dir="."),
    )


def test_agui_route_is_mounted_before_configured_health_catch_all():
    executor, launch_context = _runtime_execution()
    app = create_runtime_app(
        RuntimeAppConfig(
            runtime_executor=executor,
            launch_context=launch_context,
            agui=AGUIConfig(enabled=True, agent_name="agent"),
            route_groups={"agui", "health_meta"},
        ),
        configure_runtime_app,
    )
    paths = [route.path for route in app.routes if hasattr(route, "path")]
    assert "/agentengine/agui" in paths
    assert paths.index("/agentengine/agui") < paths.index("/{requested_path:path}")
    assert "/agentengine/agui/health" in paths
    assert app.state.runtime.executor is executor
    assert app.state.runtime.launch_context is launch_context


def test_agui_run_is_immediately_available_through_session_message_history():
    session_id = f"thread-history-{uuid4().hex}"
    executor, launch_context = _runtime_execution()
    app = create_runtime_app(
        RuntimeAppConfig(
            runtime_executor=executor,
            launch_context=launch_context,
            agui=AGUIConfig(enabled=True, agent_name="agent"),
            route_groups={"agui", "sessions", "health_meta"},
        ),
        configure_runtime_app,
    )

    with TestClient(app) as client:
        streamed = client.post(
            "/agentengine/agui",
            json={
                "threadId": session_id,
                "runId": "run-history-1",
                "state": {},
                "messages": [{"id": "u1", "role": "user", "content": "hello"}],
                "tools": [],
                "context": [],
                "forwardedProps": {"userId": "user-history-1"},
            },
            headers={"accept": "text/event-stream"},
        )
        history = client.post(
            "/agentengine/api/v1/ListSessionMessages",
            json={
                "AgentId": "agent",
                "UserId": "user-history-1",
                "SessionId": session_id,
                "IncludeToolEvents": True,
            },
        )

    assert streamed.status_code == 200
    assert history.status_code == 200
    assert [
        (message["Role"], message["Content"]["text"])
        for message in history.json()["Data"]["Messages"]
    ] == [("user", "hello"), ("assistant", "ok")]


def test_agui_is_opt_in_and_does_not_change_default_app():
    app = create_runtime_app(RuntimeAppConfig(route_groups={"health_meta"}))
    assert not any(getattr(route, "path", "") == "/agentengine/agui" for route in app.routes)
