"""Admit bounded delivery context repair without reopening research."""

import json

from .context_history import _unambiguous_json_object
from .delivery_review import review_pending
from .tool_budget import finalization_only, wall_delivery_only


def delivery_summary_allowed(snapshot, config):
    if config.limits.delivery_context_recovery_policy != "delivery-window-summary/v2":
        return post_write_summary_allowed(snapshot, config)
    # A completed exploration batch can cross the window and overflow the next
    # input before any write. Context repair is still ordinary accounted model
    # work; it never restores research tools or spends the final model reserve.
    return bool(
        wall_delivery_only(snapshot, config)
        and not finalization_only(snapshot, config)
        and snapshot.execution_budget_observation.wall_seconds_remaining > 0
        and not snapshot.pending_tools and not snapshot.pending_interaction
        and not review_pending(snapshot)
    )


def post_write_summary_allowed(snapshot, config):
    if (
        config.limits.delivery_context_recovery_policy != "committed-write-summary/v1"
        or not wall_delivery_only(snapshot, config)
        or finalization_only(snapshot, config)
        or snapshot.pending_tools or snapshot.pending_interaction
        or snapshot.last_workspace_mutation_tool_call != snapshot.tool_calls
        or len(snapshot.messages) < 2
    ):
        return False
    parent, receipt = snapshot.messages[-2:]
    calls = parent.get("tool_calls", ())
    if parent.get("role") != "assistant" or len(calls) != 1 or receipt.get("role") != "tool":
        return False
    call = calls[0]
    function = call.get("function", {})
    writers = {t.name for t in config.tools if t.delivery_write and not t.read_only}
    if (
        function.get("name") not in writers
        or receipt.get("tool_call_id") != call.get("id")
        or receipt.get("receipt_goal_revision") != snapshot.goal_revision
        or receipt.get("receipt_tool_call") != snapshot.tool_calls
        or not str(receipt.get("receipt_ref", "")).startswith(f"{snapshot.run_id}:effect:")
    ):
        return False
    content = receipt.get("content")
    # Extra typed diagnostic blocks are not part of the canonical tool result.
    if isinstance(content, list):
        content = content[0].get("text") if content and isinstance(content[0], dict) else None
    result = _unambiguous_json_object(content)
    try:
        arguments = json.loads(function["arguments"])
    except (ValueError, KeyError, TypeError):
        return False
    written = result.get("content") if result and result.get("status") == "ok" else None
    return bool(
        isinstance(arguments, dict) and isinstance(written, dict)
        and written.get("ok") is True and isinstance(arguments.get("path"), str)
        and written.get("path") == arguments["path"]
    )
