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
        if raw.is_absolute():
            candidate = raw.resolve(strict=must_exist)
        else:
            candidate = (self.root / raw).resolve(strict=must_exist)
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise StudioError(
                "WORKSPACE_PATH_FORBIDDEN",
                "路径不在当前工作区内",
                status_code=403,
                details={"path": str(relative)},
            ) from exc
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
