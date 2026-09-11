"""Read one bounded artifact through non-following directory file descriptors."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from .errors import TeamsError

MAX_ARTIFACT_BYTES = 20 * 1024 * 1024


def read_workspace_artifact(root: Path, supplied_path: str) -> tuple[str, bytes]:
    root = root.resolve()
    path = Path(supplied_path)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError as error:
            raise TeamsError(
                "artifact_path_forbidden", "文件不属于本次工作区", status=403
            ) from error
    if not path.parts or any(part in {"..", ""} for part in path.parts):
        raise TeamsError("artifact_path_forbidden", "文件不属于本次工作区", status=403)
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        raise TeamsError(
            "artifact_platform_unsupported", "当前平台缺少受控产物读取能力", status=422
        )
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        filename = re.fullmatch(r"[^/\\\x00]+", path.name)
        if filename is None:
            raise TeamsError("artifact_path_forbidden", "文件名包含不允许的字符", status=403)
        descriptor = os.open(
            filename.group(0),  # lgtm[py/path-injection]
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory,
        )
        with os.fdopen(descriptor, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise TeamsError("artifact_path_forbidden", "产物必须是普通文件", status=403)
            if info.st_size > MAX_ARTIFACT_BYTES:
                raise TeamsError("artifact_too_large", "产物超过20MiB限制", status=422)
            data = file.read(MAX_ARTIFACT_BYTES + 1)
            if len(data) > MAX_ARTIFACT_BYTES:
                raise TeamsError("artifact_too_large", "产物超过20MiB限制", status=422)
            return path.name, data
    except OSError as error:
        raise TeamsError(
            "artifact_path_forbidden", "文件不存在或包含不允许的符号链接", status=403
        ) from error
    finally:
        os.close(directory)
