"""Bounded, read-only identity for a local workspace without Git metadata."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from ..native.ports import WorkspaceObservation, WorkspaceRequest
from .native_git import GitWorkspacePort


class _CaptureUnavailable(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    path: str
    kind: Literal["directory", "file", "symlink"]
    mode: int
    content_digest: str | None


class DirectoryWorkspacePort:
    """Hash a scoped directory without following symlinks or writing metadata."""

    def __init__(
        self,
        project_dir: str | Path,
        *,
        max_entries: int = 2048,
        max_hashed_bytes: int = 64 * 1024 * 1024,
        ignored_path_parts: tuple[str, ...] = (),
    ) -> None:
        if max_entries <= 0 or max_hashed_bytes <= 0:
            raise ValueError("workspace capture limits must be positive")
        self.root = Path(project_dir).resolve()
        self.max_entries = max_entries
        self.max_hashed_bytes = max_hashed_bytes
        self.ignored_path_parts = frozenset(ignored_path_parts)

    async def capture(self, request: WorkspaceRequest) -> WorkspaceObservation:
        return await asyncio.to_thread(self._capture, request.operation_key)

    def _capture(self, operation_key: str) -> WorkspaceObservation:
        observation, _entries = self._capture_inventory(operation_key)
        return observation

    async def capture_inventory(
        self, *, operation_key: str
    ) -> tuple[WorkspaceObservation, tuple[DirectoryEntry, ...]]:
        """Expose the same stable capture's entries; never a second run ledger."""
        return await asyncio.to_thread(self._capture_inventory, operation_key)

    def _capture_inventory(
        self, operation_key: str
    ) -> tuple[WorkspaceObservation, tuple[DirectoryEntry, ...]]:
        try:
            first, paths, entries = self._scan()
            second, second_paths, second_entries = self._scan()
        except _CaptureUnavailable as exc:
            return WorkspaceObservation(
                operation_key=operation_key, status="unavailable", reason=exc.reason
            ), ()
        except OSError:
            return WorkspaceObservation(
                operation_key=operation_key, status="unavailable", reason="capture_failed"
            ), ()
        if first != second or paths != second_paths or entries != second_entries:
            return WorkspaceObservation(
                operation_key=operation_key,
                status="unavailable",
                reason="workspace_changed_during_capture",
            ), ()
        return WorkspaceObservation(
            operation_key=operation_key,
            status="captured",
            revision="sha256:" + first,
            # A directory has no stable baseline analogous to Git HEAD.
            changed_paths=(),
        ), entries

    def _scan(self) -> tuple[str, tuple[str, ...], tuple[DirectoryEntry, ...]]:
        if not self.root.is_dir():
            raise _CaptureUnavailable("workspace_missing")
        digest = hashlib.sha256(b"ksadk.directory-workspace/v1\0")
        entries: list[str] = []
        inventory: list[DirectoryEntry] = []
        hashed_bytes = 0

        def onerror(error: OSError) -> None:
            raise error

        for directory, dirnames, filenames, dirfd in os.fwalk(
            self.root, topdown=True, follow_symlinks=False, onerror=onerror
        ):
            relative_dir = Path(directory).relative_to(self.root)
            dirnames[:] = sorted(name for name in dirnames if name not in self.ignored_path_parts)
            filenames[:] = sorted(name for name in filenames if name not in self.ignored_path_parts)
            for name in sorted((*dirnames, *filenames)):
                relative = (relative_dir / name).as_posix()
                try:
                    relative.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise _CaptureUnavailable("unsupported_workspace_path") from exc
                if (
                    "\\" in relative
                    or PurePosixPath(relative).as_posix() != relative
                    or ".." in PurePosixPath(relative).parts
                ):
                    raise _CaptureUnavailable("unsupported_workspace_path")
                entries.append(relative)
                if len(entries) > self.max_entries:
                    raise _CaptureUnavailable("capture_limit_exceeded")
                before = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
                digest.update(b"\0path\0" + relative.encode("utf-8"))
                digest.update(f"\0mode:{stat.S_IMODE(before.st_mode):o}".encode())
                content_digest = None
                if stat.S_ISDIR(before.st_mode):
                    kind = "directory"
                    digest.update(b"\0directory")
                elif stat.S_ISLNK(before.st_mode):
                    kind = "symlink"
                    target = os.readlink(name, dir_fd=dirfd).encode("utf-8", "surrogateescape")
                    hashed_bytes += len(target)
                    digest.update(b"\0symlink\0" + target)
                    content_digest = "sha256:" + hashlib.sha256(target).hexdigest()
                elif stat.S_ISREG(before.st_mode):
                    kind = "file"
                    if hashed_bytes + before.st_size > self.max_hashed_bytes:
                        raise _CaptureUnavailable("capture_limit_exceeded")
                    file_hash = hashlib.sha256()
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dirfd)
                    with os.fdopen(fd, "rb") as stream:
                        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                            raise _CaptureUnavailable("unsupported_workspace_entry")
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            hashed_bytes += len(chunk)
                            if hashed_bytes > self.max_hashed_bytes:
                                raise _CaptureUnavailable("capture_limit_exceeded")
                            file_hash.update(chunk)
                    digest.update(b"\0file\0" + file_hash.digest())
                    content_digest = "sha256:" + file_hash.hexdigest()
                else:
                    raise _CaptureUnavailable("unsupported_workspace_entry")
                after = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
                if _identity(before) != _identity(after):
                    raise _CaptureUnavailable("workspace_changed_during_capture")
                if hashed_bytes > self.max_hashed_bytes:
                    raise _CaptureUnavailable("capture_limit_exceeded")
                inventory.append(
                    DirectoryEntry(relative, kind, stat.S_IMODE(before.st_mode), content_digest)
                )
        return digest.hexdigest(), tuple(sorted(entries)), tuple(inventory)


class LocalWorkspacePort:
    """Use Git identity where available, with a bounded directory fallback."""

    def __init__(
        self, project_dir: str | Path, *, ignored_path_parts: tuple[str, ...] = ()
    ) -> None:
        self.git = GitWorkspacePort(project_dir, ignored_path_parts=ignored_path_parts)
        self.directory = DirectoryWorkspacePort(project_dir, ignored_path_parts=ignored_path_parts)

    async def capture(self, request: WorkspaceRequest) -> WorkspaceObservation:
        observed = await self.git.capture(request)
        if observed.reason in {"not_git_repository", "workspace_scope_ignored_by_git"}:
            return await self.directory.capture(request)
        return observed


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino
