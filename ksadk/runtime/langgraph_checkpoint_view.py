"""A fresh user turn must not execute an abandoned checkpoint's tool calls."""

from __future__ import annotations

import hashlib
import inspect
from contextvars import ContextVar
from functools import wraps
from typing import Any

# This applies only to the first latest-root checkpoint read of a new input.
# Explicit checkpoint/approval resumes and ordinary history reads see the exact
# original checkpoint. Context variables isolate concurrent sessions and tasks.
_FRESH_READ: ContextVar[dict[str, bool] | None] = ContextVar(
    "langgraph_fresh_user_checkpoint", default=None,
)


def fresh_user_checkpoint(function):
    def fresh(payload):
        return bool(
            not payload.get("resume") and not payload.get("checkpoint_resume")
            and (payload.get("input") or payload.get("messages"))
        )

    if inspect.isasyncgenfunction(function):
        @wraps(function)
        async def generate(self, input_data):
            context = {"used": False} if fresh(input_data) else None
            results = function(self, input_data)
            try:
                while True:
                    # Some adapters drive each anext in a different Task.
                    # Never carry a ContextVar token across a public yield.
                    token = _FRESH_READ.set(context)
                    try:
                        item = await results.__anext__()
                    except StopAsyncIteration:
                        return
                    finally:
                        _FRESH_READ.reset(token)
                    yield item
            finally:
                token = _FRESH_READ.set(context)
                try:
                    await results.aclose()
                finally:
                    _FRESH_READ.reset(token)
        return generate

    @wraps(function)
    async def invoke(self, input_data):
        token = _FRESH_READ.set({"used": False} if fresh(input_data) else None)
        try:
            return await function(self, input_data)
        finally:
            _FRESH_READ.reset(token)
    return invoke


def consume_fresh_root_read(config: Any) -> bool:
    context = _FRESH_READ.get()
    address = config.get("configurable", {})
    if (context is None or context["used"] or address.get("checkpoint_id")
            or address.get("checkpoint_ns")):
        return False
    context["used"] = True
    return True


def fresh_checkpoint_view(config: Any, saved: Any) -> Any:
    """Close unmatched calls in a *new input's read view*, never replay them.

    No extra DB read is needed. The original checkpoint remains immutable; the
    graph commits this view together with the new user input in its next normal
    checkpoint. Native pending tool results take precedence over unknown output.
    """
    if not consume_fresh_root_read(config):
        return saved
    if saved is None:
        return saved
    from langchain_core.messages import AIMessage, ToolMessage

    messages = saved.checkpoint.get("channel_values", {}).get("messages")
    if not isinstance(messages, list):
        return saved
    pending = list(saved.pending_writes or [])
    if any(channel == "__interrupt__" for _, channel, _ in pending):
        return saved
    # A custom graph can append other messages before it records a tool receipt.
    # Match each receipt to the most recent preceding call with that provider ID.
    call_positions = {}
    results_by_call = {}
    result_positions = set()
    changed = False
    for index, message in enumerate(messages):
        if isinstance(message, AIMessage) and message.tool_calls:
            for call in message.tool_calls:
                call_positions[call.get("id")] = index
        elif isinstance(message, ToolMessage) and message.tool_call_id in call_positions:
            owner = call_positions[message.tool_call_id]
            results = results_by_call.setdefault(owner, [])
            results.append(message)
            result_positions.add(index)
            # Strict providers require the receipts immediately after their AI message.
            if index != owner + len(results):
                changed = True
    pending_results = {}
    updated_pending = []
    for task, channel, value in pending:
        if channel == "messages":
            values = value if isinstance(value, list) else [value]
            normalized = []
            for message in values:
                if isinstance(message, ToolMessage):
                    if not message.id:
                        identity = hashlib.sha256(
                            f"{saved.checkpoint['id']}:{message.tool_call_id}".encode()
                        ).hexdigest()
                        message = message.model_copy(update={"id": f"ksadk-result-{identity}"})
                    pending_results[message.tool_call_id] = message
                normalized.append(message)
            value = normalized if isinstance(value, list) else normalized[0]
        updated_pending.append((task, channel, value))

    repaired = []
    for index, message in enumerate(messages):
        if index in result_positions:
            continue  # Emitted with the original call, in receipt order.
        repaired.append(message)
        if not isinstance(message, AIMessage):
            continue
        results = results_by_call.get(index, ())
        repaired.extend(results)
        answered = {result.tool_call_id for result in results}
        for call in message.tool_calls:
            call_id = call.get("id")
            if not call_id or call_id in answered:
                continue
            result = pending_results.get(call_id) if index == call_positions[call_id] else None
            if result is None:
                identity = hashlib.sha256(
                    f"{saved.checkpoint['id']}:{index}:{call_id}".encode()
                ).hexdigest()
                result = ToolMessage(
                    id=f"ksadk-interrupted-{identity}", tool_call_id=call_id,
                    name=call.get("name"), status="error",
                    content=(
                        "The previous turn ended without a committed tool result. "
                        "Its outcome is unknown. This is a history recovery marker, "
                        "not a tool execution result. Do not retry that old call or "
                        "claim it succeeded. Handle the new user request separately."
                    ),
                    additional_kwargs={"ksadk_history_recovery": {
                        "outcome": "unknown", "replayed": False,
                    }},
                )
            repaired.append(result)
            answered.add(call_id)
            changed = True
    if not changed:
        return saved
    return saved._replace(
        checkpoint={**saved.checkpoint, "channel_values": {
            **saved.checkpoint["channel_values"], "messages": repaired,
        }},
        pending_writes=updated_pending,
    )
