"""Explicit interaction-suspension result (Native Harness Phase 1).

Replaces LangGraph ``interrupt()`` inside phase handlers. The handler returns a
``NeedInteraction`` instead of raising; the calling engine decides how to
suspend (LangGraph calls ``interrupt()``, Native Engine writes a
``PendingInteraction`` to the snapshot).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class NeedInteraction:
    """A phase handler requests suspension pending an external interaction.

    Fields mirror native-harness-engine-technical-design.md §11.3
    PendingInteraction, minus the expiry clock (owned by the engine, not the
    handler).
    """

    interaction_id: str
    kind: Literal["tool_approval", "child_approval", "user_question"]
    request_payload: dict[str, Any]
    resume_phase: str
    expected_revision: int


__all__ = ["NeedInteraction"]
