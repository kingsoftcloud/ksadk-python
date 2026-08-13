"""Validated dependency windows for supported Python agent frameworks."""

from __future__ import annotations

from typing import Iterable

FASTAPI_REQUIREMENT = "fastapi>=0.136.0,<0.137.0"
STARLETTE_REQUIREMENT = "starlette>=1.0,<1.4"

ADK_REQUIREMENTS = (
    # goal-00: 与 ksadk 自身 adk extra 对齐,允许 1.34.x 与 2.x
    "google-adk>=1.34.0,<3.0.0",
    "litellm>=1.0.0",
)

LANGCHAIN_ECOSYSTEM_REQUIREMENTS = (
    "langchain>=1.3.14,<2.0.0",
    "langchain-openai>=1.4.0,<2.0.0",
    "langchain-core>=1.5.0,<2.0.0",
    "langgraph>=1.2.0,<1.3.0",
)

DEEPAGENTS_REQUIREMENTS = ("deepagents>=0.6.2,<1.0.0",)

# codex runtime:openai-codex SDK(自带 codex CLI 二进制,见 PyPI cli-bin wheel)
CODEX_REQUIREMENTS = ("openai-codex==0.144.4",)


def code_requirements_for_framework(framework: str) -> list[str]:
    """Dependencies safe to install into a portable Code artifact.

    Codex carries a platform-selected executable package. ManagedRuntime code
    bundles must remain system independent, so Codex is deliberately excluded
    from this dependency policy.
    """
    normalized = (framework or "").strip().lower()
    if normalized == "codex":
        return []
    return requirements_for_framework(normalized)


def requirements_for_framework(framework: str) -> list[str]:
    normalized = (framework or "").strip().lower()
    if normalized == "adk":
        return list(ADK_REQUIREMENTS)
    if normalized == "codex":
        return list(CODEX_REQUIREMENTS)
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
    if normalized == "codex":
        return list(CODEX_REQUIREMENTS)
    if normalized in {"langchain", "langgraph", "deepagents"}:
        requirements = [
            "langchain>=1.3.14,<2.0.0",
            "langchain-openai>=1.4.0,<2.0.0",
            "langchain-core>=1.5.0,<2.0.0",
        ]
        if normalized in {"langgraph", "deepagents"}:
            requirements.append("langgraph>=1.2.0,<1.3.0")
        if normalized == "deepagents":
            requirements.extend(DEEPAGENTS_REQUIREMENTS)
        return requirements
    return []


def as_lines(requirements: Iterable[str]) -> str:
    return "\n".join(requirements)
