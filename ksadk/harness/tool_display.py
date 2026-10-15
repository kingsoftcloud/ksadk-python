"""Host tool output disclosure and pure display contract compatibility exports."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from .tool_display_contract import (
    ToolDisplay as ToolDisplay,
)
from .tool_display_contract import (
    display_text,
    safe_tool_display,
    tool_display_start,
)


def tool_display_output(
    start: dict[str, Any], *, output: Any = None, diff: Any = None, exit_code: Any = None
) -> dict[str, Any]:
    """For trusted host adapters only, after their disclosure/policy decision."""
    value = dict(start)
    total = max(
        len(output) if isinstance(output, str) else 0, len(diff) if isinstance(diff, str) else 0
    )
    for key, raw in (("output", output), ("diff", diff)):
        if isinstance(raw, str):
            value[key] = display_text(raw)
    value.update(truncated=total > 16384, total_chars=total)
    if type(exit_code) is int:
        value["exit_code"] = exit_code
    return safe_tool_display(value) or {}


def builtin_tool_display(name: str, args: Any, result: Any) -> dict[str, Any]:
    """Explicit SDK host disclosure allowlist. Never call for custom/MCP tools."""
    start = tool_display_start(name, args)
    if not isinstance(result, dict):
        return start
    category = start["category"]
    output = diff = None
    if category == "terminal":
        output = "\n".join(
            result[key] for key in ("stdout", "stderr") if isinstance(result.get(key), str)
        )
    elif category == "read" and start.get("path"):
        output = result.get("content")
    elif category == "edit" and start.get("path") and result.get("ok") is True:
        diff = result.get("diff")
    elif category == "search" and isinstance(result.get("results"), list):
        # Only file location/line text, never arbitrary search result objects.
        lines = []
        for item in result["results"][:100]:
            if isinstance(item, dict):
                path = PurePosixPath(str(item.get("path") or "")).name
                if not re.search(r"(?i)(?:\.env|credential|secret|token|password)", path):
                    lines.append(f"{path}:{item.get('line', '')} {item.get('text', '')}")
        output = "\n".join(lines)
    display = tool_display_output(
        start, output=output, diff=diff, exit_code=result.get("exit_code")
    )
    if result.get("truncated") is True or result.get("diff_truncated") is True:
        display["truncated"] = True
    retained = diff if isinstance(diff, str) else output
    if isinstance(retained, str) and len(retained) > 16384:
        from .public_output import retain_public_output

        identity = retain_public_output(
            retained, incomplete=(result.get("truncated") is True
                                  or result.get("diff_truncated") is True),
        )
        if identity:
            display["artifact_id"] = identity
    return display
