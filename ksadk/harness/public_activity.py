"""Small, display-safe facts about actions, never a copy of tool arguments."""

from __future__ import annotations

import ipaddress
import re
import shlex
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_SENSITIVE = re.compile(
    r"(?i)(?:api[_ -]?key|secret|password|passwd|authorization|bearer|token|"
    r"sk-[a-z0-9]|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})"
)
_SECRET_VALUE = re.compile(
    r"(?i)(?:\b(?:api[_ -]?key|secret|password|passwd|authorization|access[_ -]?token|"
    r"refresh[_ -]?token)\b\s*[:=]\s*\S+|\bbearer\s+\S+|\bsk-[a-z0-9_-]{12,})"
)


def _text(value: Any) -> str:
    # Tool arguments are untrusted payloads.  Keep the deliberately broad
    # policy here: even a credential-shaped field name or URL path is enough
    # to omit the action rather than risk displaying it.
    if not isinstance(value, str) or _SENSITIVE.search(value):
        return ""
    # Free-text search requests can embed URLs too; never publish credentials.
    if re.search(r"https?://[^\s/]*@", value):
        return ""
    return " ".join(value.split())[:180]


def public_commentary_text(value: Any, *, limit: int = 360) -> str:
    """Return model-authored text that is safe to show as public progress.

    This accepts only the assistant's public text channel.  Provider reasoning,
    tool arguments and tool results never call this helper.  Fail closed for
    credentials and for tool-protocol fragments that some OpenAI-compatible
    providers accidentally place in ``content`` alongside a tool call.
    """
    # Public assistant prose may legitimately discuss concepts such as a
    # "token budget".  Only reject text that contains a credential value;
    # unlike tool arguments, this channel has already been separated from
    # provider reasoning and tool payloads.
    if not isinstance(value, str) or _SECRET_VALUE.search(value):
        return ""
    if re.search(r"https?://[^\s/]*@", value):
        return ""
    if re.search(
        r"(?i)(?:<\|\s*DSML\s*\||</?calls?>|</?invoke\b|<parameter\b|"
        r"tool_calls?\s*[:=]|function_call\s*[:=])",
        value,
    ):
        return ""
    # Child conclusions are often Markdown.  The activity feed is prose, so
    # remove presentation markers while retaining the actual conclusion.
    text = re.sub(r"(?m)^\s{0,3}(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)", "", value)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = " ".join(text.split()).strip()
    # Streaming providers can occasionally leave a trailing punctuation-only
    # content fragment after a tool call.  It conveys no progress and would
    # otherwise render as a stray activity row such as ``。``.
    if not re.search(r"[A-Za-z0-9\u3400-\u9fff]", text):
        return ""
    # Tool-preface boilerplate is not a useful user-facing checkpoint.  It is
    # intentionally omitted instead of translated into another canned phrase.
    if re.fullmatch(
        r"(?i)(?:let me|i(?:'ll| will)|we(?:'ll| will))\s+"
        r"(?:delegate|search|look up|check|call|use|run)\b.*",
        text,
    ):
        return ""
    if len(text) <= limit:
        return text
    shortened = text[:limit]
    boundary = max(shortened.rfind(mark) for mark in "。！？；.!?;")
    if boundary >= max(40, limit // 2):
        shortened = shortened[: boundary + 1]
    return shortened.rstrip() + "…"


def tool_public_action(payload: dict[str, Any]) -> dict[str, str] | None:
    """Allowlist display fields; omit results, command arguments and credentials."""
    name = str(payload.get("name") or "").lower()
    args = payload.get("args")
    if not isinstance(args, dict):
        return None
    if name == "web_search":
        query = _text(args.get("query"))
        return {"text": f"搜索资料：{query}"} if query else None
    if name == "web_fetch":
        try:
            url = urlsplit(str(args.get("url") or ""))
            host = url.hostname or ""
            if url.scheme not in {"https", "http"} or not host:
                return None
            if host == "localhost" or "." not in host or host.endswith((".local", ".internal")):
                return None
            try:
                if not ipaddress.ip_address(host).is_global:
                    return None
            except ValueError:
                pass
            # Queries, fragments and URL credentials never become presentation data.
            safe = urlunsplit((url.scheme, host, url.path, "", ""))
            if not _text(safe) or len(safe) > 300:
                return None
            return {"text": f"查看网页：{host}{url.path}", "href": safe}
        except ValueError:
            return None
    if name in {
        "read_workspace_file",
        "write_workspace_file",
        "edit_workspace_file",
        "list_workspace_files",
    }:
        target = _text(PurePosixPath(str(args.get("path") or "").replace("\\", "/")).name)
        if not target or target in {".", "..", ".env"}:
            return None
        verb = (
            "保存文件"
            if name == "write_workspace_file"
            else "编辑文件"
            if name == "edit_workspace_file"
            else "查看文件"
            if name == "read_workspace_file"
            else "查看目录"
        )
        return {"text": f"{verb}：{target}"}
    if name in {"exec_command", "run_command", "execute_command", "run_workspace_command"}:
        try:
            words = shlex.split(str(args.get("command") or args.get("cmd") or ""))
        except ValueError:
            return None
        command = PurePosixPath(words[0]).name if words else ""
        if command in {
            "rg",
            "git",
            "ls",
            "cat",
            "sed",
            "python",
            "python3",
            "node",
            "npm",
            "uv",
            "pytest",
        }:
            return {"text": f"运行命令：{command}（参数未展示）"}
    return None


__all__ = ["public_commentary_text", "tool_public_action"]
