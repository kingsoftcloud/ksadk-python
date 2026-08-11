from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ksadk.builders.code_builder import CodeBuilder
from ksadk.builders.container_builder import ContainerBuilder
from ksadk.deployment.manager import K8sDeployer
from ksadk.detection import DetectionResult, FrameworkType


@pytest.fixture
def detection(tmp_path: Path) -> DetectionResult:
    package = tmp_path / "demo_agent"
    package.mkdir()
    return DetectionResult(
        type=FrameworkType.LANGGRAPH,
        name="demo-agent",
        entry_point="demo_agent/agent.py",
        package_path=str(package),
        agent_variable="root_agent",
        confidence=1.0,
    )


@pytest.mark.parametrize("source", ["code", "container", "deployment"])
def test_generated_runtime_entrypoints_only_compose_runtime_executor(
    source: str,
    detection: DetectionResult,
    tmp_path: Path,
) -> None:
    if source == "code":
        entrypoint = CodeBuilder(tmp_path)._generate_entrypoint(detection)
    elif source == "container":
        entrypoint = ContainerBuilder(tmp_path)._generate_entrypoint(
            detection,
            "demo_agent",
        )
    else:
        entrypoint = K8sDeployer()._generate_entrypoint(detection)

    ast.parse(entrypoint)
    assert "RuntimeExecutor" in entrypoint
    assert "RuntimeLaunchContext" in entrypoint
    assert "create_runtime_app" in entrypoint
    assert "create_runner" not in entrypoint
    assert "set_runner" not in entrypoint
    assert "ksadk.runners" not in entrypoint
