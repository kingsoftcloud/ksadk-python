"""Structural first-observation protection; never a model receipt or ACK."""

from __future__ import annotations

import hashlib

from .context_history import _json

FRESH_TOOL_OBSERVATION_POLICY = "complete-fresh-tool-group/v1"


def fresh_tool_group_refs(messages) -> frozenset[str]:
    """Return the last raw assistant's complete call/receipt group, if any.

    Inspect the canonical transcript, not a previously retained projection:
    removing a later assistant must not make an old group fresh again. User
    and host messages after the group do not attest model observation. This
    conservative structural rule proves neither dispatch nor comprehension.
    """
    parent_index = next(
        (index for index in range(len(messages) - 1, -1, -1)
         if messages[index].get("role") == "assistant"),
        None,
    )
    if parent_index is None:
        return frozenset()
    calls = messages[parent_index].get("tool_calls")
    if not isinstance(calls, (list, tuple)) or not calls:
        return frozenset()
    call_ids = [call.get("id") if isinstance(call, dict) else None for call in calls]
    if any(not isinstance(value, str) or not value for value in call_ids):
        return frozenset()
    if len(set(call_ids)) != len(call_ids):
        return frozenset()
    receipt_indexes = [
        index for index in range(parent_index + 1, len(messages))
        if messages[index].get("role") == "tool"
    ]
    receipt_ids = [messages[index].get("tool_call_id") for index in receipt_indexes]
    if (
        any(not isinstance(value, str) for value in receipt_ids)
        or len(receipt_ids) != len(call_ids)
        or set(receipt_ids) != set(call_ids)
    ):
        return frozenset()
    return frozenset(
        f"message:{index}:" + hashlib.sha256(_json((messages[index],)).encode()).hexdigest()
        for index in (parent_index, *receipt_indexes)
    )
