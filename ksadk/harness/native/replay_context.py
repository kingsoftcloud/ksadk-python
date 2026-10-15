"""Bind private replay to a host-selected message view in the original ledger.

This is an identity for Native's input, not a cryptographic signature check
or a claim about unobserved provider transformations. Original opaque blocks
and tool receipts stay immutable. Abandonment is monotonic across replans.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re

from ksadk._model_anthropic import anthropic_content
from ksadk._model_json import load_model_json

from .context_replay import POLICY, _mark
from .model import ModelReplayInputContext, ModelReplayMetadata
from .model_replay import project_model_message

CONTEXT_VERSION = "native-anthropic-input-prefix/v2"
EDIT_VERSION = "native-model-input-edit/v1"


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def replay_identity(replay):
    return _digest({"profile": replay.profile_digest, "content": replay.anthropic_content_json,
                    "input_context": replay.input_context.model_dump(mode="json")
                    if replay.input_context is not None else None})


def mark_input_edit(message, original_content, profile, *, source_digest=None):
    copied = copy.deepcopy(message)
    if profile is not None and profile.provider_replay_projection_policy == POLICY:
        copied["model_input_edit"] = {
            "version": EDIT_VERSION, "profile_digest": profile.digest,
            "source_content_digest": source_digest or _digest(original_content),
            "view_content_digest": _digest(copied.get("content")),
        }
    return copied


def content_digest(content):
    return _digest(content)


def _edited(message, profile):
    raw = message.get("model_input_edit")
    if raw is None:
        return False
    if (not isinstance(raw, dict)
            or set(raw) != {"version", "profile_digest", "source_content_digest",
                            "view_content_digest"}
            or raw["version"] != EDIT_VERSION or raw["profile_digest"] != profile.digest
            or raw["view_content_digest"] != _digest(message.get("content"))
            or not isinstance(raw["source_content_digest"], str)
            or re.fullmatch(r"[0-9a-f]{64}", raw["source_content_digest"]) is None):
        raise ValueError("model_input_edit_mismatch")
    return True


def _prefix_part(source, profile, identities):
    """Normalize host aliases back to original provider IDs, excluding thinking."""
    message = project_model_message(source, profile)
    original = source.get("provider_replay")
    if source.get("role") == "assistant" and original is not None:
        replay = ModelReplayMetadata.model_validate(original)
        if replay.profile_digest == profile.digest and replay.anthropic_content_json is not None:
            blocks = anthropic_content(load_model_json(replay.anthropic_content_json))
            mapped = dict(replay.anthropic_tool_id_map)
            for block in blocks:
                if block["type"] == "tool_use":
                    identities[mapped.get(block["id"], block["id"])] = block["id"]
            return {"role": "assistant", "content": [
                b for b in blocks if b["type"] not in {"thinking", "redacted_thinking"}
            ]}
    message.pop("anthropic_content", None)
    message.pop("anthropic_tool_id_map", None)
    message.pop("reasoning_content", None)
    if message.get("role") == "tool":
        call = message.get("tool_call_id")
        message["tool_call_id"] = identities.get(call, call)
    return message


def message_prefix_digest(messages, profile):
    identities = {}
    return _digest([_prefix_part(m, profile, identities) for m in messages])


def request_header_digest(instructions, tools, profile):
    if profile is None or profile.provider_replay_projection_policy != POLICY:
        return None
    # Only model-visible declarations. Host permissions, admission choices,
    # purpose, request IDs and output budgets are not signed prefix fields.
    return _digest({"instructions": instructions,
                    "tools": [tool.function_schema() for tool in tools]})


def bind_replay_input(result, request):
    replay = result.provider_replay
    if (request.profile.provider_replay_projection_policy != POLICY or replay is None
            or replay.anthropic_content_json is None):
        return result
    bound = replay.model_copy(update={"input_context": ModelReplayInputContext(
        version=CONTEXT_VERSION,
        messages_digest=message_prefix_digest(request.messages, request.profile),
        header_digest=request_header_digest(request.instructions, request.tools, request.profile),
    )})
    return result.model_copy(update={"provider_replay": bound})


def project_replay_context(messages, profile, *, abandoned=(), header_digest=None):
    if profile is None or profile.provider_replay_projection_policy != POLICY:
        return copy.deepcopy(messages), ()
    discarded = set(abandoned)
    prefix, identities, output = [], {}, []
    edited = False
    for index, source in enumerate(messages):
        edited = _edited(source, profile) or edited
        message = copy.deepcopy(source)
        original = source.get("provider_replay")
        if source.get("role") == "assistant" and original is not None:
            replay = ModelReplayMetadata.model_validate(original)
            if (replay.profile_digest == profile.digest
                    and replay.anthropic_content_json is not None):
                blocks = anthropic_content(load_model_json(replay.anthropic_content_json))
                if any(b["type"] in {"thinking", "redacted_thinking"} for b in blocks):
                    key = replay_identity(replay)
                    current = _digest(prefix)
                    mismatch = (replay.input_context.messages_digest != current
                                if replay.input_context is not None else edited)
                    if header_digest is not None and replay.input_context is not None:
                        # v1 records cannot establish an unchanged header. Do
                        # not infer one from the current request or rewrite the
                        # old record as v2; active Runs pin this algorithm.
                        mismatch = mismatch or replay.input_context.header_digest != header_digest
                    if (key in discarded or mismatch
                            or source.get("provider_replay_projection") is not None):
                        discarded.add(key)
                        message = _mark(source, f"request-message:{index}", current)
        output.append(message)
        prefix.append(_prefix_part(message, profile, identities))
    return tuple(output), tuple(sorted(discarded))
