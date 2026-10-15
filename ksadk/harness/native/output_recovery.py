"""Choose an output recovery contract from host policy and actual admission."""

from typing import Literal

from .ports import RunConfiguration
from .state import RunSnapshot
from .tool_budget import model_tool_surface


def output_recovery_mode(
    snapshot: RunSnapshot, configuration: RunConfiguration,
) -> Literal["suffix", "task_resume"]:
    if configuration.output_recovery_policy != "task-or-delivery/v1":
        return "suffix"
    if configuration.final_output_format != "text":
        return "suffix"
    if snapshot.messages:
        last = snapshot.messages[-1]
        if (last.get("kind") == "continuation_observation"
                and last.get("continuation_mode", "suffix") == "suffix"):
            # Preserve a contiguous suffix chain, including restored old frames.
            return "suffix"
    # Use actual host admission, not catalog visibility or goal/prompt guessing.
    return "task_resume" if model_tool_surface(snapshot, configuration) else "suffix"
