"""Phase handler input/output containers (Native Harness Phase 1).

Handlers receive a PhaseContext (explicit dependencies, no closure capture of
engine/run) and return a PhaseResult or NeedInteraction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ksadk.harness.engine.phases.interaction import NeedInteraction
from ksadk.harness.events import RuntimeEvent
from ksadk.harness.spec import HarnessSpec


@dataclass(frozen=True)
class PhaseContext:
    """Dependencies a phase handler needs, assembled by the calling engine.

    ``run`` and ``engine`` are typed Any on purpose: handlers use only the
    documented surface (events/seq/budget/controller on run; context_pipeline
    on engine) and never import LangGraph via these.
    """

    spec: HarnessSpec
    run: Any
    engine: Any
    turn_id: str
    state: dict[str, Any]


@dataclass
class PhaseResult:
    """Outcome of one phase: updated state slice, emitted events, route.

    ``interaction`` non-None means the handler requests suspension — the engine
    translates it to its suspension primitive (LangGraph ``interrupt()`` or a
    Native snapshot PendingInteraction).
    """

    state: dict[str, Any]
    events: list[RuntimeEvent]
    route: str | None = None
    interaction: NeedInteraction | None = None


__all__ = ["PhaseContext", "PhaseResult"]
