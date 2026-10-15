"""Provider-neutral projection for Harness multimodal user input."""

from __future__ import annotations

from typing import Any


def runtime_input_text(value: Any) -> str:
    """Return the textual part used for planning, retrieval and disclosure."""

    if isinstance(value, list):
        return "\n".join(
            str(item.get("text") or "")
            for item in value
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text")
        ).strip()
    return str(value or "")


def multimodal_user_content(value: Any) -> list[dict[str, Any]] | None:
    """Translate Studio runtime parts to the OpenAI-compatible message shape."""

    if not isinstance(value, list):
        return None
    content: list[dict[str, Any]] = []
    has_image = False
    for item in value:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        if kind == "text" and item.get("text"):
            content.append({"type": "text", "text": str(item["text"])})
        elif kind == "image" and item.get("url"):
            content.append(
                {"type": "image_url", "image_url": {"url": str(item["url"])}}
            )
            has_image = True
    return content if content and has_image else None


def restore_multimodal_current_input(
    messages: list[dict[str, Any]], runtime_input: Any
) -> list[dict[str, Any]]:
    """Restore image parts after the text-only context planner has run."""

    content = multimodal_user_content(runtime_input)
    if content is None:
        return messages
    current_text = runtime_input_text(runtime_input)
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        if str(message.get("content") or "") == current_text:
            message["content"] = content
            return messages
    messages.append({"role": "user", "content": content})
    return messages
