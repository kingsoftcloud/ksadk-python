"""Durable local workspace identities and linked directory permissions."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Literal

AccessMode = Literal["read", "write"]


def _canonical(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


@dataclass(frozen=True)
class WorkspaceRecord:
    workspace_id: str
    path: str
    name: str
    last_opened_at: float


@dataclass(frozen=True)
class LinkedDirectory:
    path: str
    mode: AccessMode = "read"
    label: str | None = None


class WorkspaceRegistry:
    """A small atomic registry; it stores paths and metadata, never secrets."""

    def __init__(self, home: Path | str | None = None) -> None:
        self.path = Path(home or (Path.home() / ".agentengine")) / "workspaces.json"

    def _read(self) -> list[WorkspaceRecord]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return [WorkspaceRecord(**item) for item in payload.get("items", [])]
        except (OSError, ValueError, TypeError, KeyError):
            return []

    def _write(self, items: list[WorkspaceRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "items": [asdict(i) for i in items]}, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def open(self, path: Path | str, *, create: bool = False) -> WorkspaceRecord:
        root = _canonical(path)
        if create:
            root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise FileNotFoundError(root)
        items = self._read()
        existing = next((i for i in items if _canonical(i.path) == root), None)
        record = existing or WorkspaceRecord(hashlib.sha256(str(root).encode()).hexdigest()[:24], str(root), root.name or "workspace", time.time())
        record = WorkspaceRecord(record.workspace_id, str(root), record.name, time.time())
        self._write([record] + [i for i in items if i.workspace_id != record.workspace_id])
        return record

    def list(self) -> list[WorkspaceRecord]:
        return sorted(self._read(), key=lambda i: i.last_opened_at, reverse=True)


class LinkedDirectoryPolicy:
    """Persist explicit directory relationships in the private workspace config."""

    def __init__(self, workspace_root: Path | str) -> None:
        self.path = _canonical(workspace_root) / ".agentkit" / "linked-directories.json"

    def list(self) -> list[LinkedDirectory]:
        try:
            return [LinkedDirectory(**x) for x in json.loads(self.path.read_text()).get("items", [])]
        except (OSError, ValueError, TypeError, KeyError):
            return []

    def set(self, path: Path | str, mode: AccessMode = "read", label: str | None = None) -> LinkedDirectory:
        if mode not in ("read", "write"):
            raise ValueError("mode must be read or write")
        target = _canonical(path)
        if not target.exists() or not target.is_dir():
            raise FileNotFoundError(target)
        item = LinkedDirectory(str(target), mode, label or target.name)
        items = [x for x in self.list() if _canonical(x.path) != target]
        items.append(item)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "items": [asdict(x) for x in items]}, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
        return item

    def remove(self, path: Path | str) -> bool:
        target = _canonical(path)
        items = [x for x in self.list() if _canonical(x.path) != target]
        changed = len(items) != len(self.list())
        if changed:
            self.path.write_text(json.dumps({"version": 1, "items": [asdict(x) for x in items]}, ensure_ascii=False, indent=2), encoding="utf-8")
        return changed
