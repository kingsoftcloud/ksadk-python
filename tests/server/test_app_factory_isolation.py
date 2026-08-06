from __future__ import annotations

import asyncio
import importlib
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from ksadk.hermes_terminal import TERMINAL_SUBPROTOCOL
from ksadk.sandbox.registry import GLOBAL_SANDBOX_REGISTRY, get_sandbox_registry
from ksadk.server.app import _configure_runtime_app
from ksadk.server.factory import (
    ALL_GROUPS,
    RuntimeAppConfig,
    create_runtime_app,
    get_state,
)
from ksadk.server.terminal_sessions import TerminalSession

server_app_module = importlib.import_module("ksadk.server.app")


def _make_app(runner=None):
    return create_runtime_app(
        RuntimeAppConfig(runner=runner, route_groups=set(ALL_GROUPS)),
        _configure_runtime_app,
    )


def test_runtime_apps_own_terminal_sandbox_and_stream_registries():
    app_a = _make_app()
    app_b = _make_app()

    state_a = app_a.state.runtime
    state_b = app_b.state.runtime

    assert state_a.terminal_manager is not state_b.terminal_manager
    assert state_a.sandbox_registry is not state_b.sandbox_registry
    assert state_a.stream_registry is not state_b.stream_registry


@pytest.mark.asyncio
async def test_runtime_apps_own_session_services(monkeypatch):
    monkeypatch.setenv("KSADK_SESSION_BACKEND", "memory")
    app_a = _make_app()
    app_b = _make_app()
    transport_a = httpx.ASGITransport(app=app_a)
    transport_b = httpx.ASGITransport(app=app_b)

    async with httpx.AsyncClient(transport=transport_a, base_url="http://app-a.local") as client_a:
        async with httpx.AsyncClient(
            transport=transport_b, base_url="http://app-b.local"
        ) as client_b:
            created_a, created_b = await asyncio.gather(
                client_a.post(
                    "/agentengine/api/v1/CreateSession",
                    json={"AgentId": "agent", "SessionId": "shared-session"},
                ),
                client_b.post(
                    "/agentengine/api/v1/CreateSession",
                    json={"AgentId": "agent", "SessionId": "shared-session"},
                ),
            )
            listed_a, listed_b = await asyncio.gather(
                client_a.post("/agentengine/api/v1/ListSessions", json={"AgentId": "agent"}),
                client_b.post("/agentengine/api/v1/ListSessions", json={"AgentId": "agent"}),
            )

    assert created_a.status_code == 200, created_a.text
    assert created_b.status_code == 200, created_b.text
    assert [item["SessionId"] for item in listed_a.json()["Data"]["Sessions"]] == ["shared-session"]
    assert [item["SessionId"] for item in listed_b.json()["Data"]["Sessions"]] == ["shared-session"]
    assert (
        app_a.state.runtime.resolve_session_service()
        is not app_b.state.runtime.resolve_session_service()
    )


def test_workspace_proxy_targets_the_current_runtime_app():
    def make_app(label: str):
        def configure(app, state, groups):
            @app.get("/_ksadk/workspace/v1/entries")
            async def list_entries(path: str = ".", recursive: bool = False):
                return {"label": label, "path": path, "recursive": recursive}

            _configure_runtime_app(app, state, groups)

        return create_runtime_app(RuntimeAppConfig(), configure)

    app_a = make_app("a")
    app_b = make_app("b")

    with TestClient(app_a) as client_a, TestClient(app_b) as client_b:
        response_a = client_a.post(
            "/agentengine/api/v1/ListWorkspaceFiles",
            json={"Path": "one", "Recursive": True},
        )
        response_b = client_b.post(
            "/agentengine/api/v1/ListWorkspaceFiles",
            json={"Path": "two", "Recursive": False},
        )

    assert response_a.status_code == 200
    assert response_a.json()["Data"] == {
        "label": "a",
        "path": "one",
        "recursive": True,
    }
    assert response_b.status_code == 200
    assert response_b.json()["Data"] == {
        "label": "b",
        "path": "two",
        "recursive": False,
    }


def test_terminal_websocket_binds_own_runtime_state(monkeypatch):
    runner = SimpleNamespace(
        detection_result=SimpleNamespace(type=SimpleNamespace(value="openclaw"))
    )
    app = _make_app(runner=runner)
    manager = app.state.runtime.terminal_manager
    frameworks: list[str] = []

    monkeypatch.setattr(manager, "_resolve_terminal_command", lambda _session: [])

    def fake_spawn(session):
        frameworks.append(session.framework)
        session.status = "running"

    async def fake_attach(_ws, session):
        session.status = "detached"

    monkeypatch.setattr(manager, "_spawn_session", fake_spawn)
    monkeypatch.setattr(manager, "_attach_existing", fake_attach)

    with TestClient(app) as client:
        with client.websocket_connect(
            "/_ksadk/terminal/ws",
            subprotocols=[TERMINAL_SUBPROTOCOL],
        ) as websocket:
            websocket.send_json({"type": "start", "session_id": "shared-session", "mode": "tui"})

    assert frameworks == ["openclaw"]


def test_same_sandbox_key_is_isolated_by_runtime_app():
    created: list[str] = []

    class FakeSession:
        def __init__(self, sandbox_id: str):
            self.sandbox_id = sandbox_id

        def kill(self):
            return None

    class FakeBackend:
        def create_session(self, **_kwargs):
            sandbox_id = f"sandbox-{len(created) + 1}"
            created.append(sandbox_id)
            return FakeSession(sandbox_id)

    backend = FakeBackend()

    def configure(app, state, groups):
        @app.post("/__test__/sandbox")
        async def create_sandbox():
            entry, _ = GLOBAL_SANDBOX_REGISTRY.get_or_create(
                key="same-session",
                backend_name="fake",
                backend=backend,
                ttl_seconds=60,
                isolated=True,
            )
            return {"sandbox_id": entry.sandbox_id}

        _configure_runtime_app(app, state, groups)

    app_a = create_runtime_app(RuntimeAppConfig(), configure)
    app_b = create_runtime_app(RuntimeAppConfig(), configure)

    with TestClient(app_a) as client_a, TestClient(app_b) as client_b:
        sandbox_a = client_a.post("/__test__/sandbox").json()["sandbox_id"]
        sandbox_b = client_b.post("/__test__/sandbox").json()["sandbox_id"]
        assert sandbox_a != sandbox_b
        assert [entry.sandbox_id for entry in app_a.state.runtime.sandbox_registry.entries()] == [
            sandbox_a
        ]
        assert [entry.sandbox_id for entry in app_b.state.runtime.sandbox_registry.entries()] == [
            sandbox_b
        ]


@pytest.mark.asyncio
async def test_background_tasks_keep_app_context_after_request_and_peer_shutdown():
    gates = {"a": asyncio.Event(), "b": asyncio.Event()}
    tasks: dict[str, asyncio.Task[None]] = {}
    seen: dict[str, list[tuple[str, object, object]]] = {"a": [], "b": []}

    def make_app(label: str):
        def configure(app, state, groups):
            @app.post("/__test__/background/{business_id}")
            async def start_background_task(business_id: str):
                async def record_context():
                    await gates[label].wait()
                    seen[label].append((business_id, get_state(), get_sandbox_registry()))

                tasks[label] = asyncio.create_task(record_context())
                return {"scheduled": True}

            _configure_runtime_app(app, state, groups)

        return create_runtime_app(RuntimeAppConfig(), configure)

    app_a = make_app("a")
    app_b = make_app("b")
    transport_a = httpx.ASGITransport(app=app_a)
    transport_b = httpx.ASGITransport(app=app_b)

    async with app_b.router.lifespan_context(app_b):
        async with httpx.AsyncClient(
            transport=transport_b, base_url="http://app-b.local"
        ) as client_b:
            async with app_a.router.lifespan_context(app_a):
                async with httpx.AsyncClient(
                    transport=transport_a, base_url="http://app-a.local"
                ) as client_a:
                    response_a, response_b = await asyncio.gather(
                        client_a.post("/__test__/background/same-session"),
                        client_b.post("/__test__/background/same-session"),
                    )
                    assert response_a.json() == {"scheduled": True}
                    assert response_b.json() == {"scheduled": True}

                    gates["a"].set()
                    gates["b"].set()
                    await asyncio.gather(tasks["a"], tasks["b"])

                    assert seen["a"] == [
                        (
                            "same-session",
                            app_a.state.runtime,
                            app_a.state.runtime.sandbox_registry,
                        )
                    ]
                    assert seen["b"] == [
                        (
                            "same-session",
                            app_b.state.runtime,
                            app_b.state.runtime.sandbox_registry,
                        )
                    ]

            gates["b"] = asyncio.Event()
            response_b = await client_b.post("/__test__/background/same-session")
            assert response_b.json() == {"scheduled": True}
            gates["b"].set()
            await tasks["b"]
            assert seen["b"][-1] == (
                "same-session",
                app_b.state.runtime,
                app_b.state.runtime.sandbox_registry,
            )


@pytest.mark.asyncio
async def test_shutdown_of_one_app_does_not_touch_other_app_resources(monkeypatch):
    monkeypatch.setenv("KSADK_SANDBOX_SWEEP_INTERVAL_SECONDS", "0")
    closed: list[str] = []
    killed: list[str] = []

    class FakeRunner:
        def __init__(self, name: str):
            self.name = name

        async def close(self):
            closed.append(self.name)

    class FakeSession:
        def __init__(self, sandbox_id: str):
            self.sandbox_id = sandbox_id

        def kill(self):
            killed.append(self.sandbox_id)

    class FakeBackend:
        def __init__(self, sandbox_id: str):
            self.sandbox_id = sandbox_id

        def create_session(self, **_kwargs):
            return FakeSession(self.sandbox_id)

    app_a = _make_app(runner=FakeRunner("runner-a"))
    app_b = _make_app(runner=FakeRunner("runner-b"))
    state_a = app_a.state.runtime
    state_b = app_b.state.runtime

    for state, sandbox_id in ((state_a, "sandbox-a"), (state_b, "sandbox-b")):
        state.terminal_manager.sessions["same-terminal"] = TerminalSession(
            id="same-terminal",
            session_id="same-session",
            mode="tui",
            status="detached",
        )
        state.sandbox_registry.get_or_create(
            key="same-session",
            backend_name="fake",
            backend=FakeBackend(sandbox_id),
            ttl_seconds=60,
            isolated=True,
        )

    block_a = asyncio.Event()
    block_b = asyncio.Event()
    stream_a = asyncio.create_task(block_a.wait())
    stream_b = asyncio.create_task(block_b.wait())
    state_a.stream_registry.streams.add(stream_a)
    state_b.stream_registry.streams.add(stream_b)

    async with app_b.router.lifespan_context(app_b):
        async with app_a.router.lifespan_context(app_a):
            pass

        assert closed == ["runner-a"]
        assert stream_a.done()
        assert not stream_b.done()
        assert state_a.stream_registry.streams == set()
        assert state_b.stream_registry.streams == {stream_b}
        assert state_a.terminal_manager.sessions == {}
        assert "same-terminal" in state_b.terminal_manager.sessions
        assert state_a.sandbox_registry.entries() == []
        assert [entry.sandbox_id for entry in state_b.sandbox_registry.entries()] == ["sandbox-b"]
        assert killed == ["sandbox-a"]

    assert closed == ["runner-a", "runner-b"]
    assert stream_b.done()
    assert state_b.terminal_manager.sessions == {}
    assert state_b.sandbox_registry.entries() == []
    assert killed == ["sandbox-a", "sandbox-b"]


def test_two_apps_create_same_terminal_session_id_concurrently(monkeypatch):
    app_a = _make_app()
    app_b = _make_app()

    for manager in (
        app_a.state.runtime.terminal_manager,
        app_b.state.runtime.terminal_manager,
    ):
        monkeypatch.setattr(manager, "_resolve_terminal_command", lambda _session: [])

        def fake_spawn(session):
            session.status = "running"

        monkeypatch.setattr(manager, "_spawn_session", fake_spawn)

    with TestClient(app_a) as client_a, TestClient(app_b) as client_b:
        with ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(
                client_a.post,
                "/_ksadk/terminal/sessions",
                json={"session_id": "same-session", "mode": "tui"},
            )
            future_b = pool.submit(
                client_b.post,
                "/_ksadk/terminal/sessions",
                json={"session_id": "same-session", "mode": "tui"},
            )
            response_a = future_a.result(timeout=5)
            response_b = future_b.result(timeout=5)

        terminal_a = response_a.json()["session"]["terminal_session_id"]
        terminal_b = response_b.json()["session"]["terminal_session_id"]
        assert terminal_a != terminal_b
        assert set(app_a.state.runtime.terminal_manager.sessions) == {terminal_a}
        assert set(app_b.state.runtime.terminal_manager.sessions) == {terminal_b}


def test_compatibility_shims_stay_bound_to_default_app():
    default_state = server_app_module.app.state.runtime
    other_app = _make_app()
    other_state = other_app.state.runtime
    unique_invocation = "non-default-app-only"

    other_state.stream_registry.streams_by_invocation[unique_invocation] = object()
    try:
        assert server_app_module.terminal_manager is default_state.terminal_manager
        assert server_app_module.terminal_manager is not other_state.terminal_manager
        assert get_sandbox_registry() is default_state.sandbox_registry
        assert get_sandbox_registry() is not other_state.sandbox_registry
        assert (
            server_app_module._DETACHED_STREAMS_BY_INVOCATION
            is default_state.stream_registry.streams_by_invocation
        )
        assert unique_invocation not in server_app_module._DETACHED_STREAMS_BY_INVOCATION
    finally:
        other_state.stream_registry.streams_by_invocation.pop(unique_invocation, None)
