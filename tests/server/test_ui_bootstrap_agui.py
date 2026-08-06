from types import SimpleNamespace

from fastapi.testclient import TestClient

from ksadk.agui.config import AGUIConfig
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
            "ResumeRun": {"Supported": self.loaded},
            "Checkpoint": {"Supported": self.loaded},
        }


def test_bootstrap_advertises_agui_only_when_endpoint_is_enabled():
    app = create_runtime_app(
        RuntimeAppConfig(
            runner=_Runner(),
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
    app = create_runtime_app(RuntimeAppConfig(route_groups={"ui_bootstrap"}))
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

    app = create_runtime_app(
        RuntimeAppConfig(
            runner=_NoCheckpointRunner(),
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
    assert app.state.runtime.runner_loaded is True
    assert data["HostedChat"]["Transports"][0]["Capabilities"] == {
        "A2UI": True,
        "Interrupt": False,
        "Cancel": True,
    }
