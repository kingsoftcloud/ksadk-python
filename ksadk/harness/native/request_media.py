"""Pure, profile-bound inline media projection before origin metadata is lost.

This counts encoded data URLs, not tokens, remote resource sizes or the full
wire body. Canonical receipts/source coverage remain untouched. Only the
current dispatch view may omit already observed historical media.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field

from .context_history import _content_blocks, text_only_tool_message
from .model import ModelProfile
from .state import Contract
from .tool_observation import model_observation_messages


class RequestMediaError(ValueError):
    """A known preflight refusal; no model request has been dispatched."""


class ModelRequestDispatchDenial(Contract):
    schema_version: Literal["ksadk.native-model-request-denial/v1"] = (
        "ksadk.native-model-request-denial/v1"
    )
    operation_key: str = Field(min_length=1)
    reason: Literal["request_current_media_over_budget"] = "request_current_media_over_budget"


def _omitted(block):
    digest = hashlib.sha256(json.dumps(
        block, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return {"type": "text", "text": json.dumps({
        "type": "ksadk.request-media-omission/v1",
        "media_digest": digest,
        "status": "not_sent_in_this_request",
        "reason": "inline_image_budget",
        "observation": (
            "Historical image omitted from this model request. Use the textual receipt; "
            "do not claim to have seen the image or repeat already executed side effects "
            "solely because its pixels were omitted."
        ),
    }, ensure_ascii=False, sort_keys=True)}


def project_request_media(messages, profile: ModelProfile | None, *, purpose="task"):
    if profile is None or profile.request_media_policy is None:
        # Legacy profiles retain their serialization and request semantics.
        return tuple(json.loads(json.dumps(message, ensure_ascii=False)) for message in messages)
    copied = model_observation_messages(messages)
    from .replay_context import content_digest

    original_digests = tuple(content_digest(m.get("content")) for m in copied)
    actual_user = -1
    if purpose == "task":
        actual_user = next((i for i in range(len(copied) - 1, -1, -1)
                            if copied[i].get("role") == "user"
                            and copied[i].get("kind") is None), -1)
    effective = []
    media = []
    for i, message in enumerate(copied):
        # Validate before omission; a zero budget cannot legitimize bad media.
        blocks = _content_blocks(message)
        has_image = any(block["type"] == "image_url" for block in blocks)
        if has_image and message.get("role") not in {"user", "tool"}:
            raise ValueError("image observations require user or tool content")
        if has_image and profile.image_input_mode == "text_only":
            if message.get("role") != "tool":
                raise ValueError("model profile does not accept user image input")
            message = text_only_tool_message(message)
            blocks = ()
        effective.append(message)
        for j, block in enumerate(blocks):
            if block["type"] == "image_url" and block["image_url"]["url"].startswith("data:"):
                media.append((i, j, len(block["image_url"]["url"].encode("utf-8"))))
    limit = profile.request_media_policy.max_inline_image_bytes
    if sum(size for _, _, size in media) <= limit:
        return _edited_media(original_digests, effective, profile)
    protected = {(i, j) for i, j, _ in media if i == actual_user}
    remaining = limit - sum(size for i, _, size in media if i == actual_user)
    if remaining < 0:
        raise RequestMediaError("request_current_media_over_budget")
    retained = set(protected)
    for i, j, size in reversed(media):
        if (i, j) in protected or size > remaining:
            continue
        retained.add((i, j))
        remaining -= size
    for i, j, _ in media:
        if (i, j) not in retained:
            effective[i]["content"][j] = _omitted(effective[i]["content"][j])
    for message in effective:
        content = message.get("content")
        if (message.get("role") == "tool" and isinstance(content, list)
                and not any(block["type"] == "image_url" for block in content)):
            message["content"] = "\n".join(block["text"] for block in content)
    return _edited_media(original_digests, effective, profile)


def _edited_media(originals, effective, profile):
    from .replay_context import content_digest, mark_input_edit

    return tuple(
        mark_input_edit(message, None, profile, source_digest=original)
        if content_digest(message.get("content")) != original else message
        for original, message in zip(originals, effective)
    )
