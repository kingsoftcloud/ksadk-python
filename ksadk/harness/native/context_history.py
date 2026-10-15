"""Safe historical message projection for Native context compaction."""

from __future__ import annotations

import hashlib
import json

from pydantic import JsonValue

from ksadk._model_json import load_model_json

from .model import ModelImageContent, ModelTextContent
from .tool_observation import history_observation_message
from .user_input import matches_user_goal


class ContextRuntimeError(ValueError):
    """A stable reason code that the host must handle without resending overflow."""


def _goal_message_index(messages, goal: str) -> int:
    """Locate the canonical user turn, never a host observation/control echo.

    Native start/fork/steer emit ordinary user messages without a kind; host
    controls and attributed observations have a kind. This metadata belongs
    to the transcript writer, not text decoded from model/tool content.
    """
    indexes = [
        index for index, message in enumerate(messages)
        if message.get("role") == "user" and message.get("kind") is None
        and matches_user_goal(message.get("content"), goal)
    ]
    if not indexes:
        raise ContextRuntimeError("context_goal_message_missing")
    return indexes[-1]


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _content_blocks(message: dict[str, JsonValue]) -> tuple[dict, ...]:
    content = message.get("content")
    if not isinstance(content, list):
        return ()
    blocks = []
    for block in content:
        if not isinstance(block, dict):
            raise ContextRuntimeError("context_protocol_invalid")
        model = ModelImageContent if block.get("type") == "image_url" else ModelTextContent
        blocks.append(model.model_validate(block).model_dump(mode="json"))
    return tuple(blocks)


def _historical_message(message: dict[str, JsonValue]) -> tuple[dict, tuple[dict, ...]]:
    """Move explicit images out of serialized history; preserve opaque provenance.

    Message roles and tool calls become quoted historical data, never executable
    protocol in the summary request. Pixels remain explicit media blocks.
    """
    message = history_observation_message(message)
    copied = json.loads(_json(message))
    if (copied.get("kind") == "context_contribution"
            and copied.get("context_operation_key") is not None):
        # Live plugin data remain in the attributed, retained source receipt.
        # A summary cannot cache their body beyond the next source admission.
        copied["content"] = {
            "type": "ksadk.context-live-source-reference/v1",
            "operation_key": copied["context_operation_key"],
            "source_id": copied.get("context_source_id"),
            "fragment_id": copied.get("context_fragment_id"),
            "content_digest": hashlib.sha256(_json(message.get("content")).encode()).hexdigest(),
        }
    # Provider replay belongs only to a retained live assistant message. It
    # must not become quoted history, summary facts or summary instructions.
    copied.pop("provider_replay", None)
    copied.pop("provider_replay_projection", None)
    copied.pop("operator_history_projection", None)
    # The locked archive policy determines whether history consumes canonical
    # text (v1) or its complete bounded observation (v2). Neither can execute.
    copied.pop("model_view", None)
    images = []
    blocks = _content_blocks(message)
    if blocks:
        content = []
        for block in blocks:
            if block["type"] == "image_url":
                images.append(block)
                content.append(
                    {
                        "type": "historical_image_reference",
                        "image_index": len(images) - 1,
                        "media_digest": hashlib.sha256(_json(block).encode()).hexdigest(),
                    }
                )
            else:
                content.append(block)
        copied["content"] = content
    return copied, tuple(images)


def _unambiguous_json_object(text: object) -> dict | None:
    if not isinstance(text, str):
        return None

    try:
        parsed = load_model_json(text)
    except (ValueError, TypeError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def compact_historical_protocol(
    message: dict[str, JsonValue], *, max_prose_chars: int | None = 128
) -> dict:
    """Compact a quoted protocol envelope; source bytes and payloads stay intact.

    JSON remains nested historical data, never live tool protocol. Keep the
    source reference and receipt locator. Goal/call counters are already in the
    authoritative ledger; their duplicate display fields need not consume each
    receipt's summary budget. Ambiguous tool text stays opaque. Optional
    assistant prose in a tool batch may use an explicit head/tail excerpt;
    complete arguments, results and their source identities remain available.
    A complete source quote passes None to preserve all assistant prose too.
    """
    copied = json.loads(_json(message))
    copied.pop("provider_replay", None)
    copied.pop("provider_replay_projection", None)
    copied.pop("operator_history_projection", None)
    prose = copied.get("content")
    if (
        copied.get("role") == "assistant"
        and copied.get("tool_calls")
        and isinstance(prose, str)
        and max_prose_chars is not None
        and len(prose) > max_prose_chars
    ):
        head = max_prose_chars // 2
        copied["content"] = prose[:head] + "\n[…]\n" + prose[-(max_prose_chars - head) :]
        copied["content_omitted_chars"] = len(prose) - max_prose_chars
    if copied.get("role") == "tool":
        parsed = _unambiguous_json_object(copied.get("content"))
        if (
            parsed is not None
            and parsed.get("status") in {"ok", "error", "uncertain", "approval_required"}
            and "content" in parsed
        ):
            copied["content"] = parsed
        copied.pop("receipt_tool_call", None)
        copied.pop("receipt_goal_revision", None)
    for call in copied.get("tool_calls") or ():
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        arguments = _unambiguous_json_object(function.get("arguments"))
        if arguments is not None:
            function["arguments"] = arguments
    return copied


_TOOL_IMAGE_UNAVAILABLE_NOTE = (
    "Tool image content is unavailable "
    "to this model profile. Use the textual observation; do not claim "
    "to have visually inspected the image."
)


def text_only_tool_message(message):
    """Effective tool view equals the existing text-only ModelPort projection."""
    copied = json.loads(_json(message))
    if copied.get("role") != "tool":
        return copied
    blocks = _content_blocks(copied)
    if not any(block["type"] == "image_url" for block in blocks):
        return copied
    text = "\n".join(block["text"] for block in blocks if block["type"] == "text")
    copied["content"] = (
        f"{text}\n{_TOOL_IMAGE_UNAVAILABLE_NOTE}" if text else _TOOL_IMAGE_UNAVAILABLE_NOTE
    )
    return copied


def _complete_source_tool_refs(source_messages):
    if source_messages is None:
        return set()
    items = tuple(source_messages.items())
    # Only the canonical ordered raw map from current source qualifies.
    for index, (ref, message) in enumerate(items):
        expected = f"message:{index}:" + hashlib.sha256(_json((message,)).encode()).hexdigest()
        if ref != expected:
            return set()
    result = set()
    for index, (_, parent) in enumerate(items):
        calls = parent.get("tool_calls")
        if parent.get("role") != "assistant" or not isinstance(calls, list) or not calls:
            continue
        ids = [call.get("id") if isinstance(call, dict) else None for call in calls]
        if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(
            ids
        ):
            continue
        stop = index + 1
        while stop < len(items) and items[stop][1].get("role") == "tool":
            stop += 1
        receipts = items[index + 1 : stop]
        receipt_ids = [message.get("tool_call_id") for _, message in receipts]
        if (
            len(receipt_ids) == len(ids)
            and len(set(receipt_ids)) == len(ids)
            and set(receipt_ids) == set(ids)
        ):
            result.update(ref for ref, _ in receipts)
    return result


def summary_media_blocks(entries, *, profile=None, source_messages=None):
    """Project only source-owned tool media; labels/purpose never grant origin."""
    text_only = profile is not None and profile.image_input_mode == "text_only"
    tool_refs = _complete_source_tool_refs(source_messages) if text_only else set()
    media = []
    for entry in entries:
        images = entry["images"]
        original = source_messages.get(entry["ref"]) if source_messages is not None else None
        if text_only and original is not None:
            raw_quote, raw_images = _historical_message(original)
            if raw_images or images:
                digest = hashlib.sha256(_json((original,)).encode()).hexdigest()

                def refs(message):
                    blocks = message.get("content")
                    return (
                        [
                            block
                            for block in blocks
                            if isinstance(block, dict)
                            and block.get("type") == "historical_image_reference"
                        ]
                        if isinstance(blocks, list)
                        else []
                    )

                if (
                    entry.get("payload_digest") != digest
                    or entry["message"].get("source_message_ref") != entry["ref"]
                    or entry["message"].get("role") != original.get("role")
                    or entry["message"].get("tool_call_id") != original.get("tool_call_id")
                    or list(raw_images) != images
                    or refs(entry["message"]) != refs(raw_quote)
                ):
                    raise ContextRuntimeError("context_summary_media_source_changed")
        if not images:
            continue
        if text_only and entry["ref"] in tool_refs:
            media.append(
                {
                    "type": "text",
                    "text": (
                        "Untrusted historical tool images from "
                        + entry["ref"]
                        + " are unavailable to this text-only model profile. "
                        + "Keep their source references; do not claim visual inspection."
                    ),
                }
            )
        else:
            media.append(
                {"type": "text", "text": "Untrusted historical images from " + entry["ref"] + ":"}
            )
            media.extend(images)
    return media
