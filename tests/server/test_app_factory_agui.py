from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from ksadk.agui.config import AGUIConfig
from ksadk.server.composition import configure_runtime_app
from ksadk.server.factory import RuntimeAppConfig, bind_runtime_state, create_runtime_app
from ksadk.server.routes.common import set_runner


class _Runner:
    detection_result = SimpleNamespace(name="agent", type=SimpleNamespace(value="langgraph"))

    def load_agent(self):
        return None

    async def invoke(self, input_data):
        return {"output": "ok"}

    def stream(self, input_data):
        async def generate():
            yield {"type": "final", "output": "ok"}

        return generate()


def test_agui_route_is_mounted_before_configured_health_catch_all():
    runner = _Runner()
    app = create_runtime_app(
        RuntimeAppConfig(
            runner=runner,
            agui=AGUIConfig(enabled=True, agent_name="agent"),
            route_groups={"agui", "health_meta"},
        ),
        configure_runtime_app,
    )
    paths = [route.path for route in app.routes if hasattr(route, "path")]
    assert "/agentengine/agui" in paths
    assert paths.index("/agentengine/agui") < paths.index("/{requested_path:path}")
    assert "/agentengine/agui/health" in paths


def test_agui_run_is_immediately_available_through_session_message_history():
    session_id = f"thread-history-{uuid4().hex}"
    app = create_runtime_app(
        RuntimeAppConfig(
            runner=_Runner(),
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


def test_legacy_set_runner_mounts_production_agui_before_static_catch_all(monkeypatch):
    monkeypatch.setattr("ksadk.agui.config.agui_dependencies_available", lambda: True)
    app = create_runtime_app(
        RuntimeAppConfig(route_groups={"agui", "health_meta", "ui_bootstrap"}),
        configure_runtime_app,
    )
    runner = _Runner()

    with bind_runtime_state(app.state.runtime):
        set_runner(runner)

    paths = [route.path for route in app.routes if hasattr(route, "path")]
    assert "/agentengine/agui" in paths
    assert paths.index("/agentengine/agui") < paths.index("/{requested_path:path}")
    assert app.state.runtime.runner is runner
    assert app.state.runtime.agui_agent is not None

    response = TestClient(app).post(
        "/agentengine/api/v1/GetAgentUiBootstrap",
        json={"AgentId": "agent", "UserId": "user", "SessionId": "s1"},
    )
    hosted_chat = response.json()["Data"]["HostedChat"]
    assert hosted_chat["PreferredTransport"] == "ag-ui"
    assert [item["Protocol"] for item in hosted_chat["Transports"]] == [
        "ag-ui",
        "responses",
    ]


def test_default_agui_config_does_not_read_runner_private_agent(monkeypatch):
    from ksadk.agui.config import default_agui_config

    monkeypatch.setattr("ksadk.agui.config.agui_dependencies_available", lambda: True)

    class _NoPrivateAgentRunner(_Runner):
        def __getattribute__(self, name):
            if name == "_agent":
                raise AssertionError("production AG-UI wiring must not read runner._agent")
            return super().__getattribute__(name)

    config = default_agui_config(_NoPrivateAgentRunner())

    assert config.enabled is True
    assert config.runtime_type == "langgraph"


def test_legacy_set_runner_keeps_responses_fallback_without_agui_dependencies(monkeypatch):
    monkeypatch.setattr("ksadk.agui.config.agui_dependencies_available", lambda: False)
    app = create_runtime_app(
        RuntimeAppConfig(route_groups={"agui", "health_meta", "ui_bootstrap"}),
        configure_runtime_app,
    )

    with bind_runtime_state(app.state.runtime):
        set_runner(_Runner())

    paths = [route.path for route in app.routes if hasattr(route, "path")]
    assert "/agentengine/agui" not in paths
    response = TestClient(app).post(
        "/agentengine/api/v1/GetAgentUiBootstrap",
        json={"AgentId": "agent", "UserId": "user", "SessionId": "s1"},
    )
    hosted_chat = response.json()["Data"]["HostedChat"]
    assert hosted_chat["PreferredTransport"] == "responses"
    assert [item["Protocol"] for item in hosted_chat["Transports"]] == ["responses"]
