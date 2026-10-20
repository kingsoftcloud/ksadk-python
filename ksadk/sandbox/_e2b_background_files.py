"""Private, crash-safe file operations for durable E2B command records."""

from __future__ import annotations

import codecs
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from ksadk.sandbox.base import SandboxError


def _private_directory(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink():
            raise SandboxError("Background command state cannot follow symbolic links")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _open_private(path: Path, flags: int) -> int:
    return os.open(path, flags | os.O_NOFOLLOW, 0o600)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}")
    try:
        with os.fdopen(_open_private(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "w") as out:
            json.dump(value, out, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _json(path: Path) -> dict[str, Any]:
    with os.fdopen(_open_private(path, os.O_RDONLY), "r") as source:
        result = json.load(source)
    if not isinstance(result, dict):
        raise SandboxError("Invalid background command record")
    return result


def _read_window(path: Path, cursor: int, max_bytes: int) -> tuple[str, int]:
    if type(cursor) is not int or cursor < 0:
        raise SandboxError("Command output cursor must be a non-negative byte offset")
    with os.fdopen(_open_private(path, os.O_RDONLY), "rb") as source:
        size = os.fstat(source.fileno()).st_size
        if cursor > size:
            raise SandboxError("Command output cursor exceeds retained output")
        source.seek(cursor)
        data = source.read(max_bytes)
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        text = decoder.decode(data, final=False)
    except UnicodeDecodeError as exc:
        raise SandboxError("Command output cursor is not a UTF-8 boundary") from exc
    pending, _ = decoder.getstate()
    return text, cursor + len(data) - len(pending)


def _read_window_all(path: Path) -> tuple[str, int]:
    with os.fdopen(_open_private(path, os.O_RDONLY), "rb") as source:
        data = source.read()
    return data.decode("utf-8"), len(data)


def _append_output(path: Path, text: str) -> None:
    with os.fdopen(_open_private(path, os.O_WRONLY | os.O_APPEND), "ab") as out:
        out.write(text.encode("utf-8"))


def _finalize_output_from_log(path: Path) -> None:
    """Atomically replace the append log with itself, making the retained
    output the canonical record for cursor stability. The content is unchanged;
    only the in-place append log is committed as the durable full output."""
    with os.fdopen(_open_private(path, os.O_RDONLY), "rb") as source:
        data = source.read()
    temporary = path.with_name(f".{path.name}.{uuid4().hex}")
    with os.fdopen(_open_private(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)
