"""DeepAgents framework integration tests."""

from pathlib import Path

import pytest
import yaml

from ksadk.detection import FrameworkDetector, FrameworkType, DetectionResult
from ksadk.runners.factory import create_runner
from ksadk.runners.utils.loader import load_agent_module


def _write_deepagents_project(project_dir: Path) -> None:
    package_name = "deepagents_demo"
    package_dir = project_dir / package_name
    package_dir.mkdir(parents=True)

    (package_dir / "__init__.py").write_text(
        'from .agent import root_agent\n__all__ = ["root_agent"]\n',
        encoding="utf-8",
    )

    (package_dir / "agent.py").write_text(
        '''from collections.abc import Callable, Sequence
from typing import Any

from deepagents import create_deep_agent
from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool


class FixedGenericFakeChatModel(GenericFakeChatModel):
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        return self


fake_model = FixedGenericFakeChatModel(
    messages=iter(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_todos",
                        "args": {"todos": []},
                        "id": "call_1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="DeepAgents invoke ok"),
        ]
    )
)

root_agent = create_deep_agent(model=fake_model)
''',
        encoding="utf-8",
    )

    (project_dir / "agentengine.yaml").write_text(
        yaml.dump(
            {
                "name": "deepagents-demo",
                "framework": "deepagents",
                "entry_point": f"{package_name}/agent.py",
                "package": package_name,
                "agent_variable": "root_agent",
            }
        ),
        encoding="utf-8",
    )


def _write_deepagents_script_entry(project_dir: Path, entry_file: str) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / entry_file).write_text(
        """from deepagents import create_deep_agent

root_agent = create_deep_agent(model=None)
""",
        encoding="utf-8",
    )


def test_detector_supports_deepagents_from_config(tmp_path: Path):
    _write_deepagents_project(tmp_path)
    detector = FrameworkDetector(str(tmp_path))
    result = detector.detect()
    assert result.type == FrameworkType.DEEPAGENTS
    assert result.entry_point.endswith("deepagents_demo/agent.py")


def test_detector_reads_custom_runner_class_from_config(tmp_path: Path):
    package_dir = tmp_path / "demo_agent"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "agent.py").write_text("root_agent = object()\n", encoding="utf-8")
    (tmp_path / "agentengine.yaml").write_text(
        yaml.dump(
            {
                "name": "demo-agent",
                "framework": "langgraph",
                "entry_point": "demo_agent/agent.py",
                "package": "demo_agent",
                "agent_variable": "root_agent",
                "runner_class": "demo_agent.agent.CustomRunner",
            }
        ),
        encoding="utf-8",
    )

    result = FrameworkDetector(str(tmp_path)).detect()

    assert result.type == FrameworkType.LANGGRAPH
    assert result.runner_class == "demo_agent.agent.CustomRunner"


def test_detector_ignores_config_when_agent_variable_missing_and_finds_src_agent(tmp_path: Path):
    package_dir = tmp_path / "src" / "demo_agent"
    package_dir.mkdir(parents=True)
    (package_dir / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n",
        encoding="utf-8",
    )
    (package_dir / "agent.py").write_text(
        "from langchain_openai import ChatOpenAI\n"
        "from langchain_core.output_parsers import StrOutputParser\n"
        "root_agent = ChatOpenAI() | StrOutputParser()\n",
        encoding="utf-8",
    )
    (tmp_path / "agentengine.yaml").write_text(
        "name: demo-agent\nframework: langchain\nentry_point: src/demo_agent/main.py\nagent_variable: root_agent\n",
        encoding="utf-8",
    )

    result = FrameworkDetector(str(tmp_path)).detect()

    assert result.type == FrameworkType.LANGCHAIN
    assert result.entry_point == "src/demo_agent/agent.py"
    assert Path(result.package_path) == package_dir


def test_detector_reads_valid_langgraph_json_when_config_is_stale(tmp_path: Path):
    package_dir = tmp_path / "src" / "demo_agent"
    package_dir.mkdir(parents=True)
    (package_dir / "graph.py").write_text(
        "from langgraph.graph import StateGraph\n"
        "graph = StateGraph(dict).compile()\n",
        encoding="utf-8",
    )
    (tmp_path / "agentengine.yaml").write_text(
        "name: demo-agent\nframework: langgraph\nentry_point: src/demo_agent/main.py\nagent_variable: root_agent\n",
        encoding="utf-8",
    )
    (tmp_path / "langgraph.json").write_text(
        '{"graphs": {"agent": "./src/demo_agent/graph.py:graph"}}\n',
        encoding="utf-8",
    )

    result = FrameworkDetector(str(tmp_path)).detect()

    assert result.type == FrameworkType.LANGGRAPH
    assert result.entry_point == "src/demo_agent/graph.py"
    assert result.agent_variable == "graph"


@pytest.mark.parametrize("entry_file", ["agent.py", "main.py", "app.py"])
def test_detector_supports_script_project_without_package_init(tmp_path: Path, entry_file: str):
    nested_project = tmp_path / "deep" / "deep"
    _write_deepagents_script_entry(nested_project, entry_file)

    detector = FrameworkDetector(str(nested_project))
    result = detector.detect()

    assert result.type == FrameworkType.DEEPAGENTS
    assert result.entry_point == entry_file
    assert Path(result.package_path) == nested_project


def test_detector_supports_bom_encoded_agent_file(tmp_path: Path):
    nested_project = tmp_path / "deep" / "deep"
    nested_project.mkdir(parents=True, exist_ok=True)
    (nested_project / "agent.py").write_text(
        "\ufefffrom deepagents import create_deep_agent\nroot_agent = create_deep_agent(model=None)\n",
        encoding="utf-8",
    )

    detector = FrameworkDetector(str(nested_project))
    result = detector.detect()

    assert result.type == FrameworkType.DEEPAGENTS
    assert result.entry_point == "agent.py"


def test_loader_supports_src_layout_imports(tmp_path: Path):
    package_dir = tmp_path / "src" / "src_demo"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "helper.py").write_text("VALUE = 'src import ok'\n", encoding="utf-8")
    (package_dir / "agent.py").write_text(
        "from src_demo.helper import VALUE\n"
        "root_agent = VALUE\n",
        encoding="utf-8",
    )

    agent, module = load_agent_module(str(tmp_path), "src/src_demo/agent.py", "root_agent")

    assert agent == "src import ok"
    assert module.__name__ == "src.src_demo.agent"


def test_factory_creates_deepagents_runner(tmp_path: Path):
    detection = DetectionResult(
        type=FrameworkType.DEEPAGENTS,
        name="deepagents-demo",
        entry_point="deepagents_demo/agent.py",
        package_path=str(tmp_path / "deepagents_demo"),
        agent_variable="root_agent",
    )
    runner = create_runner(detection, str(tmp_path))
    assert runner.__class__.__name__ == "DeepAgentsRunner"


@pytest.mark.asyncio
async def test_create_runner_invoke_deepagents_e2e(tmp_path: Path):
    pytest.importorskip("deepagents")

    _write_deepagents_project(tmp_path)
    detector = FrameworkDetector(str(tmp_path))
    result = detector.detect()
    assert result.type == FrameworkType.DEEPAGENTS

    runner = create_runner(result, str(tmp_path))
    runner.load_agent()

    response = await runner.invoke({"input": "hello deepagents"})
    assert "output" in response
    assert "DeepAgents invoke ok" in response["output"]
