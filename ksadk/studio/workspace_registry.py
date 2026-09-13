"""Durable local workspace identities and linked directory permissions."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import RLock
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
        tmp.write_text(
            json.dumps(
                {"version": 1, "items": [asdict(i) for i in items]}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def open(self, path: Path | str, *, create: bool = False) -> WorkspaceRecord:
        root = _canonical(path)
        if create:
            root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise FileNotFoundError(root)
        items = self._read()
        existing = next((i for i in items if _canonical(i.path) == root), None)
        record = existing or WorkspaceRecord(
            hashlib.sha256(str(root).encode()).hexdigest()[:24],
            str(root),
            root.name or "workspace",
            time.time(),
        )
        record = WorkspaceRecord(record.workspace_id, str(root), record.name, time.time())
        self._write([record] + [i for i in items if i.workspace_id != record.workspace_id])
        return record

    def list(self) -> list[WorkspaceRecord]:
        return sorted(self._read(), key=lambda i: i.last_opened_at, reverse=True)


class WorkspaceRuntimeManager:
    """Keeps one isolated Studio service per workspace and proxies the active one."""

    def __init__(self, initial, factory, *, registry: WorkspaceRegistry | None = None) -> None:
        self._factory = factory
        self._registry = registry or WorkspaceRegistry()
        self._lock = RLock()
        self._services = {_canonical(initial.workspace.root): initial}
        self._active = _canonical(initial.workspace.root)

    @property
    def active(self):
        return self._services[self._active]

    def __getattr__(self, name):
        return getattr(self.active, name)

    def switch(self, path: str | Path, *, create: bool = False):
        root = _canonical(path)
        with self._lock:
            record = self._registry.open(root, create=create)
            if root not in self._services:
                self._services[root] = self._factory(root)
            self._active = root
            return record

    def services(self):
        return tuple(self._services.values())

    def all_runs(self) -> list[dict]:
        runs: list[dict] = []
        for runtime in self._services.values():
            workspace_id = getattr(runtime, "workspace_record", None)
            workspace_id = getattr(workspace_id, "workspace_id", None)
            for run in runtime.event_store.list_runs():
                item = run.model_dump(by_alias=True) if hasattr(run, "model_dump") else dict(run)
                item["workspaceId"] = workspace_id
                runs.append(item)
        return runs

    def runtime_for_run(self, run_id: str):
        for runtime in self._services.values():
            try:
                runtime.event_store.get(run_id)
                return runtime
            except Exception:
                continue
        return None

    def runtime_for_session(self, session_id: str):
        for runtime in self._services.values():
            try:
                if runtime.event_store.list_runs(session_id=session_id):
                    return runtime
            except Exception:
                continue
            # A newly created local session has no Run yet, but it is still a
            # valid routing target for replay and SSE.  Consult the local
            # session index before declaring it missing.
            try:
                session = runtime.session_service._get_session_sync(  # type: ignore[attr-defined]
                    session_id, include_events=False
                )
                if session is not None:
                    return runtime
            except Exception:
                continue
        return None

    def runtime_for_operation(self, operation_id: str):
        for runtime in self._services.values():
            try:
                runtime.operations.get(operation_id)
                return runtime
            except Exception:
                continue
        return None

    def all_traces(self) -> list[dict]:
        traces: list[dict] = []
        for runtime in self._services.values():
            wid = getattr(getattr(runtime, "workspace_record", None), "workspace_id", None)
            page = runtime.event_store.list_traces_page(limit=1000)
            for trace in page.get("items", []):
                item = dict(trace)
                item["workspaceId"] = wid
                traces.append(item)
        return traces

    def all_operations(self) -> list[dict]:
        operations: list[dict] = []
        for runtime in self._services.values():
            wid = getattr(getattr(runtime, "workspace_record", None), "workspace_id", None)
            for operation in runtime.operations.list():
                item = operation.model_dump(by_alias=True) if hasattr(operation, "model_dump") else dict(operation)
                item["workspaceId"] = wid
                operations.append(item)
        return operations


class LinkedDirectoryPolicy:
    """Persist explicit directory relationships in the private workspace config."""

    def __init__(self, workspace_root: Path | str) -> None:
        self.path = _canonical(workspace_root) / ".agentkit" / "linked-directories.json"

    def list(self) -> list[LinkedDirectory]:
        try:
            return [
                LinkedDirectory(**x) for x in json.loads(self.path.read_text()).get("items", [])
            ]
        except (OSError, ValueError, TypeError, KeyError):
            return []

    def set(
        self, path: Path | str, mode: AccessMode = "read", label: str | None = None
    ) -> LinkedDirectory:
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
        tmp.write_text(
            json.dumps(
                {"version": 1, "items": [asdict(x) for x in items]}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)
        return item

    def remove(self, path: Path | str) -> bool:
        target = _canonical(path)
        items = [x for x in self.list() if _canonical(x.path) != target]
        changed = len(items) != len(self.list())
        if changed:
            self.path.write_text(
                json.dumps(
                    {"version": 1, "items": [asdict(x) for x in items]},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        return changed

    def authorize(self, path: Path | str, operation: Literal["read", "write"] = "read") -> bool:
        """Return whether an explicit file operation is covered by this policy."""
        target = _canonical(path)
        workspace_root = self.path.parent.parent.resolve()
        if target == workspace_root or workspace_root in target.parents:
            return True
        for item in self.list():
            root = _canonical(item.path)
            if target == root or root in target.parents:
                return operation == "read" or item.mode == "write"
        return False
