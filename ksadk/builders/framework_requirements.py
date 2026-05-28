"""Validated dependency windows for supported Python agent frameworks."""

from __future__ import annotations

from typing import Iterable


FASTAPI_REQUIREMENT = "fastapi>=0.100.0,<1.0.0"

ADK_REQUIREMENTS = (
    "google-adk>=1.34.0,<2.0.0",
    "litellm>=1.0.0",
)

LANGCHAIN_ECOSYSTEM_REQUIREMENTS = (
    "langchain>=1.3.0,<2.0.0",
    "langchain-openai>=1.2.0,<2.0.0",
    "langchain-core>=1.4.0,<2.0.0",
    "langgraph>=1.2.0,<1.3.0",
)

DEEPAGENTS_REQUIREMENTS = (
    "deepagents>=0.6.2,<1.0.0",
)


def requirements_for_framework(framework: str) -> list[str]:
    normalized = (framework or "").strip().lower()
    if normalized == "adk":
        return list(ADK_REQUIREMENTS)
    if normalized in {"langchain", "langgraph", "deepagents"}:
        requirements = list(LANGCHAIN_ECOSYSTEM_REQUIREMENTS)
        if normalized == "deepagents":
            requirements.extend(DEEPAGENTS_REQUIREMENTS)
        return requirements
    return []


def minimal_requirements_for_framework(framework: str) -> list[str]:
    """Return deploy-manager requirements without optional MCP adapter packages."""
    normalized = (framework or "").strip().lower()
    if normalized == "adk":
        return list(ADK_REQUIREMENTS)
    if normalized in {"langchain", "langgraph", "deepagents"}:
        requirements = [
            "langchain>=1.3.0,<2.0.0",
            "langchain-openai>=1.2.0,<2.0.0",
            "langchain-core>=1.4.0,<2.0.0",
        ]
        if normalized in {"langgraph", "deepagents"}:
            requirements.append("langgraph>=1.2.0,<1.3.0")
        if normalized == "deepagents":
            requirements.extend(DEEPAGENTS_REQUIREMENTS)
        return requirements
    return []


def as_lines(requirements: Iterable[str]) -> str:
    return "\n".join(requirements)
