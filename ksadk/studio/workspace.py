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
        raw = Path(relative)
        root_path = os.fspath(self.root)

        def reject_outside(requested: Path | str) -> None:
            raise StudioError(
                "WORKSPACE_PATH_FORBIDDEN",
                "路径不在当前工作区内",
                status_code=403,
                details={"path": str(requested)},
            )

        def is_contained(candidate_path: str) -> bool:
            try:
                return os.path.commonpath((root_path, candidate_path)) == root_path
            except ValueError:
                # Different drives on Windows cannot share a workspace root.
                return False

        # Normalize and validate the lexical path before resolving filesystem
        # links.  Keeping untrusted input out of Path.resolve makes this data
        # flow auditable while the second containment check below still blocks
        # symlinks that leave the workspace.
        if ".." in raw.parts:
            reject_outside(relative)

        raw_path = os.fspath(raw)
        requested_path = (
            raw_path if raw.is_absolute() else os.path.join(root_path, raw_path)
        )
        lexical_path = os.path.abspath(requested_path)
        if not is_contained(lexical_path):
            reject_outside(relative)

        candidate_path = os.path.realpath(lexical_path)
        if not is_contained(candidate_path):
            reject_outside(relative)

        candidate = Path(candidate_path)
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
