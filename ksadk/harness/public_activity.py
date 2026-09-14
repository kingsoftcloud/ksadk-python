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


def _text(value: Any) -> str:
    if not isinstance(value, str) or _SENSITIVE.search(value):
        return ""
    # Free-text search requests can embed URLs too; never publish credentials.
    if re.search(r"https?://[^\s/]*@", value):
        return ""
    return " ".join(value.split())[:180]


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
