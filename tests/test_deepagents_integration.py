"""DeepAgents framework integration tests."""

from pathlib import Path

import pytest
import yaml

from ksadk.detection import FrameworkDetector, FrameworkType, DetectionResult
from ksadk.runners.factory import create_runner
from ksadk.runners.unified_runner import UnifiedRunner


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
async def test_unified_runner_invoke_deepagents_e2e(tmp_path: Path):
    pytest.importorskip("deepagents")

    _write_deepagents_project(tmp_path)
    detector = FrameworkDetector(str(tmp_path))
    result = detector.detect()
    assert result.type == FrameworkType.DEEPAGENTS

    runner = UnifiedRunner.create(result, str(tmp_path))
    runner.load_agent()

    response = await runner.invoke({"input": "hello deepagents"})
    assert "output" in response
    assert "DeepAgents invoke ok" in response["output"]
