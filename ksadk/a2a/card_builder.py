"""Helpers for building A2A agent cards."""

from __future__ import annotations

from collections.abc import Sequence

from a2a.types import AgentCapabilities, AgentCard, AgentSkill

_DEFAULT_IO_MODES = ["text/plain"]


class AgentCardBuilder:
    """Build an A2A AgentCard for a KsADK runner."""

    def __init__(
        self,
        name: str,
        url: str,
        description: str = "",
        skills: Sequence[str] | None = None,
        version: str = "1.0.0",
    ) -> None:
        self.name = name
        self.url = url.rstrip("/") or url
        self.description = description or f"{name} agent powered by ksadk"
        self.skills = list(skills or [])
        self.version = version

    def build(self) -> AgentCard:
        """Create the final A2A agent card."""
        return AgentCard(
            name=self.name,
            description=self.description,
            url=self.url,
            version=self.version,
            capabilities=AgentCapabilities(
                streaming=True,
                push_notifications=False,
            ),
            default_input_modes=list(_DEFAULT_IO_MODES),
            default_output_modes=list(_DEFAULT_IO_MODES),
            skills=self._build_skills(),
        )

    def _build_skills(self) -> list[AgentSkill]:
        if not self.skills:
            return [
                AgentSkill(
                    id="general",
                    name="General",
                    description="General purpose agent powered by ksadk",
                    tags=["general"],
                )
            ]

        return [
            AgentSkill(
                id=skill,
                name=skill.replace("_", " ").title(),
                description=f"Skill: {skill}",
                tags=[skill],
            )
            for skill in self.skills
        ]
