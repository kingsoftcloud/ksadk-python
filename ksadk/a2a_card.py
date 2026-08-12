"""Lightweight A2A AgentCard construction shared by discovery and data-plane code.

This module deliberately lives outside :mod:`ksadk.a2a`: importing that package
initializes the full v2 protocol stack, including optional database adapters.
Managed v1 discovery must be able to start with only the core FastAPI A2A
dependency installed.
"""

from __future__ import annotations

from collections.abc import Sequence

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

A2A_PROTOCOL_VERSION = "1.0"
JSONRPC_PATH = "/a2a/jsonrpc"
REST_PATH_PREFIX = "/a2a/v1"

_DEFAULT_IO_MODES = ["text/plain"]


def build_agent_card(
    *,
    name: str,
    base_url: str,
    description: str = "",
    version: str = "1.0.0",
    skills: Sequence[str | AgentSkill] | None = None,
    streaming: bool = True,
    provider: dict[str, str] | None = None,
) -> AgentCard:
    """Build a wire-1.0 AgentCard with supported interfaces."""
    base = base_url.rstrip("/")
    supported_interfaces = [
        AgentInterface(
            url=f"{base}{JSONRPC_PATH}",
            protocol_binding="JSONRPC",
            protocol_version=A2A_PROTOCOL_VERSION,
        ),
        AgentInterface(
            url=f"{base}{REST_PATH_PREFIX}",
            protocol_binding="HTTP+JSON",
            protocol_version=A2A_PROTOCOL_VERSION,
        ),
    ]

    card_kwargs: dict = {
        "name": name,
        "description": description or f"{name} agent powered by ksadk",
        "version": version,
        "supported_interfaces": supported_interfaces,
        "capabilities": AgentCapabilities(streaming=streaming, push_notifications=False),
        "default_input_modes": list(_DEFAULT_IO_MODES),
        "default_output_modes": list(_DEFAULT_IO_MODES),
        "skills": _build_skills(skills),
    }
    if provider:
        from a2a.types import AgentProvider

        card_kwargs["provider"] = AgentProvider(
            organization=provider.get("organization", ""),
            url=provider.get("url", ""),
        )
    return AgentCard(**card_kwargs)


def _build_skills(skills: Sequence[str | AgentSkill] | None) -> list[AgentSkill]:
    if not skills:
        return [
            AgentSkill(
                id="general",
                name="General",
                description="General purpose agent powered by ksadk",
                tags=["general"],
            )
        ]
    result: list[AgentSkill] = []
    for skill in skills:
        if isinstance(skill, AgentSkill):
            result.append(skill)
            continue
        if not isinstance(skill, str) or not skill.strip():
            raise TypeError("AgentCard skills must be non-empty strings or AgentSkill objects")
        skill_id = skill.strip()
        result.append(
            AgentSkill(
                id=skill_id,
                name=skill_id.replace("_", " ").title(),
                description=f"Skill: {skill_id}",
                tags=[skill_id],
            )
        )
    return result


__all__ = [
    "A2A_PROTOCOL_VERSION",
    "JSONRPC_PATH",
    "REST_PATH_PREFIX",
    "build_agent_card",
]
