"""Host-owned model-turn authority, separate from historical task messages."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from .state import Contract

EXHAUSTED_INSTRUCTION = (
    "The runtime task-tool-call budget is exhausted. No task tools are available for this "
    "turn. Answer using only observations already received. State any unfinished "
    "work or missing verification plainly; do not claim it was completed."
)
FINALIZATION_INSTRUCTION = (
    "The runtime model-call budget is near its limit. This is the finalization "
    "turn and no task tools are available. Report only what the current evidence "
    "supports, including unfinished work and missing verification."
)
VERIFICATION_INSTRUCTION = (
    "The web delivery budget is in its verification window. Workspace-changing "
    "tools are unavailable. Inspect the current page with the available browser "
    "tool before claiming it works; report any remaining gap truthfully."
)
OBSERVATION_DUE_INSTRUCTION = (
    "The current web revision needs a browser observation before more workspace "
    "work. Only browser observation tools are available on this turn. Inspect "
    "the current page; the next turn can use the resulting evidence to repair "
    "any observed problem."
)
WALL_DELIVERY_INSTRUCTION = (
    "The runtime is in its reserved deadline delivery window. Stop exploration "
    "and further research. Use only the currently admitted delivery tools to "
    "persist the requested deliverable from evidence already received, then "
    "answer. Disclose unsupported conclusions, incomplete work and missing "
    "verification honestly. This window grants no extra calls, permissions, "
    "verification pass or deadline extension."
)


class ModelTurnControl(Contract):
    """A request projection of Kernel admission; it grants no verification pass.

    Rendered after complete historical tool receipts, never stored as a new
    user goal or assistant answer. The Kernel still refuses prohibited calls
    even when a provider ignores this control.
    """

    reason: Literal[
        "model_budget_reserved", "task_tools_exhausted",
        "browser_observation_due", "browser_verification_reserved",
        "wall_delivery_reserved",
    ]
    final_submission_required: bool = Field(default=False, strict=True)
    # Current admission, not the Agent's full registered catalog. Omission
    # preserves finalization controls captured by older durable snapshots.
    admitted_tool_names: tuple[str, ...] | None = None

    @property
    def finalization_only(self) -> bool:
        return self.reason in {"model_budget_reserved", "task_tools_exhausted"}

    @property
    def tool_choice(self) -> Literal["auto", "required", "none"]:
        # This admission already validates a singleton submit_final_output
        # surface. Convey that existing authority to the provider; ordinary
        # and observation turns still choose whether to use a task tool.
        if self.reason == "wall_delivery_reserved" and not self.admitted_tool_names:
            return "none"
        return "required" if self.finalization_only and self.final_submission_required else "auto"

    @model_validator(mode="after")
    def admission_is_explicit(self) -> ModelTurnControl:
        names = self.admitted_tool_names
        if names is not None and (any(not name.strip() for name in names) or len(set(names)) != len(names)):
            raise ValueError("admitted tool names must be nonblank and unique")
        if not self.finalization_only and self.reason != "wall_delivery_reserved" and (
            not names or any(not name.strip() for name in names) or len(set(names)) != len(names)
        ):
            raise ValueError("observation control requires explicit admitted tool names")
        if self.reason == "wall_delivery_reserved" and names is None:
            raise ValueError("deadline delivery requires an explicit admitted surface")
        if names is not None and self.final_submission_required != ("submit_final_output" in names):
            raise ValueError("turn control conflicts with structured submission admission")
        return self

    @property
    def instruction(self) -> str:
        instruction = {
            "model_budget_reserved": FINALIZATION_INSTRUCTION,
            "task_tools_exhausted": EXHAUSTED_INSTRUCTION,
            "browser_observation_due": OBSERVATION_DUE_INSTRUCTION,
            "browser_verification_reserved": VERIFICATION_INSTRUCTION,
            "wall_delivery_reserved": WALL_DELIVERY_INSTRUCTION,
        }[self.reason]
        if self.reason == "wall_delivery_reserved":
            instruction += " Admitted tool names: " + json.dumps(self.admitted_tool_names) + "."
        if self.final_submission_required:
            instruction += (
                " For final delivery, call submit_final_output with arguments matching its "
                "schema. A plain-text final response cannot satisfy this output contract."
            )
        return instruction

    def message(self) -> dict[str, JsonValue]:
        return {"role": "user", "content": self.instruction}

    def validate_tools(self, names: tuple[str, ...], *, stable_catalog: bool = False) -> None:
        expected = (
            ("submit_final_output",) if self.final_submission_required else ()
        ) if self.finalization_only else self.admitted_tool_names
        valid = (len(names) == len(set(names)) and set(expected).issubset(names)
                 if stable_catalog else names == expected)
        if not valid:
            raise ValueError("turn control conflicts with the admitted tool surface")
