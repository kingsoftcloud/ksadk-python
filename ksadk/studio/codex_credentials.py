"""Keep local MCP OAuth grants usable across one Agent's immutable builds.

Plugin snapshots and native transcripts remain build-isolated. Only grants for
explicitly bound, identical MCP server names and URLs may cross that boundary;
Codex account credentials and other Agents' grants never do.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

from ksadk.runtime.factory import _isolated_codex_home
from ksadk.studio.codex_builder import CodexBuildRecord


def restore_mcp_oauth_credentials(
    project_dir: Path,
    build: CodexBuildRecord,
    builds: Iterable[CodexBuildRecord],
    servers: list[dict[str, Any]],
) -> None:
    allowed = {
        (str(server.get("name") or ""), str(server.get("url") or ""))
        for server in servers
        if server.get("enabled", True) and server.get("url") and not server.get("env_key")
    }
    if not allowed:
        return
    home = _isolated_codex_home(project_dir, build.id)
    marker = home / ".ksadk-mcp-oauth-initialized"
    if marker.exists():
        return
    target = home / ".credentials.json"
    if target.is_symlink() or marker.is_symlink():
        raise ValueError("Codex MCP credential store must not be a symlink")
    current = _read(target)
    merged = dict(current)
    previous = max(
        (
            candidate
            for candidate in builds
            if candidate.agent_name == build.agent_name
            and candidate.id != build.id
            and candidate.created_at <= build.created_at
            and re.fullmatch(r"build_[0-9a-f]{8,64}", candidate.id)
        ),
        key=lambda candidate: (candidate.created_at, candidate.source_revision),
        default=None,
    )
    # Only the immediately preceding build may contribute grants. Searching
    # further back could resurrect credentials removed by a user's logout.
    if previous is not None:
        source = home.parent / previous.id / ".credentials.json"
        if source.is_file() and not source.is_symlink() and not source.parent.is_symlink():
            for key, grant in _read(source).items():
                if key in merged or not isinstance(grant, dict):
                    continue
                if (grant.get("server_name"), grant.get("server_url")) in allowed:
                    merged[key] = grant
    if merged != current:
        fd, temporary = tempfile.mkstemp(prefix=".mcp-oauth-", dir=home)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                # mkstemp creates a private 0600 file. Never log or serialize
                # this into a Build, deployment manifest, or response.
                json.dump(merged, stream)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    # Initialization is deliberately one-shot, even if no grant was found.
    # Subsequent login, refresh and logout belong to the current build.
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    os.close(fd)


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Invalid Codex MCP credential store")
    return value
