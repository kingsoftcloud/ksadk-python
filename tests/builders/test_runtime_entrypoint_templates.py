from __future__ import annotations

import ast
import os
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


@pytest.mark.parametrize("source", ["code", "container"])
def test_generated_runtime_entrypoint_starts_without_managed_a2a(
    source: str,
    detection: DetectionResult,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal deployment must not require managed A2A environment injection."""
    monkeypatch.delenv("KSADK_A2A_RUNTIME_ID", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.delenv("CLOUD_MONITOR_OTLP_TRACES_HEADERS", raising=False)
    monkeypatch.delenv("CLOUD_MONITOR_OTLP_HEADERS", raising=False)
    monkeypatch.delenv("CLOUD_MONITOR_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("CLOUD_MONITOR_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.delenv("CLOUD_MONITOR_APP_KEY", raising=False)
    monkeypatch.setenv("CODE_PATH", str(tmp_path))
    monkeypatch.setattr(os, "chdir", lambda _path: None)

    if source == "code":
        entrypoint = CodeBuilder(tmp_path)._generate_entrypoint(detection)
    else:
        entrypoint = ContainerBuilder(tmp_path)._generate_entrypoint(detection, "demo_agent")

    namespace = {"__name__": "generated_entrypoint_probe"}
    exec(compile(entrypoint, f"<{source}-entrypoint>", "exec"), namespace)

    assert namespace["app"] is not None
