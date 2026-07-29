"""Configuration and dependency gates for the optional AG-UI transport."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any

AGUI_PROTOCOL_VERSION = "0.1.19"
AGUI_LANGGRAPH_VERSION = "0.0.42"
COPILOTKIT_VERSION = "0.1.94"

_REQUIRED_DISTRIBUTIONS = {
    "ag-ui-protocol": AGUI_PROTOCOL_VERSION,
    "ag-ui-langgraph": AGUI_LANGGRAPH_VERSION,
    "copilotkit": COPILOTKIT_VERSION,
}


@dataclass(frozen=True)
class AGUIConfig:
    """Optional per-app AG-UI endpoint configuration."""

    enabled: bool = False
    path: str = "/agentengine/agui"
    agent_name: str = "ksadk"
    runtime_type: str = "langgraph"
    preferred: bool = True


def agui_dependency_errors() -> list[str]:
    """Return exact-version dependency mismatches without importing AG-UI eagerly."""
    errors: list[str] = []
    for distribution, expected in _REQUIRED_DISTRIBUTIONS.items():
        try:
            actual = version(distribution)
        except PackageNotFoundError:
            errors.append(f"{distribution}=={expected} is not installed")
            continue
        if actual != expected:
            errors.append(f"{distribution}=={expected} required, found {actual}")
    return errors


def agui_dependencies_available() -> bool:
    return not agui_dependency_errors()


def default_agui_config(runner: Any) -> AGUIConfig:
    """Build the production AG-UI config without loading the user's agent.

    Goal 24's first production vertical is LangGraph.  An optional AG-UI install
    therefore enables the endpoint automatically only for a detected LangGraph
    runner; explicit ``RuntimeAppConfig.agui`` remains available to harnesses and
    future framework adapters.
    """
    detection = getattr(runner, "detection_result", None)
    framework_type = getattr(detection, "type", None)
    runtime_type = str(getattr(framework_type, "value", framework_type) or "").lower()
    agent_name = str(getattr(detection, "name", None) or "ksadk")
    return AGUIConfig(
        enabled=runtime_type in ("langgraph", "codex") and agui_dependencies_available(),
        agent_name=agent_name,
        runtime_type=runtime_type or "unknown",
    )


def require_agui_dependencies() -> None:
    errors = agui_dependency_errors()
    if errors:
        detail = "; ".join(errors)
        raise RuntimeError(
            f"AG-UI transport is enabled but unavailable: {detail}. Install ksadk[agui]."
        )


__all__ = [
    "AGUIConfig",
    "AGUI_LANGGRAPH_VERSION",
    "AGUI_PROTOCOL_VERSION",
    "COPILOTKIT_VERSION",
    "agui_dependencies_available",
    "agui_dependency_errors",
    "default_agui_config",
    "require_agui_dependencies",
]
