"""Pure recognition of a host-declared artifact reader's returned text page.

Callers must first check the locked ToolDefinition. Source JSON, tool names
and similarly shaped prose never grant this role or storage access.
"""

from __future__ import annotations

import hashlib
import json

from .context_history import _unambiguous_json_object
from .ports import ArtifactReadResult, ArtifactRef


def artifact_page_observation(message, *, arguments):
    if message.get("role") != "tool" or not isinstance(arguments, dict):
        return None
    receipt = _unambiguous_json_object(message.get("content"))
    payload = receipt.get("content") if receipt and receipt.get("status") == "ok" else None
    if not isinstance(payload, dict):
        return None
    uri = arguments.get("uri")
    offset, limit = arguments.get("offset_chars", 0), arguments.get("limit_chars", 16000)
    text = payload.get("content")
    if (
        not isinstance(uri, str) or payload.get("uri") != uri
        or type(offset) is not int or offset < 0
        or type(limit) is not int or not 1 <= limit <= 32000
        or payload.get("offset_chars") != offset
        or type(payload.get("complete")) is not bool
        or not isinstance(text, str) or len(text) > limit
    ):
        return None
    try:
        ref = ArtifactRef.model_validate({
            key: payload[key] for key in ("uri", "content_hash", "size_bytes", "mime_type")
        })
        ArtifactReadResult.model_validate({
            "ref": ref, "content": text, "offset_chars": payload["offset_chars"],
            "next_offset_chars": payload["next_offset_chars"], "complete": payload["complete"],
        })
        if offset == 0 and payload["complete"] and (
            ref.size_bytes != len(text.encode("utf-8"))
            or ref.content_hash != "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        ):
            return None
    except (ValueError, TypeError, KeyError):
        return None
    # Retain exact text and metadata, including incomplete-page state. This
    # is a quoted observation, not a grant to open the URI or a fresh file read.
    return json.loads(json.dumps(message, ensure_ascii=False))


def complete_artifact_call_ids(messages, *, tool_names):
    calls = {}
    observed = set()
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls", ()):
                function = call.get("function", {})
                if function.get("name") in tool_names:
                    arguments = _unambiguous_json_object(function.get("arguments"))
                    if arguments is not None:
                        calls[call["id"]] = arguments
        elif message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if call_id in calls and artifact_page_observation(
                message, arguments=calls[call_id]
            ) is not None:
                observed.add(call_id)
    return frozenset(observed)


__all__ = ["artifact_page_observation", "complete_artifact_call_ids"]
