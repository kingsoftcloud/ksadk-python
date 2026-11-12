"""A fresh user turn must not execute an abandoned checkpoint's tool calls."""

from __future__ import annotations

from typing import Any

# Keep the historical Runner import path bound to the exact shared functions
# and ContextVar also consumed by engine checkpoint reads.
from ksadk.runtime.langgraph_checkpoint_view import (
    _FRESH_READ as _FRESH_READ,
    consume_fresh_root_read as consume_fresh_root_read,
    fresh_checkpoint_view as fresh_checkpoint_view,
    fresh_user_checkpoint as fresh_user_checkpoint,
)


async def fresh_memory_input(graph: Any, config: Any, state: Any) -> Any:
    """Recover standard messages on known native savers on a fresh user turn.

    Managed PostgreSQL applies ``fresh_checkpoint_view`` at its existing read
    boundary. Direct native savers have no such boundary. For these known
    backends only, use LangGraph's native Overwrite input to commit the repaired
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
    if getattr(saver, "fresh_checkpoint_view_on_read", False):
        return state  # The managed saver adapts its first graph read itself.
    local_savers = (InMemorySaver,)
    try:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        local_savers += (AsyncSqliteSaver,)
    except ImportError:
        pass
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        local_savers += (AsyncPostgresSaver,)
    except ImportError:
        pass
    if (not isinstance(saver, local_savers)
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
