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


def fresh_checkpoint_view(config: Any, saved: Any) -> Any:
    """Close unmatched calls in a *new input's read view*, never replay them.

    No extra DB read is needed. The original checkpoint remains immutable; the
    graph commits this view together with the new user input in its next normal
    checkpoint. Native pending tool results take precedence over unknown output.
    """
    context = _FRESH_READ.get()
    address = config.get("configurable", {})
    if (context is None or context["used"] or address.get("checkpoint_id")
            or address.get("checkpoint_ns")):
        return saved
    context["used"] = True
    if saved is None:
        return saved
    from langchain_core.messages import AIMessage, ToolMessage

    messages = saved.checkpoint.get("channel_values", {}).get("messages")
    if not isinstance(messages, list):
        return saved
    pending = list(saved.pending_writes or [])
    if any(channel == "__interrupt__" for _, channel, _ in pending):
        return saved
    results = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)}
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
    changed = False
    for message in messages:
        repaired.append(message)
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls:
            call_id = call.get("id")
            if not call_id or call_id in results:
                continue
            result = pending_results.get(call_id)
            if result is None:
                identity = hashlib.sha256(
                    f"{saved.checkpoint['id']}:{call_id}".encode()
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
            results[call_id] = result
            changed = True
    if not changed:
        return saved
    return saved._replace(
        checkpoint={**saved.checkpoint, "channel_values": {
            **saved.checkpoint["channel_values"], "messages": repaired,
        }},
        pending_writes=updated_pending,
    )


async def fresh_memory_input(graph: Any, config: Any, state: Any) -> Any:
    """Recover the standard in-memory messages channel on a fresh user turn.

    Managed PostgreSQL applies ``fresh_checkpoint_view`` at its existing read
    boundary. A user-supplied MemorySaver has no such boundary. For that known
    backend only, use LangGraph's native Overwrite input to commit the repaired
    view and new input together. Do not replace the graph, saver, historical
    checkpoints, custom reducers, or native approval/checkpoint resumes.
    """
    context = _FRESH_READ.get()
    if (context is None or context["used"] or not isinstance(state, dict)
            or not isinstance(state.get("messages"), list)):
        return state
    address = config.get("configurable", {})
    if address.get("checkpoint_id") or address.get("checkpoint_ns"):
        return state

    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph.message import add_messages

    saver = getattr(graph, "checkpointer", None)
    channel = getattr(graph, "channels", {}).get("messages")
    if (not isinstance(saver, InMemorySaver)
            or getattr(channel, "operator", None) is not add_messages):
        return state
    saved = await saver.aget_tuple(config)
    repaired = fresh_checkpoint_view(config, saved)
    if repaired is saved:
        return state

    from langgraph.types import Overwrite

    # The missing result may precede intervening failed user inputs. Appending
    # results at the tail would still violate the provider's message ordering.
    messages = repaired.checkpoint["channel_values"]["messages"]
    return {**state, "messages": Overwrite(add_messages(messages, state["messages"]))}
