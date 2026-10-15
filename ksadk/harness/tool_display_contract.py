"""Versioned, bounded public tool presentation, independent of raw receipts.

Arguments and results remain redacted in canonical tool items. Hosts explicitly
opt into publishing output; a tool-returned JSON key cannot grant disclosure.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .public_activity import public_commentary_text, tool_public_action


class ToolDisplay(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["tool-display/v1"] = "tool-display/v1"
    category: Literal["terminal", "read", "search", "edit", "todo", "tool"] = "tool"
    summary: str = Field(default="", max_length=280)
    path: str | None = Field(default=None, max_length=180)
    output: str | None = Field(default=None, max_length=16384)
    diff: str | None = Field(default=None, max_length=16384)
    truncated: bool = False
    total_chars: int | None = Field(default=None, ge=0)
    exit_code: int | None = None
    # Opaque host-owned identity, resolved only through a run-scoped API.
    artifact_id: str | None = Field(default=None, max_length=180)
    todos: list[dict[str, str]] | None = Field(default=None, max_length=64)


_CATEGORIES = {
    **dict.fromkeys(
        (
            "exec_command",
            "run_command",
            "execute_command",
            "run_workspace_command",
            "sandbox_run_command",
            "codex.command",
        ),
        "terminal",
    ),
    **dict.fromkeys(
        ("read_file", "read_workspace_file", "list_workspace_files", "sandbox_read_file"), "read"
    ),
    **dict.fromkeys(
        ("web_search", "search_workspace_files", "search_knowledge", "knowledge_search"), "search"
    ),
    **dict.fromkeys(
        ("write_workspace_file", "edit_workspace_file", "multi_edit_workspace_file", "apply_patch"),
        "edit",
    ),
    "TodoWrite": "todo",
}
_SECRET = re.compile(
    r"(?i)(?:\b(?:api[_ -]?key|secret|password|passwd|authorization|access[_ -]?token|"
    r"refresh[_ -]?token|token|private[_ -]?key)\b\s*[:=]\s*[^\n]+|"
    r"\bbearer\s+\S+|\bsk-[a-z0-9_-]{12,})"
)
_ANSI = re.compile(r"\x1b(?:\][^\x07]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~])")


def display_text(value: Any, limit: int = 16384) -> str:
    if not isinstance(value, str):
        return ""
    value = _ANSI.sub("", value)
    value = _SECRET.sub("[redacted]", value)
    value = re.sub(r"https?://[^\s/]*@[^\s]+", "[redacted URL]", value)
    value = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|\Z)",
        "[redacted key]",
        value,
    )
    # Keep tabs/newlines, never terminal control sequences or bidi overrides.
    value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f\u202a-\u202e\u2066-\u2069]", "", value)
    return value[:limit]


def safe_tool_display(value: Any) -> dict[str, Any] | None:
    """Revalidate at every public boundary; unknown versions fail closed."""
    try:
        display = ToolDisplay.model_validate(value)
    except (ValueError, TypeError):
        return None
    result = display.model_dump(exclude_none=True)
    for key in ("summary", "path", "output", "diff", "artifact_id"):
        if key in result:
            result[key] = display_text(result[key])
    if display.todos is not None:
        result["todos"] = []
        seen = set()
        for item in display.todos:
            content = public_commentary_text(item.get("content"), limit=500)
            state = item.get("status")
            if not content or state not in {"pending", "in_progress", "completed"}:
                continue
            # Legacy model plans lack IDs. The public identity survives reorder
            # and status updates; edited text intentionally becomes a new item.
            identity = (
                item.get("id") or hashlib.sha256(content.casefold().encode()).hexdigest()[:20]
            )
            if identity in seen:
                continue
            seen.add(identity)
            result["todos"].append(
                {
                    "id": display_text(identity, 180),
                    "content": content,
                    "status": state,
                    "priority": item.get("priority", "medium"),
                }
            )
    return result


def tool_display_start(name: str, args: Any) -> dict[str, Any]:
    args = args if isinstance(args, dict) else {}
    action = tool_public_action({"name": name, "args": args}) or {}
    display = ToolDisplay(category=_CATEGORIES.get(name, "tool"), summary=action.get("text", ""))
    if display.category in {"read", "edit"}:
        raw_path = str(args.get("path") or "").replace("\\", "/")
        target = PurePosixPath(raw_path).name
        if target and not re.search(
            r"(?i)(?:\.env|credential|secret|token|password|id_rsa|\.ssh|\.aws|kubeconfig|\.pypirc)",
            raw_path,
        ):
            display.path = display_text(target, 180)
    if name == "TodoWrite" and isinstance(args.get("todos"), (list, tuple)):
        display.todos = [item for item in args["todos"][:64] if isinstance(item, dict)]
    return safe_tool_display(display.model_dump()) or ToolDisplay().model_dump(exclude_none=True)
