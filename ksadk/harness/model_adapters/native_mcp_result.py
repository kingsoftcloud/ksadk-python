"""Model-facing projection of an MCP result, separate from transport status.

MCP transports return typed content blocks. A plain text result should reach
the model as that text, not as a nested SDK envelope. Preserve structured or
non-text blocks until a host has an explicit multimodal projection for them.
"""

from __future__ import annotations

from typing import Any


def model_mcp_content(result: Any) -> Any:
    """Flatten only unambiguous text-only MCP content blocks."""

    if not isinstance(result, dict):
        return result
    blocks = result.get("content")
    if not isinstance(blocks, list) or not blocks:
        return result
    if any(
        not isinstance(block, dict)
        or block.get("type") != "text"
        or not isinstance(block.get("text"), str)
        for block in blocks
    ):
        return result
    structured = result.get("structuredContent")
    # FastMCP duplicates a scalar string in both its text block and
    # ``structuredContent.result``. Dropping only this exact mirror avoids
    # showing the model two nested copies of the same answer. Every other
    # structured value remains intact for the host's richer projection.
    duplicate_scalar = len(blocks) == 1 and structured == {"result": blocks[0]["text"]}
    if structured not in (None, {}, []) and not duplicate_scalar:
        return result
    return "\n\n".join(block["text"] for block in blocks)
