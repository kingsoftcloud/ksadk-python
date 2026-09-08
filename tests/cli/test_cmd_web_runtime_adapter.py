from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from ksadk.cli import runtime_bootstrap
from ksadk.runtime import RuntimeExecutor, RuntimeRegistry
from ksadk.runtime.runner_adapter import RunnerRuntimeAdapter


class _FrameworkType:
    value = "fixture"


class _Detection:
    type = _FrameworkType()
    name = "fixture-agent"
    raw_config = {"feature": "enabled"}


class _LangGraphFrameworkType:
    value = "langgraph"


class _LangGraphDetection:
    type = _LangGraphFrameworkType()
    name = "langgraph-agent"
    raw_config = {}


class _LangGraphRunner:
    detection_result = SimpleNamespace(
        type=SimpleNamespace(value="langgraph"),
        name="langgraph-agent",
    )

    def load_agent(self) -> None:
        return None

    def get_runtime_capabilities(self):
        return {
            "CancelRun": {"Supported": True},
            "ResumeRun": {"Supported": True},
            "Checkpoint": {"Supported": True},
        }


def test_create_runtime_web_app_uses_executor_and_launch_context(monkeypatch, tmp_path: Path):
    registry = RuntimeRegistry()
    monkeypatch.setattr(runtime_bootstrap, "build_default_runtime_registry", lambda: registry)

    app = runtime_bootstrap.create_runtime_web_app(_Detection(), tmp_path)

    assert isinstance(app.state.runtime.executor, RuntimeExecutor)
    assert app.state.runtime.launch_context.runtime_type == "fixture"
    assert app.state.runtime.launch_context.project_dir == tmp_path
    assert app.state.runtime.launch_context.detection is not None
    assert app.state.runtime.launch_context.config == {"feature": "enabled"}
    assert not hasattr(app.state.runtime, "runner")


def test_create_runtime_web_app_advertises_agui_for_langgraph(monkeypatch, tmp_path: Path):
    registry = RuntimeRegistry()
    runner = _LangGraphRunner()
    registry.register(
        "langgraph",
        lambda _context: RunnerRuntimeAdapter(runner, runtime_type="langgraph"),
    )
    monkeypatch.setattr(runtime_bootstrap, "build_default_runtime_registry", lambda: registry)

    app = runtime_bootstrap.create_runtime_web_app(_LangGraphDetection(), tmp_path)
    response = TestClient(app).post(
        "/agentengine/api/v1/GetAgentUiBootstrap",
        json={"AgentId": "langgraph-agent", "UserId": "user", "SessionId": "s1"},
    )

    assert response.status_code == 200
    hosted_chat = response.json()["Data"]["HostedChat"]
    assert hosted_chat["PreferredTransport"] == "ag-ui"
    assert hosted_chat["Transports"][0]["Protocol"] == "ag-ui"
    assert hosted_chat["Transports"][0]["Capabilities"]["A2UI"] is True
