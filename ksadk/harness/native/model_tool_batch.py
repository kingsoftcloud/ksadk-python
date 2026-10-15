"""Seal a model-declared tool batch as transcript and pending calls."""

from __future__ import annotations

import json

from .history import normalize_tool_identities
from .model import ModelTurnResult
from .model_replay import replay_fields
from .state import RunPhase, RunSnapshot, move


def begin_tool_batch(
    snapshot: RunSnapshot, updated: RunSnapshot, result: ModelTurnResult
) -> RunSnapshot:
    result = normalize_tool_identities(snapshot, result)
    tools = tuple(call.model_dump(mode="json") for call in result.tool_calls)
    message = {
        "role": "assistant",
        "content": result.text or None,
        **replay_fields(result.provider_replay),
        "tool_calls": [
            {
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in result.tool_calls
        ],
    }
    return move(updated, RunPhase.EXECUTING_TOOLS).model_copy(
        update={
            "pending_tools": tools,
            "messages": (*snapshot.messages, message),
            "model_errors": 0,
            # The output-limit cap applies to one contiguous response chain.
            # A valid tool batch breaks that chain, even in a long Run.
            "continuations": 0,
        }
    )
