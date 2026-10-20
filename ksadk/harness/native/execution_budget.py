"""Persist a conservative host budget quote before context planning.

This is an observation at a preparation boundary, never a deadline extension,
permission grant, new task or claim that a delivery has passed verification.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EXECUTION_BUDGET_POLICY = "prepared-execution-budget/v1"


class ExecutionBudgetObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy: Literal["prepared-execution-budget/v1"] = EXECUTION_BUDGET_POLICY
    run_id: str = Field(min_length=1)
    source_revision: int = Field(strict=True, ge=0)
    goal_revision: int = Field(strict=True, ge=0)
    model_calls_claimed: int = Field(strict=True, ge=0)
    tool_calls_claimed: int = Field(strict=True, ge=0)
    model_calls_remaining: int | None = Field(ge=0)
    tool_calls_remaining: int | None = Field(ge=0)
    wall_seconds_remaining: int | None = Field(ge=0)
    authority: Literal["budget_observation_only"] = "budget_observation_only"
    guidance: str = (
        "Remaining allowances were observed by the host before preparing this input; "
        "null means unbounded in that dimension. Time may have elapsed since observation. "
        "Plan source exploration, required delivery and verification within the remaining "
        "allowances. As time diminishes, prioritize completing the requested deliverable "
        "with earned evidence; report any incomplete work honestly. This observation "
        "does not change tool permissions, acceptance requirements or the hard deadline."
    )

    def matches(self, snapshot) -> bool:
        return (
            self.run_id == snapshot.run_id
            and self.goal_revision == snapshot.goal_revision
            and self.model_calls_claimed == snapshot.model_budget_claimed
            and self.tool_calls_claimed == snapshot.tool_budget_claimed
            and self.source_revision < snapshot.revision
        )


def observe_budget(snapshot, config, *, wall_remaining):
    """Use effective chain call limits and the kernel's exact wall stop quote."""
    from .transitions import commit_transition

    previous = snapshot.execution_budget_observation
    reserve = config.limits.finalization_reserve_wall_seconds
    crossed_delivery_window = (
        reserve and wall_remaining is not None and wall_remaining <= reserve
        and previous is not None and previous.wall_seconds_remaining is not None
        and previous.wall_seconds_remaining > reserve
    )
    if previous is not None and previous.matches(snapshot) and not crossed_delivery_window:
        return None
    limits = config.limits
    observation = ExecutionBudgetObservation(
        run_id=snapshot.run_id,
        source_revision=snapshot.revision,
        goal_revision=snapshot.goal_revision,
        model_calls_claimed=snapshot.model_budget_claimed,
        tool_calls_claimed=snapshot.tool_budget_claimed,
        model_calls_remaining=(
            None
            if limits.max_model_calls is None
            else max(0, limits.max_model_calls - snapshot.model_budget_claimed)
        ),
        tool_calls_remaining=(
            None
            if limits.max_tool_calls is None
            else max(0, limits.max_tool_calls - snapshot.tool_budget_claimed)
        ),
        wall_seconds_remaining=(
            None if wall_remaining is None else max(0, math.floor(wall_remaining))
        ),
    )
    return commit_transition(
        snapshot,
        snapshot.model_copy(update={"execution_budget_observation": observation}),
        "budget.observed",
        observation.model_dump(mode="json"),
    )
