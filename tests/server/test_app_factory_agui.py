import asyncio
from uuid import uuid4

import pytest

from fastapi.testclient import TestClient

from ksadk.agui.config import AGUIConfig
from ksadk.events.canonical import (
    ContentSnapshot,
    ItemCompleted,
    ItemStarted,
    OutputRef,
    RunStarted,
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
from ksadk.server.factory import RuntimeAppConfig, bind_runtime_state, create_runtime_app
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.sessions.resilient import ResilientSessionService
from ksadk.sessions.errors import SessionBackendUnavailable


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
    # FastAPI can retain included routers instead of flattening app.routes.
    # Keep their registration order when checking the catch-all boundary.
    paths = [
        nested.path
        for route in app.routes
        for nested in getattr(getattr(route, "original_router", None), "routes", [route])
        if hasattr(nested, "path")
    ]
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


@pytest.mark.asyncio
async def test_factory_agui_cold_hot_and_concurrent_reservation_avoid_full_hydration():
    executor, launch_context = _runtime_execution()
    app = create_runtime_app(
        RuntimeAppConfig(
            runtime_executor=executor, launch_context=launch_context,
            agui=AGUIConfig(enabled=True, agent_name="agent"), route_groups={"agui"},
        ), configure_runtime_app,
    )
    service = InMemorySessionService()
    app.state.runtime.session_service = service
    store_factory = app.state.runtime.agui_agent._shared.event_store_factory
    assert store_factory is not None
    full_reads = 0
    original_get_session = service.get_session

    async def counted_get_session(session_id):
        nonlocal full_reads
        full_reads += 1
        return await original_get_session(session_id)

    service.get_session = counted_get_session

    def event(index):
        return RunStarted(
            schema_version=2, event_id=f"agui-{index}", seq=0, timestamp=float(index+1),
            run_id="run", scope_id="scope",
            source=SourceRef(framework="ksadk", metadata={
                "session_id": "thread", "agent_id": "agent", "user_id": "user",
            }), status="running",
        )

    with bind_runtime_state(app.state.runtime):
        first, created = await asyncio.gather(
            store_factory().reserve_once(event(0)),
            store_factory().reserve_once(event(0)),
        )
        assert sorted([first[1], created[1]]) == [False, True]
        assert first[0].seq == created[0].seq == 1
        for index in range(1, 50):
            persisted = await store_factory().append_one("thread", event(index))
            assert persisted.seq == index + 1
        assert full_reads == 0
        assert len(await service.get_events("thread")) == 50
        with pytest.raises(ValueError, match="different agent or user"):
            await store_factory().reserve_once(event(100).model_copy(update={
                "source": SourceRef(framework="ksadk", metadata={
                    "session_id": "thread", "agent_id": "other", "user_id": "user",
                }),
            }))


@pytest.mark.asyncio
async def test_factory_agui_pending_delete_fails_closed_without_canonical_write():
    executor, launch_context = _runtime_execution()
    app = create_runtime_app(
        RuntimeAppConfig(
            runtime_executor=executor, launch_context=launch_context,
            agui=AGUIConfig(enabled=True, agent_name="agent"), route_groups={"agui"},
        ), configure_runtime_app,
    )
    primary = InMemorySessionService()
    service = ResilientSessionService(primary)
    app.state.runtime.session_service = service
    await primary.create_session("agent", "user", "thread")
    service._deleted_session_ids.add("thread")
    store_factory = app.state.runtime.agui_agent._shared.event_store_factory
    event = RunStarted(
        schema_version=2, event_id="pending-delete", seq=0, timestamp=1.0,
        run_id="run", scope_id="scope",
        source=SourceRef(framework="ksadk", metadata={
            "session_id": "thread", "agent_id": "agent", "user_id": "user",
        }), status="running",
    )
    with bind_runtime_state(app.state.runtime):
        with pytest.raises(SessionBackendUnavailable):
            await store_factory().append_one("thread", event)
        with pytest.raises(SessionBackendUnavailable):
            await store_factory().reserve_once(event)
    assert await primary.get_events("thread") == []
    await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_factory_agui_resilient_cold_miss_race_skips_full_hydration(monkeypatch, kind):
    executor, launch_context = _runtime_execution()
    app = create_runtime_app(
        RuntimeAppConfig(
            runtime_executor=executor, launch_context=launch_context,
            agui=AGUIConfig(enabled=True, agent_name="agent"), route_groups={"agui"},
        ), configure_runtime_app,
    )
    if kind == "memory":
        primary = InMemorySessionService()
    else:
        import os
        from ksadk.sessions.postgres_service import PostgresSessionService

        if not os.environ.get("KSADK_TEST_POSTGRES_DSN"):
            pytest.skip("requires real PostgreSQL; covered by the persistence CI gate")
        primary = PostgresSessionService(
            dsn=os.environ["KSADK_TEST_POSTGRES_DSN"],
            namespace="agui_cold_race_" + uuid4().hex[:12],
        )
    service = ResilientSessionService(primary)
    app.state.runtime.session_service = service
    original_header = service.get_session_header
    original_full = primary.get_session
    full_reads = 0
    first = True

    async def raced_header(session_id):
        nonlocal first
        if first:
            first = False
            await primary.create_session("agent", "user", session_id)
            for index in range(20):
                await primary.append_event(session_id, SessionEvent(
                    id=f"existing-{index}", event_type="user_message",
                ))
            return None
        return await original_header(session_id)

    async def counted_full(session_id):
        nonlocal full_reads
        full_reads += 1
        return await original_full(session_id)

    from ksadk.sessions.base import SessionEvent

    monkeypatch.setattr(service, "get_session_header", raced_header)
    monkeypatch.setattr(primary, "get_session", counted_full)
    event = RunStarted(
        schema_version=2, event_id="cold-race", seq=0, timestamp=1.0,
        run_id="run", scope_id="scope",
        source=SourceRef(framework="ksadk", metadata={
            "session_id": "thread", "agent_id": "agent", "user_id": "user",
        }), status="running",
    )
    factory = app.state.runtime.agui_agent._shared.event_store_factory
    with bind_runtime_state(app.state.runtime):
        persisted = await factory().append_one("thread", event)
    assert persisted.seq == 21
    assert full_reads == 0
    await service.aclose()
