from types import SimpleNamespace

from fastapi.testclient import TestClient

from ksadk.agui.config import AGUIConfig
from ksadk.runtime.adapter import RuntimeLaunchContext, RuntimeRegistry
from ksadk.runtime.executor import RuntimeExecutor
from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter
from ksadk.server.factory import RuntimeAppConfig, create_runtime_app
from ksadk.server.routes.routers import ui_bootstrap_router


class _Runner:
    detection_result = SimpleNamespace(name="agent", type=SimpleNamespace(value="langgraph"))

    def __init__(self):
        self.loaded = False

    def load_agent(self):
        self.loaded = True

    def get_runtime_capabilities(self):
        return {
            "CancelRun": {"Supported": True},
            "ResumeRun": {"Supported": True},
            "Checkpoint": {"Supported": True},
        }


def _execution_for(runner):
    registry = RuntimeRegistry()
    registry.register(
        "langgraph",
        lambda _context: RunnerRuntimeAdapter(runner, runtime_type="langgraph"),
    )
    return (
        RuntimeExecutor(registry),
        RuntimeLaunchContext(
            runtime_type="langgraph",
            project_dir=".",
            detection=runner.detection_result,
        ),
    )


def test_bootstrap_advertises_agui_only_when_endpoint_is_enabled():
    runner = _Runner()
    executor, launch_context = _execution_for(runner)
    app = create_runtime_app(
        RuntimeAppConfig(
            runtime_executor=executor,
            launch_context=launch_context,
            agui=AGUIConfig(enabled=True, agent_name="agent"),
            route_groups={"ui_bootstrap", "agui"},
        )
    )
    app.include_router(ui_bootstrap_router)
    response = TestClient(app).post(
        "/agentengine/api/v1/GetAgentUiBootstrap",
        json={"AgentId": "agent", "UserId": "user", "SessionId": "s1"},
    )
    assert response.status_code == 200
    data = response.json()["Data"]["HostedChat"]
    assert data["PreferredTransport"] == "ag-ui"
    assert data["Transports"][0] == {
        "Protocol": "ag-ui",
        "Runtime": "copilotkit",
        "Endpoint": "/agentengine/agui",
        "Version": "0.1.19",
        "Capabilities": {"A2UI": True, "Interrupt": True, "Cancel": True},
    }
    assert data["Transports"][1]["Protocol"] == "responses"


def test_bootstrap_falls_back_to_responses_without_agui():
    executor, launch_context = _execution_for(_Runner())
    app = create_runtime_app(
        RuntimeAppConfig(
            runtime_executor=executor,
            launch_context=launch_context,
            route_groups={"ui_bootstrap"},
        )
    )
    app.include_router(ui_bootstrap_router)
    response = TestClient(app).post(
        "/agentengine/api/v1/GetAgentUiBootstrap",
        json={"AgentId": "agent", "UserId": "user", "SessionId": "s1"},
    )
    assert response.status_code == 200
    data = response.json()["Data"]["HostedChat"]
    assert data["PreferredTransport"] == "responses"
    assert [transport["Protocol"] for transport in data["Transports"]] == ["responses"]


def test_bootstrap_does_not_advertise_agui_interrupt_without_runtime_checkpoint():
    class _NoCheckpointRunner(_Runner):
        def get_runtime_capabilities(self):
            return {
                "CancelRun": {"Supported": False},
                "ResumeRun": {"Supported": False},
                "Checkpoint": {"Supported": False},
            }

    runner = _NoCheckpointRunner()
    executor, launch_context = _execution_for(runner)
    app = create_runtime_app(
        RuntimeAppConfig(
            runtime_executor=executor,
            launch_context=launch_context,
            agui=AGUIConfig(enabled=True, agent_name="agent"),
            route_groups={"ui_bootstrap", "agui"},
        )
    )
    app.include_router(ui_bootstrap_router)

    response = TestClient(app).post(
        "/agentengine/api/v1/GetAgentUiBootstrap",
        json={"AgentId": "agent", "UserId": "user", "SessionId": "s1"},
    )

    assert response.status_code == 200
    data = response.json()["Data"]
    assert data["HostedChat"]["Transports"][0]["Capabilities"] == {
        "A2UI": True,
        "Interrupt": False,
        "Cancel": True,
    }


def test_lifespan_refreshes_capability_only_after_lazy_runner_load():
    calls = []

    class _LazyRunner(_Runner):
        def load_agent(self):
            calls.append("load")
            self.loaded = True

        async def refresh_runtime_capabilities(self):
            assert self.loaded is True
            calls.append("refresh")

    app = create_runtime_app(
        RuntimeAppConfig(runner=_LazyRunner(), route_groups={"ui_bootstrap"})
    )
    app.include_router(ui_bootstrap_router)

    with TestClient(app) as client:
        response = client.post(
            "/agentengine/api/v1/GetAgentUiBootstrap",
            json={"AgentId": "agent", "UserId": "user", "SessionId": "s1"},
        )

    assert response.status_code == 200
    assert calls == ["load", "refresh"]
