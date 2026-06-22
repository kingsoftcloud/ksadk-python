"""Tests for the current runner contract."""

from __future__ import annotations

import base64
import os
import textwrap
from types import SimpleNamespace
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


class _AsyncClosableToolset:
    def __init__(self):
        self.closed = 0

    async def close(self):
        self.closed += 1


class _SyncClosableToolset:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


class _AsyncAClosableToolset:
    def __init__(self):
        self.closed = 0

    async def aclose(self):
        self.closed += 1


class _FailingClosableToolset:
    def __init__(self):
        self.closed = 0

    async def close(self):
        self.closed += 1
        raise RuntimeError("close failed")


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


def _write_detection(
    framework_type: FrameworkType,
    *,
    entry_point: str = "demo/agent.py",
    package_path: str = "/tmp/demo",
) -> DetectionResult:
    return DetectionResult(
        type=framework_type,
        name="demo-agent",
        entry_point=entry_point,
        package_path=package_path,
        agent_variable="root_agent",
        confidence=1.0,
    )


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


def test_create_runner_uses_custom_runner_class(monkeypatch, tmp_path):
    runner_class = _install_runner_module(monkeypatch, "demo_agent.runner", "CustomRunner")
    detection = DetectionResult(
        type=FrameworkType.LANGGRAPH,
        name="demo-agent",
        entry_point="agent.py",
        package_path=str(tmp_path),
        agent_variable="root_agent",
        runner_class="demo_agent.runner.CustomRunner",
        confidence=1.0,
    )

    runner = create_runner(detection, str(tmp_path))

    assert isinstance(runner, runner_class)
    assert runner.detection_result is detection
    assert runner.project_dir == str(tmp_path)


def test_create_runner_rejects_custom_runner_that_is_not_base_runner(monkeypatch, tmp_path):
    fake_module = ModuleType("demo_agent.bad_runner")

    class BadRunner:
        pass

    fake_module.BadRunner = BadRunner
    monkeypatch.setitem(__import__("sys").modules, "demo_agent.bad_runner", fake_module)
    detection = DetectionResult(
        type=FrameworkType.LANGGRAPH,
        name="demo-agent",
        entry_point="agent.py",
        package_path=str(tmp_path),
        agent_variable="root_agent",
        runner_class="demo_agent.bad_runner.BadRunner",
        confidence=1.0,
    )

    with pytest.raises(TypeError, match="自定义 Runner 必须继承 BaseRunner"):
        create_runner(detection, str(tmp_path))


def test_runners_package_exports_only_create_runner():
    import ksadk.runners as runners

    assert hasattr(runners, "create_runner")
    assert set(runners.__all__) == {"BaseRunner", "create_runner"}


@pytest.mark.asyncio
async def test_base_runner_close_and_async_context_are_noops():
    detection = _write_detection(FrameworkType.LANGCHAIN)
    runner = _StubRunner(detection, "/workspace/demo")

    async with runner as active_runner:
        assert active_runner is runner

    assert await runner.close() is None


@pytest.mark.asyncio
async def test_adk_runner_close_releases_runtime_toolsets_once(tmp_path):
    from ksadk.runners.adk_runner import ADKRunner

    runner = ADKRunner(_write_detection(FrameworkType.ADK), str(tmp_path))
    async_close = _AsyncClosableToolset()
    sync_close = _SyncClosableToolset()
    async_aclose = _AsyncAClosableToolset()
    runner._runtime_toolsets = [async_close, sync_close, async_aclose]

    await runner.close()
    await runner.close()

    assert async_close.closed == 1
    assert sync_close.closed == 1
    assert async_aclose.closed == 1
    assert runner._runtime_toolsets == []


@pytest.mark.asyncio
async def test_adk_runner_close_continues_after_toolset_failure(tmp_path, caplog):
    from ksadk.runners.adk_runner import ADKRunner

    runner = ADKRunner(_write_detection(FrameworkType.ADK), str(tmp_path))
    failing = _FailingClosableToolset()
    ok = _AsyncClosableToolset()
    runner._runtime_toolsets = [failing, ok]

    await runner.close()

    assert failing.closed == 1
    assert ok.closed == 1
    assert runner._runtime_toolsets == []
    assert "Failed to close runtime toolset" in caplog.text


def test_langchain_runner_prepare_for_request_reloads_agent_when_model_changes(
    monkeypatch,
    tmp_path,
):
    import ksadk.runners.langchain_runner as langchain_runner_module

    loaded_models: list[tuple[str | None, bool]] = []

    def fake_load_agent_module(project_dir: str, entry_point: str, agent_variable: str, *, force_reload: bool = False):
        loaded_models.append((os.getenv("OPENAI_MODEL_NAME"), force_reload))
        return SimpleNamespace(invoke=lambda *args, **kwargs: None), ModuleType("demo.agent")

    monkeypatch.setattr(langchain_runner_module, "load_agent_module", fake_load_agent_module)
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-5.1")
    monkeypatch.setenv("MODEL_NAME", "glm-5.1")

    runner = langchain_runner_module.LangChainRunner(
        _write_detection(FrameworkType.LANGCHAIN),
        str(tmp_path),
    )
    runner.load_agent()
    runner.prepare_for_request("gpt-4o")

    assert loaded_models == [("glm-5.1", False), ("gpt-4o", True)]


def test_langgraph_runner_prepare_for_request_reloads_agent_when_model_changes(
    monkeypatch,
    tmp_path,
):
    import ksadk.runners.langgraph_runner as langgraph_runner_module

    loaded_models: list[tuple[str | None, bool]] = []

    def fake_load_agent_module(project_dir: str, entry_point: str, agent_variable: str, *, force_reload: bool = False):
        loaded_models.append((os.getenv("OPENAI_MODEL_NAME"), force_reload))
        return SimpleNamespace(invoke=lambda *args, **kwargs: None), ModuleType("demo.agent")

    monkeypatch.setattr(langgraph_runner_module, "load_agent_module", fake_load_agent_module)
    monkeypatch.setenv("OPENAI_MODEL_NAME", "glm-5.1")
    monkeypatch.setenv("MODEL_NAME", "glm-5.1")

    runner = langgraph_runner_module.LangGraphRunner(
        _write_detection(FrameworkType.LANGGRAPH),
        str(tmp_path),
    )
    runner.load_agent()
    runner.prepare_for_request("gpt-4o")

    assert loaded_models == [("glm-5.1", False), ("gpt-4o", True)]


def test_adk_runner_prepare_for_request_updates_explicit_model_tree(monkeypatch, tmp_path):
    from ksadk.runners.adk_runner import ADKRunner

    class FakeLiteLlm:
        def __init__(self, model: str):
            self.model = model

    child_agent = SimpleNamespace(model=FakeLiteLlm("openai/glm-5.1"), sub_agents=[])
    root_agent = SimpleNamespace(model=FakeLiteLlm("openai/glm-5.1"), sub_agents=[child_agent])

    runner = ADKRunner(_write_detection(FrameworkType.ADK), str(tmp_path))
    runner._agent = root_agent

    monkeypatch.delenv("OPENAI_MODEL_NAME", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)

    runner.prepare_for_request("gpt-4o")

    assert root_agent.model.model == "openai/gpt-4o"
    assert child_agent.model.model == "openai/gpt-4o"
    assert os.environ["OPENAI_MODEL_NAME"] == "gpt-4o"
    assert os.environ["MODEL_NAME"] == "gpt-4o"


def test_adk_runner_prepare_for_request_restores_default_model_when_request_omits_model(
    monkeypatch,
    tmp_path,
):
    from ksadk.runners.adk_runner import ADKRunner

    class FakeLiteLlm:
        def __init__(self, model: str):
            self.model = model

    child_agent = SimpleNamespace(model=FakeLiteLlm("openai/deepseek-v3.2"), sub_agents=[])
    root_agent = SimpleNamespace(model=FakeLiteLlm("openai/deepseek-v3.2"), sub_agents=[child_agent])

    runner = ADKRunner(_write_detection(FrameworkType.ADK), str(tmp_path))
    runner._agent = root_agent
    runner._default_model_name = "deepseek-v3.2"
    runner._default_model_reference = "openai/deepseek-v3.2"
    runner._active_model_name = "openai/deepseek-v3.2"

    monkeypatch.setenv("OPENAI_MODEL_NAME", "deepseek-v3.2")
    monkeypatch.setenv("MODEL_NAME", "deepseek-v3.2")

    runner.prepare_for_request("dummy")
    assert root_agent.model.model == "openai/dummy"
    assert child_agent.model.model == "openai/dummy"

    runner.prepare_for_request(None)

    assert root_agent.model.model == "openai/deepseek-v3.2"
    assert child_agent.model.model == "openai/deepseek-v3.2"
    assert os.environ["OPENAI_MODEL_NAME"] == "deepseek-v3.2"
    assert os.environ["MODEL_NAME"] == "deepseek-v3.2"


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


def test_adk_runner_load_agent_does_not_inject_legacy_sandbox_tools_by_default(
    monkeypatch, tmp_path
):
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

    monkeypatch.delenv("KSADK_SKILLS_MODE", raising=False)
    monkeypatch.setattr(ADKRunner, "_apply_json_patch", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_short_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_long_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_knowledge_base", lambda self: None)
    monkeypatch.setattr(adk_runners, "Runner", FakeRunner)
    runner = ADKRunner(detection, str(tmp_path))
    runner.load_agent()

    assert _tool_names(runner._agent.tools) == []
    assert len(FakeRunner.instances) == 1


def test_adk_runner_load_agent_deduplicates_existing_execute_skills(monkeypatch, tmp_path):
    import google.adk.runners as adk_runners

    from ksadk.runners.adk_runner import ADKRunner

    detection = _write_adk_project(
        tmp_path,
        """
        def execute_skills(workflow_prompt: str) -> dict:
            return {"stdout": workflow_prompt}

        def keep_tool(value: str) -> str:
            return value

        class DemoAgent:
            def __init__(self):
                self.name = "demo-agent"
                self.tools = [keep_tool, execute_skills]
                self.instruction = "Be helpful."

        root_agent = DemoAgent()
        """,
    )

    class FakeRunner:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setenv("KSADK_SKILLS_MODE", "sandbox")
    monkeypatch.setenv("KSADK_SKILL_RUNTIME_BACKEND", "disabled")
    monkeypatch.setenv("KSADK_SKILL_SPACE_IDS", "ss-1")
    monkeypatch.setattr(ADKRunner, "_apply_json_patch", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_short_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_long_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_knowledge_base", lambda self: None)
    monkeypatch.setattr(adk_runners, "Runner", FakeRunner)

    runner = ADKRunner(detection, str(tmp_path))
    runner.load_agent()

    tool_names = _tool_names(runner._agent.tools)
    assert tool_names.count("execute_skills") == 1
    assert "keep_tool" in tool_names


def test_adk_runner_load_agent_skips_skill_runtime_when_not_in_sandbox_mode(
    monkeypatch, tmp_path
):
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

    monkeypatch.setenv("KSADK_SKILLS_MODE", "local")
    monkeypatch.setattr(ADKRunner, "_apply_json_patch", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_short_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_long_term_memory", lambda self: None)
    monkeypatch.setattr(ADKRunner, "_init_knowledge_base", lambda self: None)
    monkeypatch.setattr(adk_runners, "Runner", FakeRunner)

    runner = ADKRunner(detection, str(tmp_path))
    runner.load_agent()

    assert _tool_names(runner._agent.tools) == []


def test_adk_runner_build_adk_content_supports_inline_and_reference_attachments(tmp_path):
    from ksadk.runners.adk_runner import ADKRunner

    detection = SimpleNamespace(
        entry_point="agent.py",
        agent_variable="root_agent",
        name="demo-agent",
    )
    runner = ADKRunner(detection, str(tmp_path))
    archive_path = tmp_path / "bundle.zip"
    archive_path.write_bytes(b"PK\x03\x04demo-zip")

    content = runner._build_adk_content(
        "请总结附件",
        [
            {
                "display_name": "notes.txt",
                "mime_type": "text/plain",
                "transport": "inline",
                "data": base64.b64encode("候选人简历内容".encode("utf-8")).decode("ascii"),
            },
            {
                "display_name": "bundle.zip",
                "mime_type": "application/zip",
                "transport": "reference",
                "file_uri": "ksadk-upload://abc123",
                "storage_path": str(archive_path),
            },
        ],
    )

    assert content.parts[0].text == "请总结附件"
    assert content.parts[1].inline_data.data == "候选人简历内容".encode("utf-8")
    assert content.parts[2].inline_data.data == b"PK\x03\x04demo-zip"


def test_adk_runner_build_adk_content_does_not_read_arbitrary_local_file_uri(tmp_path):
    from ksadk.runners.adk_runner import ADKRunner

    detection = SimpleNamespace(
        entry_point="agent.py",
        agent_variable="root_agent",
        name="demo-agent",
    )
    runner = ADKRunner(detection, str(tmp_path))

    secret_path = tmp_path / "secret.txt"
    secret_path.write_text("should-not-leak", encoding="utf-8")

    content = runner._build_adk_content(
        "请分析附件",
        [
            {
                "display_name": "secret.txt",
                "mime_type": "text/plain",
                "transport": "reference",
                "file_uri": f"local:{secret_path}",
            }
        ],
    )

    assert len(content.parts) == 1
    assert content.parts[0].text == "请分析附件"


def test_adk_runner_build_adk_content_skips_images_for_text_only_models(tmp_path):
    from ksadk.runners.adk_runner import ADKRunner

    detection = SimpleNamespace(
        entry_point="agent.py",
        agent_variable="root_agent",
        name="demo-agent",
    )
    runner = ADKRunner(detection, str(tmp_path))

    content = runner._build_adk_content(
        "请分析这张图",
        [
            {
                "display_name": "diagram.png",
                "mime_type": "image/png",
                "transport": "inline",
                "data": base64.b64encode(b"fake-png-bytes").decode("ascii"),
            }
        ],
        model_metadata={
            "capabilities": {
                "multimodal_input_image": False,
            }
        },
    )

    assert len(content.parts) == 2
    assert content.parts[0].text == "请分析这张图"
    assert "当前模型不支持图片输入" in content.parts[1].text


@pytest.mark.asyncio
async def test_adk_runner_invoke_forwards_attachment_results_via_state_delta(tmp_path, monkeypatch):
    from google.genai import types
    from ksadk.runners.adk_runner import ADKRunner

    detection = SimpleNamespace(
        entry_point="agent.py",
        agent_variable="root_agent",
        name="demo-agent",
    )
    runner = ADKRunner(detection, str(tmp_path))
    runner._agent = SimpleNamespace(name="demo-agent")

    captured: dict[str, Any] = {}

    class _FakeRunner:
        async def run_async(self, *, session_id, user_id, new_message, state_delta=None, run_config=None):
            captured["session_id"] = session_id
            captured["user_id"] = user_id
            captured["new_message"] = new_message
            captured["state_delta"] = state_delta
            yield SimpleNamespace(content=SimpleNamespace(parts=[types.Part(text="ok")]))

    async def _fake_ensure_session(external_session_id=None):
        return "adk-session-1"

    monkeypatch.setattr(runner, "_ensure_session", _fake_ensure_session)
    monkeypatch.setattr(runner, "_prepare_trace_metadata", lambda session_id: ("", [], "", "demo-agent"))
    runner._runner = _FakeRunner()

    result = await runner.invoke(
        {
            "session_id": "external-session",
            "input": "请分析附件",
            "attachments": [],
            "input_parts": [{"text": "请分析附件"}],
            "attachment_results": [{"display_name": "resume.pdf", "kind": "document"}],
            "current_attachments": [],
            "current_attachment_results": [{"display_name": "resume.pdf", "kind": "document"}],
            "has_current_files": True,
        }
    )

    assert result["output"] == "ok"
    assert captured["session_id"] == "adk-session-1"
    assert captured["state_delta"] == {
        "input_parts": [{"text": "请分析附件"}],
        "attachments": [],
        "attachment_results": [{"display_name": "resume.pdf", "kind": "document"}],
        "current_attachments": [],
        "current_attachment_results": [{"display_name": "resume.pdf", "kind": "document"}],
        "has_current_files": True,
    }


@pytest.mark.asyncio
async def test_adk_runner_invoke_extracts_usage_from_final_event(tmp_path, monkeypatch):
    from google.genai import types
    from ksadk.runners.adk_runner import ADKRunner

    detection = SimpleNamespace(
        entry_point="agent.py",
        agent_variable="root_agent",
        name="demo-agent",
    )
    runner = ADKRunner(detection, str(tmp_path))
    runner._agent = SimpleNamespace(name="demo-agent")

    class _FakeRunner:
        async def run_async(self, *, session_id, user_id, new_message, state_delta=None, run_config=None):
            del session_id, user_id, new_message, state_delta, run_config
            yield SimpleNamespace(
                usage_metadata={
                    "input_tokens": 12,
                    "output_tokens": 5,
                    "total_tokens": 17,
                    "input_token_details": {},
                    "output_token_details": {"reasoning": 2},
                },
                content=SimpleNamespace(parts=[types.Part(text="ok")]),
            )

    async def _fake_ensure_session(external_session_id=None):
        del external_session_id
        return "adk-session-usage"

    monkeypatch.setattr(runner, "_ensure_session", _fake_ensure_session)
    monkeypatch.setattr(runner, "_prepare_trace_metadata", lambda session_id: ("", [], "", "demo-agent"))
    runner._runner = _FakeRunner()

    result = await runner.invoke({"session_id": "external-session", "input": "hello"})

    assert result["output"] == "ok"
    assert result["usage"] == {
        "input_tokens": 12,
        "output_tokens": 5,
        "total_tokens": 17,
        "input_token_details": {},
        "output_token_details": {"reasoning": 2},
    }
