from __future__ import annotations

from pathlib import Path

import pytest

from ksadk.builders.code_builder import CodeBuilder
from ksadk.builders.container_builder import ContainerBuilder
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


@pytest.mark.parametrize("source", ["code", "container"])
def test_managed_entrypoint_separates_discovery_from_full_product_composition(
    source: str, detection: DetectionResult, tmp_path: Path
) -> None:
    if source == "code":
        entrypoint = CodeBuilder(tmp_path)._generate_entrypoint(detection)
    else:
        entrypoint = ContainerBuilder(tmp_path)._generate_entrypoint(detection, "demo_agent")

    assert "build_managed_a2a_bootstrap_from_env" in entrypoint
    assert "KSADK_A2A_MANAGED_BOOTSTRAP_ENABLED" in entrypoint
    assert "build_managed_a2a_card_if_configured" in entrypoint
    assert "sqlite+aiosqlite:///.agentengine/a2a_tasks.db" not in entrypoint
    assert "except Exception as _e" not in entrypoint
