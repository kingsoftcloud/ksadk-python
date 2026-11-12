"""User-visible answers only; adapted from the previously verified output fix."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def extract_output(result: Any) -> str:
    """Expose an explicit answer or the final assistant message, never graph state."""

    def text_content(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(
                block.get("text", "")
                for block in value
                if isinstance(block, Mapping)
                and block.get("type") in {"text", "output_text"}
                and isinstance(block.get("text"), str)
            )
        return ""

    if isinstance(result, Mapping):
        for field in ("output", "answer"):
            if field in result and result[field] is not None:
                value = result[field]
                # Explicit structured business answers retain their old contract.
                if isinstance(value, str):
                    return value
                if (
                    isinstance(value, list)
                    and value
                    and all(
                        isinstance(block, Mapping)
                        and block.get("type")
                        in {
                            "text",
                            "output_text",
                            "reasoning",
                            "thinking",
                            "image",
                            "input_image",
                            "audio",
                            "file",
                        }
                        for block in value
                    )
                ):
                    return text_content(value)
                return str(value)
        messages = result.get("messages")
        if isinstance(messages, (list, tuple)) and messages:
            last = messages[-1]
            role = (
                (last.get("role") or last.get("type"))
                if isinstance(last, Mapping)
                else getattr(last, "type", None)
            )
            if role in {"assistant", "ai", "model"}:
                content = (
                    last.get("content")
                    if isinstance(last, Mapping)
                    else getattr(last, "content", None)
                )
                return text_content(content)
        return ""
    return str(result) if isinstance(result, (str, int, float, bool)) else ""
