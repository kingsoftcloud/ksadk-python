"""Workspace initialization and path-safe file operations."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from ksadk.studio.errors import StudioError


class Workspace:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()

    def matches_configured_root_path(self, path: str) -> bool:
        """Return whether a no-op workspace-open request names this exact root.

        The local Studio daemon is intentionally bound to one workspace for its
        lifetime.  Comparing the advertised canonical string avoids resolving
        a client-supplied pathname in the workspace-open endpoint.
        """

        return path == os.fspath(self.root)

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for relative in (
            "agents",
            "capabilities/skills",
            "capabilities/tools",
            "environments",
            ".agentkit/catalog/models",
            ".agentkit/catalog/mcp",
            ".agentkit/catalog/tools",
            ".agentkit/catalog/tool-sources",
            ".agentkit/codex-drafts",
            ".agentkit/builds",
            ".agentkit/operations",
            ".agentkit/runs",
            ".agentkit/traces",
            ".agentkit/assets/agent-avatars",
            ".agentkit/cache",
            ".agentkit/trash",
            "dist",
        ):
            (self.root / relative).mkdir(parents=True, exist_ok=True)
        manifest = self.root / "agentkit.yaml"
        if not manifest.exists():
            self.atomic_write_yaml(
                manifest,
                {
                    "apiVersion": "agentkit.ksyun.com/v1alpha1",
                    "kind": "AgentWorkspace",
                    "metadata": {"name": self.root.name or "agentkit-workspace"},
                },
            )
        gitignore = self.root / ".gitignore"
        wanted = ".agentkit/secrets.env\n"
        existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
        if ".agentkit/secrets.env" not in existing.splitlines():
            gitignore.write_text(
                (existing + ("\n" if existing and not existing.endswith("\n") else "") + wanted),
                encoding="utf-8",
            )

    def resolve(self, relative: Path | str, *, must_exist: bool = False) -> Path:
        root_path = os.path.realpath(os.fspath(self.root))
        raw_path = os.fspath(relative)
        requested_path = (
            raw_path if os.path.isabs(raw_path) else os.path.join(root_path, raw_path)
        )
        candidate_path = os.path.realpath(requested_path)
        if candidate_path == root_path:
            candidate = self.root
        else:
            root_prefix = root_path if root_path.endswith(os.sep) else f"{root_path}{os.sep}"
            # `realpath` resolves traversal and symlinks before this segment-boundary check.
            if candidate_path.startswith(root_prefix):
                candidate = Path(candidate_path)
            else:
                raise StudioError(
                    "WORKSPACE_PATH_FORBIDDEN",
                    "路径不在当前工作区内",
                    status_code=403,
                    details={"path": str(relative)},
                )
        if must_exist:
            candidate.stat()
        return candidate

    def relative(self, path: Path | str) -> str:
        candidate = self.resolve(path)
        return candidate.relative_to(self.root).as_posix()

    def atomic_write_text(self, path: Path | str, content: str) -> None:
        target = self.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def atomic_write_bytes(self, path: Path | str, content: bytes) -> None:
        target = self.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def atomic_write_yaml(self, path: Path | str, payload: object) -> None:
        content = yaml.safe_dump(
            payload,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        self.atomic_write_text(path, content)
