"""Project old completed tool observations; retain canonical execution facts.

Batch recency and media/error protection follow the pinned ZCode microcompact
path; Skill retention follows OpenCode's protected tools. The host supplies
validated complete groups and source-working-set protection, not tool text.
"""

from __future__ import annotations

import hashlib

from .context_history import _content_blocks, _json, _unambiguous_json_object
from .context_pruning import clear_tool_result
from .tool_observation import model_observation_message


def project_old_tool_results(messages, refs, groups, *, tools, protected_refs, keep_recent):
    """Keep protocol pairs and original source locators in the request view."""
    indexes = {ref: index for index, ref in enumerate(refs)}
    definitions = {tool.name: tool for tool in tools}
    calls = {
        call["id"]: call["function"]["name"]
        for message in messages
        for call in message.get("tool_calls", ())
    }
    protected = set(protected_refs).union(*groups[-keep_recent:])
    projected, cleared_refs, cleared_calls = list(messages), [], []
    for group in groups:
        if not group.isdisjoint(protected):
            continue
        for ref in sorted(group, key=indexes.__getitem__):
            message = messages[indexes[ref]]
            if message.get("role") != "tool":
                continue
            definition = definitions.get(calls.get(message.get("tool_call_id")))
            if (
                definition is None
                or definition.context_retention == "persistent"
                or definition.context_role == "task_planning"
                or definition.observation_role == "browser"
                or definition.delegated_read_only
            ):
                continue
            content = message.get("content")
            source = content[0].get("text") if isinstance(content, list) and content else content
            receipt = _unambiguous_json_object(source)
            if not receipt or receipt.get("status") != "ok":
                continue
            if isinstance(receipt.get("content"), dict) and receipt["content"].get("ok") is False:
                continue
            visible = model_observation_message(message)
            if any(block["type"] != "text" for block in _content_blocks(visible)):
                continue
            projected[indexes[ref]] = clear_tool_result(
                visible,
                source_message_ref=ref,
                payload_digest=hashlib.sha256(_json(content).encode()).hexdigest(),
            )
            cleared_refs.append(ref)
            cleared_calls.append(message["tool_call_id"])
    return tuple(projected), tuple(cleared_refs), tuple(cleared_calls)
