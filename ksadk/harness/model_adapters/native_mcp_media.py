"""Project MCP data blocks without granting URLs or payloads host authority."""

from __future__ import annotations

import base64
import binascii
from typing import Any

from ksadk.harness.native.model import ModelImageContent, ModelTextContent
from ksadk.harness.native.ports import ToolModelObservation

_INLINE_CEILING = 64 * 1024 * 1024


def _image(data: Any, mime: Any) -> ModelImageContent | None:
    if not isinstance(data, str) or len(data) > _INLINE_CEILING:
        return None
    try:
        decoded = base64.b64decode(data, validate=True)
        matches = {
            "image/png": decoded.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/jpeg": decoded.startswith(b"\xff\xd8\xff"),
            "image/gif": decoded.startswith((b"GIF87a", b"GIF89a")),
            "image/webp": decoded.startswith(b"RIFF") and decoded[8:12] == b"WEBP",
        }
        if not isinstance(mime, str) or not matches.get(mime, False):
            return None
        return ModelImageContent(image_url={"url": f"data:{mime};base64,{data}"})
    except (ValueError, binascii.Error):
        return None


def model_mcp_observation(result: Any, *, failed: bool) -> ToolModelObservation | None:
    """Preserve structured data and explicit media; never fetch an embedded URI.

    Plain-text fast paths stay in native_mcp_result. This projection handles
    the remaining protocol content, including known remote error results.
    MIME signatures are checked, but image decoding/visual acceptance is the
    model's job. Unsupported or invalid media is explicitly unobserved.
    """
    if not isinstance(result, dict) or not isinstance(result.get("content"), list):
        return None
    blocks = result["content"]
    media_values: set[str] = set()
    projected: list[ModelTextContent | ModelImageContent] = []
    omissions = []
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            omissions.append({"block": index, "code": "mcp_content_invalid"})
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            projected.append(ModelTextContent(text=block["text"]))
            continue
        resource = block.get("resource") if kind == "resource" else None
        data = (
            resource.get("blob") if isinstance(resource, dict) else block.get("data")
        )
        if isinstance(data, str) and data:
            media_values.add(data)
        if isinstance(resource, dict) and isinstance(resource.get("text"), str):
            projected.append(ModelTextContent(text=resource["text"]))
            continue
        mime = resource.get("mimeType") if isinstance(resource, dict) else block.get("mimeType")
        image = _image(data, mime) if kind == "image" or isinstance(resource, dict) else None
        if image is not None:
            projected.extend((
                ModelTextContent(text=f"MCP image block {index} (when available to this profile)."),
                image,
            ))
        else:
            omissions.append({"block": index, "code": "mcp_media_unobserved", "type": kind})

    def without_media(value):
        if isinstance(value, str) and value in media_values:
            return "[MCP binary data omitted from text]"
        if isinstance(value, list):
            return [without_media(item) for item in value]
        if isinstance(value, dict):
            return {key: without_media(item) for key, item in value.items()}
        return value

    content = {
        "kind": "mcp_tool_result",
        "metadata": without_media({
            key: value for key, value in result.items()
            if key not in {"content", "structuredContent", "_meta"}
        }),
    }
    if "structuredContent" in result:
        content["structuredContent"] = without_media(result["structuredContent"])
    if omissions:
        content["unobserved_blocks"] = omissions
        content["instruction"] = (
            "Some MCP media could not be observed. Do not claim to have inspected it or "
            "repeat the operation solely for missing media; use a supported read-only observation."
        )
    return ToolModelObservation(
        status="error" if failed else "ok", content=content, content_blocks=tuple(projected),
    )
