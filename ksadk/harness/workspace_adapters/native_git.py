"""Read-only Git workspace identity captured at model-request boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import subprocess
from pathlib import Path

from ..native.ports import WorkspaceObservation, WorkspaceRequest


class GitWorkspacePort:
    """Hash the current scoped worktree without modifying its real Git index."""

    def __init__(
        self,
        project_dir: str | Path,
        *,
        command_timeout_seconds: float = 3,
        max_changed_paths: int = 2048,
        max_hashed_bytes: int = 64 * 1024 * 1024,
        ignored_path_parts: tuple[str, ...] = (),
    ) -> None:
        root = Path(project_dir).resolve()
        if command_timeout_seconds <= 0:
            raise ValueError("workspace command timeout must be positive")
        if max_changed_paths <= 0 or max_hashed_bytes <= 0:
            raise ValueError("workspace capture limits must be positive")
        self.root = root
        self.command_timeout_seconds = command_timeout_seconds
        self.max_changed_paths = max_changed_paths
        self.max_hashed_bytes = max_hashed_bytes
        self.ignored_path_parts = frozenset(ignored_path_parts)

    async def capture(self, request: WorkspaceRequest) -> WorkspaceObservation:
        return await asyncio.to_thread(self._capture, request.operation_key)

    def _capture(self, operation_key: str) -> WorkspaceObservation:
        inside = self._git(self.root, "rev-parse", "--is-inside-work-tree")
        if inside is None or inside.strip() != b"true":
            return self._unavailable(operation_key, "not_git_repository")
        top_output = self._git(self.root, "rev-parse", "--show-toplevel")
        if top_output is None:
            return self._unavailable(operation_key, "capture_failed")
        try:
            top = Path(os.fsdecode(top_output.strip())).resolve(strict=True)
            scope = self.root.relative_to(top)
        except (OSError, ValueError):
            return self._unavailable(operation_key, "workspace_scope_mismatch")
        pathspec = scope.as_posix() or "."

        # A Studio scratch workspace may live below an ignored directory of
        # the surrounding repository. Git reports no changed paths there, so
        # HEAD alone would appear to be a stable revision after every edit.
        # Let LocalWorkspacePort hash that scoped directory instead.
        if scope != Path(".") and self._git(top, "check-ignore", "-q", "--", pathspec) is not None:
            return self._unavailable(operation_key, "workspace_scope_ignored_by_git")

        raw_paths = self._changed_paths(top, pathspec)
        if raw_paths is None:
            return self._unavailable(operation_key, "capture_failed")
        if len(raw_paths) > self.max_changed_paths:
            return self._unavailable(operation_key, "capture_limit_exceeded")

        records: list[tuple[str, Path]] = []
        for raw_path in raw_paths:
            path = Path(raw_path)
            if path.is_absolute() or ".." in path.parts:
                return self._unavailable(operation_key, "workspace_scope_mismatch")
            candidate = top / path
            try:
                relative = candidate.relative_to(self.root).as_posix()
            except ValueError:
                return self._unavailable(operation_key, "workspace_scope_mismatch")
            if not relative or relative.startswith("../"):
                return self._unavailable(operation_key, "workspace_scope_mismatch")
            try:
                relative.encode("utf-8")
            except UnicodeEncodeError:
                return self._unavailable(operation_key, "unsupported_workspace_path")
            records.append((relative, candidate))
        records.sort(key=lambda item: item[0])

        head = self._git(top, "rev-parse", "--verify", "HEAD") or b"unborn"
        digest = hashlib.sha256()
        digest.update(b"ksadk.git-workspace/v1\0")
        digest.update(scope.as_posix().encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(head.strip())
        total = 0
        missing: list[Path] = []
        for relative, candidate in records:
            digest.update(b"\0path\0")
            digest.update(relative.encode("utf-8", "surrogateescape"))
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                digest.update(b"\0deleted")
                missing.append(candidate)
                continue
            digest.update(f"\0mode:{stat.S_IMODE(info.st_mode):o}".encode())
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(candidate).encode("utf-8", "surrogateescape")
                total += len(target)
                if total > self.max_hashed_bytes:
                    return self._unavailable(operation_key, "capture_limit_exceeded")
                digest.update(b"\0symlink\0")
                digest.update(target)
                if not self._same_stat(info, candidate):
                    return self._unavailable(operation_key, "workspace_changed_during_capture")
                continue
            if not stat.S_ISREG(info.st_mode):
                return self._unavailable(operation_key, "unsupported_workspace_entry")
            total += info.st_size
            if total > self.max_hashed_bytes:
                return self._unavailable(operation_key, "capture_limit_exceeded")
            file_digest = hashlib.sha256()
            try:
                with candidate.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        file_digest.update(chunk)
            except OSError:
                return self._unavailable(operation_key, "capture_failed")
            if not self._same_stat(info, candidate):
                return self._unavailable(operation_key, "workspace_changed_during_capture")
            digest.update(b"\0file\0")
            digest.update(file_digest.digest())

        final_paths = self._changed_paths(top, pathspec)
        final_head = self._git(top, "rev-parse", "--verify", "HEAD") or b"unborn"
        if (
            any(path.exists() or path.is_symlink() for path in missing)
            or final_paths != raw_paths
            or final_head.strip() != head.strip()
        ):
            return self._unavailable(operation_key, "workspace_changed_during_capture")

        return WorkspaceObservation(
            operation_key=operation_key,
            status="captured",
            revision=f"sha256:{digest.hexdigest()}",
            changed_paths=tuple(relative for relative, _ in records),
        )

    def _changed_paths(self, top: Path, pathspec: str) -> set[str] | None:
        outputs = (
            self._git(top, "diff", "--name-only", "-z", "--", pathspec),
            self._git(top, "diff", "--cached", "--name-only", "-z", "--", pathspec),
            self._git(top, "ls-files", "--others", "--exclude-standard", "-z", "--", pathspec),
        )
        if any(output is None for output in outputs):
            return None
        return {
            path
            for output in outputs
            for value in output.split(b"\0")
            if value
            for path in (os.fsdecode(value),)
            if not self.ignored_path_parts.intersection(Path(path).parts)
        }

    @staticmethod
    def _same_stat(before: os.stat_result, path: Path) -> bool:
        try:
            after = path.lstat()
        except OSError:
            return False
        return (
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            before.st_ino,
        ) == (
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_ino,
        )

    def _git(self, cwd: Path, *arguments: str) -> bytes | None:
        env = dict(os.environ)
        env["GIT_OPTIONAL_LOCKS"] = "0"
        try:
            result = subprocess.run(
                ("git", *arguments),
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=self.command_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout if result.returncode == 0 else None

    @staticmethod
    def _unavailable(operation_key: str, reason: str) -> WorkspaceObservation:
        return WorkspaceObservation(
            operation_key=operation_key,
            status="unavailable",
            reason=reason,
        )
