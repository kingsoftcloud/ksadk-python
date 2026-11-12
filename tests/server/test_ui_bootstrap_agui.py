from types import SimpleNamespace

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from ksadk.agui.config import AGUIConfig
from ksadk.runtime.adapter import RuntimeLaunchContext, RuntimeRegistry
from ksadk.runtime.executor import RuntimeExecutor
from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter
from ksadk.server.composition import configure_runtime_app
from ksadk.server.factory import RuntimeAppConfig, create_runtime_app
from ksadk.server.routes.ui_bootstrap import _submit_interaction_route_available


def test_submit_capability_uses_public_route_lookup_for_nested_routers():
    from ksadk.server.routes.interactions import control_router

    app = FastAPI()
    assert not _submit_interaction_route_available(app)
    nested = APIRouter()
    nested.include_router(control_router)
    app.include_router(nested)
    assert _submit_interaction_route_available(app)
    prefixed = FastAPI()
    prefixed.include_router(nested, prefix="/other")
    assert not _submit_interaction_route_available(prefixed)


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


def _execution_for(runner, runtime_type="langgraph"):
    registry = RuntimeRegistry()
    registry.register(
        runtime_type,
        lambda _context: RunnerRuntimeAdapter(runner, runtime_type=runtime_type),
    )
    return (
        RuntimeExecutor(registry),
        RuntimeLaunchContext(
            runtime_type=runtime_type,
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
        ),
        configure_runtime_app,
    )
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
        "Version": "1.0",
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
        ),
        configure_runtime_app,
    )
    response = TestClient(app).post(
        "/agentengine/api/v1/GetAgentUiBootstrap",
        json={"AgentId": "agent", "UserId": "user", "SessionId": "s1"},
    )
    assert response.status_code == 200
    assert response.json()["Data"]["Capabilities"]["HistoryRead"] == {
        "Protocol": "ksadk.history/v2", "SessionContractVersion": 2,
        "SupportsEpoch": True, "SupportsSnapshot": True,
    }
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
        ),
        configure_runtime_app,
    )

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


def test_bootstrap_refreshes_capability_without_loading_lazy_runner():
    calls = []

    class _LazyRunner(_Runner):
        def load_agent(self):
            calls.append("load")
            self.loaded = True

        async def refresh_runtime_capabilities(self):
            calls.append("refresh")

    executor, launch_context = _execution_for(_LazyRunner())
    app = create_runtime_app(
        RuntimeAppConfig(
            runtime_executor=executor,
            launch_context=launch_context,
            route_groups={"ui_bootstrap"},
        ),
        configure_runtime_app,
    )

    with TestClient(app) as client:
        response = client.post(
            "/agentengine/api/v1/GetAgentUiBootstrap",
            json={"AgentId": "agent", "UserId": "user", "SessionId": "s1"},
        )

    assert response.status_code == 200
    assert calls == ["refresh"]


def test_runtime_bootstrap_declares_adk_presentation_surface_only_for_supported_producer():
    from ksadk.conversations.contracts import ConversationSurface

    for framework in ("adk", "langgraph", "codex"):
        executor, context = _execution_for(_Runner(), framework)
        app = create_runtime_app(RuntimeAppConfig(
            runtime_executor=executor, launch_context=context, route_groups={"ui_bootstrap"},
        ), configure_runtime_app)
        response = TestClient(app).post("/agentengine/api/v1/GetAgentUiBootstrap",
            json={"AgentId": "agent", "SessionId": "session-1"})
        assert response.status_code == 200
        capabilities = response.json()["Data"]["Capabilities"]
        if framework == "adk":
            surface = ConversationSurface.model_validate(capabilities["ConversationSurface"])
            assert surface.session_id == "session-1"
            assert surface.permits_input("ksadk.presentation")
            assert any(c.name == "agent.block" and c.mode == "native" for c in surface.outputs)
        else:
            assert "ConversationSurface" not in capabilities


def test_managed_codex_and_langgraph_declare_remote_agent_surface():
    from ksadk.conversations.contracts import ConversationSurface

    for framework in ("codex", "langgraph"):
        executor, context = _execution_for(_Runner(), framework)
        app = create_runtime_app(RuntimeAppConfig(
            runtime_executor=executor, launch_context=context, route_groups={"ui_bootstrap"},
        ), configure_runtime_app)
        app.state.runtime.a2a_bootstrap = SimpleNamespace(outbound_enabled=True)
        response = TestClient(app).post(
            "/agentengine/api/v1/GetAgentUiBootstrap",
            json={"AgentId": "agent", "SessionId": "session-1"},
        )
        assert response.status_code == 200
        surface = ConversationSurface.model_validate(
            response.json()["Data"]["Capabilities"]["ConversationSurface"]
        )
        assert surface.provider_ref == framework
        assert surface.permits_input("ksadk.presentation")
        assert any(c.name == "agent.block" and c.mode == "translated" for c in surface.outputs)
