"""Fingerprint the installed DSH dependency tree without importing plugin code."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path


class DshInstallationDigestError(ValueError):
    pass


def installation_digest(
    root: Path, *, max_files: int = 100000, max_bytes: int = 512 * 1024 * 1024
) -> str:
    """Include installed bytes, execute bits and in-tree link targets.

    External links cannot establish a closed Build input and are rejected. The
    caller holds the profile lifecycle lock; this is not same-user OS isolation.
    """
    if type(max_files) is not int or type(max_bytes) is not int or min(max_files, max_bytes) < 1:
        raise DshInstallationDigestError("DSH digest limits must be positive integers")
    if root.is_symlink() or not root.is_dir():
        raise DshInstallationDigestError("DSH installation root must be a real directory")
    root = root.resolve()
    digest = hashlib.sha256()
    count = total = 0

    def failed_walk(error: OSError) -> None:
        raise DshInstallationDigestError("DSH installed tree cannot be read") from error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=failed_walk):
        dirs.sort()
        files.sort()
        for name in sorted(dirs + files):
            path = Path(directory) / name
            count += 1
            if count > max_files:
                raise DshInstallationDigestError("DSH installation exceeds entry limit")
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            mode = stat.S_IFMT(info.st_mode)
            digest.update(relative.encode() + b"\0")
            if mode == stat.S_IFLNK:
                try:
                    target = path.resolve(strict=True).relative_to(root).as_posix()
                except (OSError, ValueError, RuntimeError):
                    raise DshInstallationDigestError(
                        "DSH dependency link escapes the installed tree"
                    ) from None
                digest.update(b"link\0" + target.encode() + b"\0")
            elif mode == stat.S_IFDIR:
                digest.update(b"dir\0")
            elif mode == stat.S_IFREG:
                digest.update(b"file\0" + str(info.st_mode & 0o111).encode() + b"\0")
                content_digest = hashlib.sha256()
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise DshInstallationDigestError("DSH dependency is not a regular file")
                    while chunk := stream.read(65536):
                        total += len(chunk)
                        if total > max_bytes:
                            raise DshInstallationDigestError("DSH installation exceeds byte limit")
                        content_digest.update(chunk)
                digest.update(content_digest.digest())
            else:
                raise DshInstallationDigestError("DSH installation contains a special file")
    return "sha256:" + digest.hexdigest()
