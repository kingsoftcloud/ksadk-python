"""Tests for the current runner contract."""

from __future__ import annotations

import textwrap
from types import ModuleType
from typing import Any
from uuid import uuid4

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


def _write_adk_project(tmp_path, source: str) -> DetectionResult:
    package_name = f"demo_agent_{uuid4().hex[:8]}"
    package_dir = tmp_path / package_name
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "agent.py").write_text(textwrap.dedent(source), encoding="utf-8")
    return DetectionResult(
        type=FrameworkType.ADK,
        name="demo-agent",
        entry_point=f"{package_name}/agent.py",
        package_path=str(package_dir),
        agent_variable="root_agent",
        confidence=1.0,
    )


def _tool_names(tools: list[Any]) -> list[str]:
    return [getattr(tool, "name", None) or getattr(tool, "__name__", "") for tool in tools]


@pytest.mark.parametrize(
    ("framework_type", "module_path", "class_name"),
    [
        (FrameworkType.ADK, "ksadk.runners.adk_runner", "ADKRunner"),
        (FrameworkType.LANGGRAPH, "ksadk.runners.langgraph_runner", "LangGraphRunner"),
        (FrameworkType.LANGCHAIN, "ksadk.runners.langchain_runner", "LangChainRunner"),
        (FrameworkType.DEEPAGENTS, "ksadk.runners.deepagents_runner", "DeepAgentsRunner"),
    ],
)
def test_create_runner_dispatches_by_framework(
    monkeypatch,
    framework_type,
    module_path: str,
    class_name: str,
):
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


def test_adk_runner_load_agent_injects_default_sandbox_tools(monkeypatch, tmp_path):
    import google.adk.runners as adk_runners

    from ksadk.runners.adk_runner import ADKRunner

    detection = _write_adk_project(
        tmp_path,
        """
        class DemoAgent:
            def __init__(self):
                self.name = "demo-agent"
                self.tools = []
                self.instruction = "Be helpful."

        root_agent = DemoAgent()
        """,
    )

    class FakeRunner:
        instances: list["FakeRunner"] = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            FakeRunner.instances.append(self)

    monkeypatch.delenv("KSADK_ENABLE_SANDBOX_TOOLS", raising=False)
    monkeypatch.setattr(ADKRunner, "_apply_json_patch", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_short_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_long_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_knowledge_base", lambda self: None)
    monkeypatch.setattr(adk_runners, "Runner", FakeRunner)
    runner = ADKRunner(detection, str(tmp_path))
    runner.load_agent()

    tool_names = _tool_names(runner._agent.tools)
    assert "execute_python" in tool_names
    assert "execute_bash" in tool_names
    assert "execute_javascript" in tool_names
    assert len(FakeRunner.instances) == 1


def test_adk_runner_load_agent_deduplicates_existing_sandbox_tools(monkeypatch, tmp_path):
    import google.adk.runners as adk_runners

    from ksadk.runners.adk_runner import ADKRunner

    detection = _write_adk_project(
        tmp_path,
        """
        def execute_python(code: str) -> str:
            return code

        def keep_tool(value: str) -> str:
            return value

        class DemoAgent:
            def __init__(self):
                self.name = "demo-agent"
                self.tools = [keep_tool, execute_python]
                self.instruction = "Be helpful."

        root_agent = DemoAgent()
        """,
    )

    class FakeRunner:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.delenv("KSADK_ENABLE_SANDBOX_TOOLS", raising=False)
    monkeypatch.setattr(ADKRunner, "_apply_json_patch", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_short_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_long_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_knowledge_base", lambda self: None)
    monkeypatch.setattr(adk_runners, "Runner", FakeRunner)

    runner = ADKRunner(detection, str(tmp_path))
    runner.load_agent()

    tool_names = _tool_names(runner._agent.tools)
    assert tool_names.count("execute_python") == 1
    assert "keep_tool" in tool_names
    assert "execute_bash" in tool_names
    assert "execute_javascript" in tool_names


def test_adk_runner_load_agent_skips_sandbox_tools_when_disabled(monkeypatch, tmp_path):
    import google.adk.runners as adk_runners

    from ksadk.runners.adk_runner import ADKRunner

    detection = _write_adk_project(
        tmp_path,
        """
        class DemoAgent:
            def __init__(self):
                self.name = "demo-agent"
                self.tools = []
                self.instruction = "Be helpful."

        root_agent = DemoAgent()
        """,
    )

    class FakeRunner:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setenv("KSADK_ENABLE_SANDBOX_TOOLS", "0")
    monkeypatch.setattr(ADKRunner, "_apply_json_patch", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_short_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_long_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_knowledge_base", lambda self: None)
    monkeypatch.setattr(adk_runners, "Runner", FakeRunner)

    runner = ADKRunner(detection, str(tmp_path))
    runner.load_agent()

    assert _tool_names(runner._agent.tools) == []
