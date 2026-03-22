"""Tests for the current runner contract."""

from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest

from ksadk.detection import DetectionResult, FrameworkType
from ksadk.runners.base_runner import BaseRunner
from ksadk.runners.factory import create_runner


class _StubRunner(BaseRunner):
    def __init__(self, detection_result: Any, project_dir: str):
        super().__init__(detection_result, project_dir)
        self.agent = "stub-agent"

    def load_agent(self) -> None:
        self._agent = self.agent

    async def invoke(self, input_data):
        return {"output": input_data}

    async def stream(self, input_data):
        yield {"output": input_data}


def _install_runner_module(monkeypatch, module_path: str, class_name: str):
    fake_module = ModuleType(module_path)

    class _FrameworkRunner(_StubRunner):
        pass

    _FrameworkRunner.__name__ = class_name
    setattr(fake_module, class_name, _FrameworkRunner)
    monkeypatch.setitem(__import__("sys").modules, module_path, fake_module)
    return _FrameworkRunner


@pytest.mark.parametrize(
    ("framework_type", "module_path", "class_name"),
    [
        (FrameworkType.ADK, "ksadk.runners.adk_runner", "ADKRunner"),
        (FrameworkType.LANGGRAPH, "ksadk.runners.langgraph_runner", "LangGraphRunner"),
        (FrameworkType.LANGCHAIN, "ksadk.runners.langchain_runner", "LangChainRunner"),
        (FrameworkType.DEEPAGENTS, "ksadk.runners.deepagents_runner", "DeepAgentsRunner"),
    ],
)
def test_create_runner_dispatches_by_framework(monkeypatch, framework_type, module_path: str, class_name: str):
    expected_class = _install_runner_module(monkeypatch, module_path, class_name)
    detection = DetectionResult(
        type=framework_type,
        name="demo-agent",
        entry_point="demo/agent.py",
        package_path="/tmp/demo",
        agent_variable="root_agent",
        confidence=1.0,
    )

    runner = create_runner(detection, "/workspace/demo")

    assert isinstance(runner, expected_class)
    assert runner.detection_result == detection
    assert runner.project_dir == "/workspace/demo"


def test_create_runner_rejects_unknown_framework():
    detection = DetectionResult(
        type=FrameworkType.UNKNOWN,
        name="unknown-agent",
        entry_point="",
        package_path="",
    )

    with pytest.raises(ValueError, match="不支持的框架类型"):
        create_runner(detection, "/workspace/demo")


def test_base_runner_run_server_registers_runner(monkeypatch):
    recorded: dict[str, Any] = {}

    class _DemoRunner(_StubRunner):
        pass

    fake_server_module = ModuleType("ksadk.server")
    fake_server_module.app = object()
    fake_server_module.set_runner = lambda runner: recorded.setdefault("runner", runner)

    fake_uvicorn_module = ModuleType("uvicorn")
    fake_uvicorn_module.run = lambda app, host, port: recorded.update(
        {"app": app, "host": host, "port": port}
    )

    monkeypatch.setitem(__import__("sys").modules, "ksadk.server", fake_server_module)
    monkeypatch.setitem(__import__("sys").modules, "uvicorn", fake_uvicorn_module)

    detection = DetectionResult(
        type=FrameworkType.LANGGRAPH,
        name="demo-agent",
        entry_point="demo/agent.py",
        package_path="/tmp/demo",
    )
    runner = _DemoRunner(detection, "/workspace/demo")

    runner.run_server(port=9000)

    assert recorded["runner"] is runner
    assert recorded["app"] is fake_server_module.app
    assert recorded["host"] == "0.0.0.0"
    assert recorded["port"] == 9000
