from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from ksadk.events.runtime_event import RuntimeEvent
from ksadk.runtime import (
    BaseRuntime,
    CancelResult,
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
from ksadk.server.composition import configure_runtime_app
from ksadk.server.factory import RuntimeAppConfig, create_runtime_app
from ksadk.sessions.in_memory import InMemorySessionService


class _Runtime(BaseRuntime):
    runtime_type = "fixture"

    def native_capabilities(self) -> dict:
        return {"CancelRun": {"Supported": True}}


class _CancellableAdapter(RuntimeAdapter):
    def __init__(self) -> None:
        super().__init__(_Runtime())
        self.cancel_requests: list[str] = []

    async def start(self, request: StartRequest) -> RunHandle:
        return RunHandle(
            run_id=str(request.metadata["invocation_id"]),
            session_id=request.session_id,
            runtime_type="fixture",
        )

    async def stream(self, _handle: RunHandle) -> AsyncIterator[RuntimeEvent]:
        if False:
            yield RuntimeEvent.model_construct()

    async def cancel(self, handle: RunHandle) -> CancelResult:
        self.cancel_requests.append(handle.run_id)
        return CancelResult.INTERRUPTED_ACTIVE_TURN

    async def resume(
        self,
        handle: RunHandle,
        _target: ResumeTarget,
        _payload: ResumePayload | None,
    ) -> RunHandle:
        return handle

    async def checkpoint(self, _handle: RunHandle) -> CheckpointDescriptor:
        raise NotImplementedError

    async def close(self, _handle: RunHandle) -> None:
        return None


def _runtime_app() -> tuple[object, _CancellableAdapter, RuntimeExecutor, RuntimeLaunchContext]:
    adapter = _CancellableAdapter()
    registry = RuntimeRegistry()
    registry.register("fixture", lambda _context: adapter)
    executor = RuntimeExecutor(registry)
    context = RuntimeLaunchContext(
        runtime_type="fixture",
        project_dir=".",
        detection=type("Detection", (), {"name": "demo-agent"})(),
    )
    service = InMemorySessionService()
    app = create_runtime_app(
        RuntimeAppConfig(
            runtime_executor=executor,
            launch_context=context,
            route_groups={"control"},
            session_service_provider=lambda: service,
        ),
        configure_runtime_app,
    )
    return app, adapter, executor, context


@pytest.mark.asyncio
async def test_cancel_run_reports_not_running_without_owned_runtime_handle() -> None:
    app, _adapter, _executor, _context = _runtime_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/CancelRun",
            json={"AgentId": "demo-agent", "InvocationId": "inv-not-running"},
        )

    assert response.status_code == 200
    assert response.json()["Data"] == {
        "Cancelled": False,
        "Found": False,
        "Status": "not_running",
        "RuntimeCancelStatus": "not_running",
    }


@pytest.mark.asyncio
async def test_cancel_run_targets_executor_owned_runtime_adapter() -> None:
    app, adapter, executor, context = _runtime_app()
    service = app.state.runtime.resolve_session_service()
    await service.create_session("demo-agent", "user-1", "sess-cancel-long-task")
    await executor.start(
        context,
        StartRequest(
            input="start",
            user_id="user-1",
            session_id="sess-cancel-long-task",
            agent_id="demo-agent",
            metadata={"invocation_id": "inv-cancel-long-task"},
        ),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ksadk.local") as client:
        response = await client.post(
            "/agentengine/api/v1/CancelRun",
            json={
                "AgentId": "demo-agent",
                "UserId": "user-1",
                "SessionId": "sess-cancel-long-task",
                "InvocationId": "inv-cancel-long-task",
            },
        )

    assert response.status_code == 200
    assert response.json()["Data"] == {
        "Cancelled": True,
        "Found": True,
        "Status": "cancelling",
        "RuntimeCancelStatus": "interrupted_active_turn",
    }
    assert adapter.cancel_requests == ["inv-cancel-long-task"]
