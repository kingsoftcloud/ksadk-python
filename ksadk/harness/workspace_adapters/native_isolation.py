"""Host-owned local child file workspaces and version-checked publication.

These are immutable input assets, not a second Run ledger. File tools are
confined to the child directory; arbitrary host-user processes are not a
sandbox. The host keeps workspaces for follow-up and recovery inspection.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from contextlib import ExitStack, closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from ksadk.toolsets.workspace_mutation import locked_workspace_paths, publish_workspace_bytes

from .native_directory import DirectoryEntry, DirectoryWorkspacePort

_IGNORED = (".git", ".agentkit", ".harness-tools", ".artifacts")
_MAX_BYTES = 64 * 1024 * 1024


class ChildWorkspaceError(ValueError):
    """A safe host diagnostic that contains no source text or credentials."""


@dataclass(frozen=True)
class ChildFileWorkspace:
    isolation_id: str
    root: Path
    baseline: Path
    base_revision: str


@dataclass(frozen=True)
class ChildFilePublication:
    """A host grant from an acknowledged merge, never inferred from disk."""

    revision: str
    inventory_digest: str


def builtin_state_database(writable_root: Path, workspace_root: Path) -> Path:
    """The same existing observation database used by SDK builtin workers."""
    return (
        writable_root
        / ".harness-tools/state"
        / (hashlib.sha256(str(workspace_root.resolve()).encode()).hexdigest() + ".sqlite")
    )


@contextmanager
def _worker_transactions(databases):
    # Coordinate with the actual worker's BEGIN IMMEDIATE. An in-process
    # RLock alone cannot serialize a different builtin subprocess.
    with ExitStack() as stack:
        for path in sorted(set(Path(value).resolve() for value in databases)):
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = stack.enter_context(closing(sqlite3.connect(path, timeout=10)))
            stack.enter_context(connection)
            connection.execute("BEGIN IMMEDIATE")
        yield


def _inventory(root: Path):
    observed, entries = DirectoryWorkspacePort(
        root, ignored_path_parts=_IGNORED
    )._capture_inventory("child-workspace-input")
    if observed.status != "captured":
        raise ChildWorkspaceError("child_workspace_" + (observed.reason or "unavailable"))
    if any(entry.kind not in {"file", "directory"} for entry in entries):
        raise ChildWorkspaceError("child_workspace_symlink_or_special_entry")
    return observed.revision, {entry.path: entry for entry in entries}


def _payload(root: Path, relative: str, expected):
    target = root / relative
    if target.is_symlink() or target.resolve() != target or not target.is_file():
        raise ChildWorkspaceError("child_workspace_file_changed")
    with target.open("rb") as stream:
        value = stream.read(_MAX_BYTES + 1)
    if len(value) > _MAX_BYTES or (
        "sha256:" + hashlib.sha256(value).hexdigest() != expected.content_digest
    ):
        raise ChildWorkspaceError("child_workspace_file_changed")
    return value


class DirectoryChildWorkspaces:
    """Freeze actual input files once, preserving dirty and untracked content."""

    def __init__(self, *, source: Path, storage: Path, source_database: Path):
        self.source = source.resolve()
        self.storage = storage.resolve()
        self.source_database = source_database.resolve()

    def observer(self, workspace: ChildFileWorkspace):
        return DirectoryWorkspacePort(workspace.root, ignored_path_parts=_IGNORED)

    def _load(self, target: Path, identity: str) -> ChildFileWorkspace:
        manifest = target / "inputs.json"
        if manifest.is_symlink() or target.is_symlink():
            raise ChildWorkspaceError("child_workspace_identity_changed")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if (
            data.get("version") != "native-child-files/v1"
            or data.get("identity") != identity
            or data.get("source") != str(self.source)
            or (target / "work").is_symlink()
            or not (target / "work").is_dir()
            or (target / "base").is_symlink()
        ):
            raise ChildWorkspaceError("child_workspace_identity_changed")
        return ChildFileWorkspace(identity, target / "work", target / "base", data["base_revision"])

    def prepare(self, identity: str) -> ChildFileWorkspace:
        # The identity comes from scoped host assembly, never a model path.
        if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
            raise ChildWorkspaceError("child_workspace_identity_invalid")
        self.storage.mkdir(parents=True, exist_ok=True)
        target = self.storage / identity
        with locked_workspace_paths((target,)):
            if target.exists():
                return self._load(target, identity)
            temporary = Path(tempfile.mkdtemp(prefix=".child-input-", dir=self.storage))
            try:
                with _worker_transactions((self.source_database,)):
                    if target.exists():
                        return self._load(target, identity)
                    revision, entries = _inventory(self.source)
                    for tree in ("base", "work"):
                        (temporary / tree).mkdir()
                    for relative, entry in entries.items():
                        destinations = [temporary / tree / relative for tree in ("base", "work")]
                        for destination in destinations:
                            if entry.kind == "directory":
                                destination.mkdir(parents=True, exist_ok=True)
                            else:
                                destination.parent.mkdir(parents=True, exist_ok=True)
                        if entry.kind == "file":
                            value = _payload(self.source, relative, entry)
                            for destination in destinations:
                                destination.write_bytes(value)
                                destination.chmod(entry.mode)
                    # Preserve directory modes after the complete tree exists.
                    for relative, entry in reversed(tuple(entries.items())):
                        if entry.kind == "directory":
                            for tree in ("base", "work"):
                                (temporary / tree / relative).chmod(entry.mode)
                    current, _ = _inventory(self.source)
                    if current != revision:
                        raise ChildWorkspaceError("child_workspace_source_changed")
                    (temporary / "inputs.json").write_text(
                        json.dumps(
                            {
                                "version": "native-child-files/v1",
                                "identity": identity,
                                "source": str(self.source),
                                "base_revision": revision,
                            },
                            sort_keys=True,
                        ),
                        encoding="utf-8",
                    )
                    try:
                        temporary.rename(target)
                    except FileExistsError:
                        # Another process may have published the same frozen
                        # identity. Reuse its inputs; never reset the child.
                        pass
                return self._load(target, identity)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)

    def _publication_path(self, workspace: ChildFileWorkspace, revision: str) -> Path:
        if (not isinstance(revision, str) or not revision.startswith("sha256:")
                or len(revision) != 71 or any(c not in "0123456789abcdef" for c in revision[7:])):
            raise ChildWorkspaceError("child_publication_identity_invalid")
        return workspace.root.parent / "publications" / (revision[7:] + ".json")

    def _publication_inventory(self, workspace, publication):
        target = self._publication_path(workspace, publication.revision)
        if target.is_symlink() or target.parent.is_symlink():
            raise ChildWorkspaceError("child_publication_inventory_changed")
        value = target.read_bytes()
        if "sha256:" + hashlib.sha256(value).hexdigest() != publication.inventory_digest:
            raise ChildWorkspaceError("child_publication_inventory_changed")
        data = json.loads(value)
        if (
            data.get("version") != "native-child-publication/v1"
            or data.get("isolation_id") != workspace.isolation_id
            or data.get("source") != str(self.source)
            or data.get("base_revision") != workspace.base_revision
            or data.get("child_revision") != publication.revision
        ):
            raise ChildWorkspaceError("child_publication_inventory_changed")
        return {entry["path"]: DirectoryEntry(**entry) for entry in data["entries"]}

    def _save_publication(self, workspace, revision, entries):
        # This immutable inventory is a comparison asset. Its existence does
        # not acknowledge a publication; only the original effect receipt can.
        target = self._publication_path(workspace, revision)
        value = json.dumps({
            "version": "native-child-publication/v1", "isolation_id": workspace.isolation_id,
            "source": str(self.source), "base_revision": workspace.base_revision,
            "child_revision": revision, "entries": [asdict(entry) for entry in entries.values()],
        }, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        target.parent.mkdir(exist_ok=True)
        if target.is_symlink() or target.parent.is_symlink():
            raise ChildWorkspaceError("child_publication_inventory_changed")
        if target.exists():
            if target.read_bytes() != value:
                raise ChildWorkspaceError("child_publication_inventory_changed")
        else:
            publish_workspace_bytes(target, value, create_only=True)
        return {
            "revision": revision,
            "inventory_digest": "sha256:" + hashlib.sha256(value).hexdigest(),
        }

    def merge(
        self, workspace: ChildFileWorkspace, *, expected_revision: str,
        previous: ChildFilePublication | None = None,
    ):
        """Publish eligible files only when the parent still matches the base.

        A conflict publishes nothing. An OS failure can leave a known prefix
        applied; that prefix is reported instead of claiming batch atomicity.
        """
        child_database = builtin_state_database(workspace.root.parent / "runtime", workspace.root)
        with _worker_transactions((self.source_database, child_database)):
            base_revision, baseline = _inventory(workspace.baseline)
            child_revision, proposal = _inventory(workspace.root)
            if base_revision != workspace.base_revision:
                raise ChildWorkspaceError("child_workspace_baseline_changed")
            if child_revision != expected_revision:
                raise ChildWorkspaceError("child_workspace_after_delivery_changed")
            if previous is not None:
                baseline = self._publication_inventory(workspace, previous)
            files = {
                path
                for path in baseline.keys() | proposal.keys()
                if (baseline.get(path) and baseline[path].kind == "file")
                or (proposal.get(path) and proposal[path].kind == "file")
            }
            changed = sorted(path for path in files if baseline.get(path) != proposal.get(path))
            if any(
                entry is not None and entry.kind != "file"
                for path in changed
                for entry in (baseline.get(path), proposal.get(path))
            ):
                raise ChildWorkspaceError("child_workspace_file_kind_changed")
            directories = {
                path
                for path in baseline.keys() | proposal.keys()
                if (baseline.get(path) and baseline[path].kind == "directory")
                or (proposal.get(path) and proposal[path].kind == "directory")
            }
            changed_directories = {
                path for path in directories if baseline.get(path) != proposal.get(path)
            }
            removed_directories = {path for path in changed_directories if path not in proposal}
            with locked_workspace_paths(
                self.source / path for path in set(changed) | changed_directories
            ):
                _, parent = _inventory(self.source)
                conflicts = [
                    path
                    for path in changed
                    if parent.get(path) not in (baseline.get(path), proposal.get(path))
                ]
                conflicts.extend(
                    path
                    for path in changed_directories
                    if parent.get(path) not in (baseline.get(path), proposal.get(path))
                )
                conflicts.extend(
                    path
                    for path in parent
                    if path not in baseline
                    and any(path.startswith(directory + "/") for directory in removed_directories)
                )
                if conflicts:
                    return {
                        "ok": False,
                        "error_code": "child_workspace_merge_conflict",
                        "conflicts": sorted(set(conflicts)),
                        "applied": [],
                        "instruction": "Read the current parent file and resolve the conflict "
                        "with its ordinary authorized editing tools. Nothing was merged.",
                    }
                payloads = {
                    path: _payload(workspace.root, path, proposal[path])
                    for path in changed
                    if path in proposal
                }
                publication = self._save_publication(workspace, child_revision, proposal)
                applied, already_present = [], []
                create_or_mode = sorted(
                    changed_directories - removed_directories,
                    key=lambda value: (value.count("/"), value),
                )
                remove = sorted(removed_directories, key=lambda value: (-value.count("/"), value))
                for path in [*create_or_mode, *changed, *remove]:
                    if parent.get(path) == proposal.get(path):
                        already_present.append(path)
                        continue
                    target = self.source / path
                    try:
                        if path in changed_directories:
                            if path not in proposal:
                                # Never recursively delete late parent files.
                                target.rmdir()
                            elif path not in parent:
                                target.mkdir()
                            applied.append(path)
                            if path in proposal:
                                target.chmod(proposal[path].mode)
                        elif path in proposal:
                            publish_workspace_bytes(
                                target, payloads[path], create_only=path not in baseline
                            )
                            applied.append(path)
                            target.chmod(proposal[path].mode)
                        else:
                            target.unlink()
                            applied.append(path)
                    except OSError:
                        return {
                            "ok": False,
                            "error_code": "child_workspace_publish_failed",
                            "applied": applied,
                            "failed_path": path,
                            "instruction": "Inspect the reported files before continuing; "
                            "this is not an atomic multi-file transaction.",
                        }
                return {
                    "ok": True,
                    "applied": applied,
                    "already_present": already_present,
                    "base_revision": base_revision,
                    "child_revision": child_revision,
                    "comparison_revision": previous.revision if previous else base_revision,
                    "publication": publication,
                }
