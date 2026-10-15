"""Bounded, host-authored recovery feedback for malformed or empty model turns.

Never quote raw provider errors or partial tool arguments. Transport retries do
not become conversation messages. Call counters and retry limits remain owned by
the state machine; these observations grant no tool or completion authority.
"""

from __future__ import annotations

import json

from pydantic import JsonValue

from .model import ModelTurnResult
from .state import RunSnapshot

_INSTRUCTIONS = {
    "malformed_tool_call": (
        "The previous response did not contain a complete valid tool call. No tool from that "
        "response was executed. Continue the current task using only advertised tool names, "
        "complete JSON object arguments matching the tool schema, and distinct call IDs within "
        "one batch. Do not resend partial arguments. If no tool work remains, provide a grounded "
        "final response for verification."
    ),
    "empty_response": (
        "The previous response contained neither visible answer text nor a valid tool call. "
        "Continue the current task. Return a complete valid tool call when work remains, or a "
        "grounded visible final response for verification; reasoning alone does not complete "
        "the task."
    ),
}
_NO_TOOL_INSTRUCTIONS = {
    "malformed_tool_call": (
        "The previous response attempted tool-call syntax, but this turn has no available "
        "tools. No action from that response was executed. Do not emit function names, "
        "tool-call markup, or JSON tool envelopes. Answer from the tool observations "
        "already present; state any unfinished work or missing verification plainly."
    ),
    "empty_response": (
        "The previous response contained no visible answer. This turn has no available "
        "tools. Give a grounded visible answer from existing observations, and state "
        "unfinished work or missing verification plainly."
    ),
}


def model_feedback_messages(
    snapshot: RunSnapshot, result: ModelTurnResult, *, tools_available: bool = True
) -> tuple[dict[str, JsonValue], ...]:
    instructions = _INSTRUCTIONS if tools_available else _NO_TOOL_INSTRUCTIONS
    instruction = instructions.get(result.response_kind)
    if instruction is None:
        return snapshot.messages
    observation = {
        "role": "user",
        "kind": "model_recovery_observation",
        "content": json.dumps(
            {"reason": result.response_kind, "instruction": instruction}, sort_keys=True
        ),
    }
    if snapshot.messages and snapshot.messages[-1] == observation:
        return snapshot.messages
    return (*snapshot.messages, observation)
