"""Deterministic SoulDocument rendering and prompt compilation helpers."""
from __future__ import annotations

import hashlib
import json

from ksadk.studio.contracts import Instructions, SoulDocument


def render_soul_markdown(soul: SoulDocument) -> str:
    """Render a portable ``soul.md`` snapshot without executable directives."""

    lines = ["# Soul", "", "## Identity", soul.identity.strip()]
    if soul.principles:
        lines.extend(["", "## Principles", *[f"- {item.strip()}" for item in soul.principles]])
    if soul.boundaries:
        lines.extend(["", "## Boundaries", *[f"- {item.strip()}" for item in soul.boundaries]])
    if soul.tone:
        lines.extend(["", "## Tone", soul.tone.strip()])
    return "\n".join(lines).rstrip() + "\n"


def soul_digest(soul: SoulDocument) -> str:
    """Content-address the reviewed structured source, not rendered whitespace."""

    payload = soul.model_dump(by_alias=True, exclude_none=True, mode="json")
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def compose_system_instruction(
    instructions: Instructions, soul: SoulDocument | None
) -> Instructions:
    """Put the reviewed identity before mutable task-specific system guidance."""

    if soul is None:
        return instructions
    soul_section = render_soul_markdown(soul).rstrip()
    system = "\n\n".join(
        part
        for part in (
            soul_section,
            instructions.system.strip(),
        )
        if part
    )
    return instructions.model_copy(update={"system": system})


__all__ = ["compose_system_instruction", "render_soul_markdown", "soul_digest"]
