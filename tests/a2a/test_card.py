from __future__ import annotations

from a2a.types import AgentSkill

from ksadk.a2a.card import build_agent_card


def test_agent_card_preserves_standard_agent_skill_metadata() -> None:
    skill = AgentSkill(
        id="summarize",
        name="Summarize documents",
        description="Produces a concise document summary.",
        tags=["document", "summary"],
        examples=["Summarize the attached report."],
        input_modes=["text/plain", "application/pdf"],
        output_modes=["text/plain"],
    )

    card = build_agent_card(
        name="document-agent",
        base_url="https://runtime.internal",
        skills=(skill,),
    )

    assert list(card.skills) == [skill]
