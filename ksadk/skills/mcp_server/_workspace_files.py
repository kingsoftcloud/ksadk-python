"""Bounded file operations for native Skill Center workspace entries.

Hosted runtimes use POSIX directory descriptors. Keep every child operation
relative to an opened directory, refuse symlinks, and replace files atomically
instead of truncating a pathname that could have been swapped after validation.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)
MANAGED_MARKER = ".ksadk-skill-center"


def is_skill_name(name: str) -> bool:
    return bool(
        name
        and name.strip()
        and not name.startswith(".")
        and "/" not in name
        and "\\" not in name
        and not any(ord(char) < 32 for char in name)
    )


@contextmanager
def _directory(path: str, *, parent_fd: int | None = None) -> Iterator[int]:
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise OSError(errno.ENOTSUP, "Safe workspace updates require POSIX directory operations")
    # A trailing slash or '/.' must not turn the final symlink into an ancestor.
    path = os.path.normpath(path)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        yield fd
    finally:
        os.close(fd)


def _regular_file(directory_fd: int, name: str, *, missing_ok: bool = False) -> bool:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return missing_ok
    return stat.S_ISREG(info.st_mode)


def _replace_text(directory_fd: int, name: str, content: str) -> None:
    if not _regular_file(directory_fd, name, missing_ok=True):
        raise OSError(errno.EINVAL, "Refusing non-regular workspace file", name)
    try:
        mode = os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_mode & 0o777
    except FileNotFoundError:
        mode = 0o644
    temporary = f".ksadk-write-{uuid4().hex}"
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        mode,
        dir_fd=directory_fd,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
        if not _regular_file(directory_fd, name, missing_ok=True):
            raise OSError(errno.EINVAL, "Workspace file changed to a non-regular file", name)
        # Even a link introduced after the check is replaced, never followed.
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def write_skill(base_dir: str, name: str, content: str) -> None:
    if not is_skill_name(name):
        raise ValueError(f"Unsafe skill name: {name!r}")
    with _directory(base_dir) as base_fd:
        created = False
        try:
            os.mkdir(name, dir_fd=base_fd)
            created = True
        except FileExistsError:
            pass
        with _directory(name, parent_fd=base_fd) as skill_fd:
            if not created and not _regular_file(skill_fd, MANAGED_MARKER):
                raise OSError(errno.EINVAL, "Directory is not Skill Center-managed", name)
            if not _regular_file(skill_fd, MANAGED_MARKER, missing_ok=created):
                raise OSError(errno.EINVAL, "Refusing non-regular ownership marker", name)
            _replace_text(skill_fd, "SKILL.md", content)
            if created:
                _replace_text(skill_fd, MANAGED_MARKER, "1")


def cleanup_skills(base_dir: str, current_names: set[str]) -> None:
    """Remove generated files only; preserve links and user-added sidecar files."""
    with _directory(base_dir) as base_fd:
        for name in os.listdir(base_fd):
            if not is_skill_name(name) or name in current_names:
                continue
            try:
                with _directory(name, parent_fd=base_fd) as skill_fd:
                    if not _regular_file(skill_fd, MANAGED_MARKER):
                        continue
                    if not _regular_file(skill_fd, "SKILL.md", missing_ok=True):
                        continue
                    extra_files = set(os.listdir(skill_fd)) - {"SKILL.md", MANAGED_MARKER}
                    leaves = ("SKILL.md",) if extra_files else ("SKILL.md", MANAGED_MARKER)
                    for leaf in leaves:
                        try:
                            # unlink never follows a leaf replaced by a symlink.
                            os.unlink(leaf, dir_fd=skill_fd)
                        except FileNotFoundError:
                            pass
                    if extra_files:
                        # Retain ownership so rebinding can restore SKILL.md
                        # without adopting or deleting the user's sidecars.
                        logger.info("Removed stale Skill Center instructions: %s", name)
                        continue
                try:
                    os.rmdir(name, dir_fd=base_fd)
                except OSError as exc:
                    if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                        raise
                    # Additional user files are not owned by this integration.
                logger.info("Removed stale Skill Center entry: %s", name)
            except OSError as exc:
                logger.warning("Skipping stale Skill Center entry %s: %s", name, exc)


def update_text(path: str, transform: Callable[[str], str]) -> None:
    target = Path(path)
    with _directory(str(target.parent)) as directory_fd:
        if not _regular_file(directory_fd, target.name, missing_ok=True):
            raise OSError(errno.EINVAL, "Refusing non-regular workspace file", target.name)
        try:
            fd = os.open(
                target.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            existing = ""
        else:
            with os.fdopen(fd, encoding="utf-8") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise OSError(errno.EINVAL, "Refusing non-regular workspace file", target.name)
                existing = stream.read()
        updated = transform(existing)
        if updated != existing:
            _replace_text(directory_fd, target.name, updated)
