"""Bounded post-write observations owned by the Studio tool host.

This first provider parses Python only. It never executes workspace code,
imports a project, starts a language server, or establishes task acceptance.
The frozen profile is part of the ToolDefinition identity, not model policy.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import stat
import sys
import warnings
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field

from ksadk.harness.native.model import ModelTextContent
from ksadk.harness.native.state import Contract

WORKSPACE_WRITERS = frozenset(
    {
        "write_workspace_file",
        "write_workspace_files",
        "edit_workspace_file",
        "multi_edit_workspace_file",
    }
)
MAX_FILES = 8
MAX_FILE_BYTES = 128 * 1024
MAX_TOTAL_BYTES = 512 * 1024


def feedback_profile_digest() -> str:
    payload = {
        "version": "workspace-diagnostics/v1",
        "provider": "python_ast_parse",
        "python_version": list(sys.version_info[:3]),
        "max_files": MAX_FILES,
        "max_file_bytes": MAX_FILE_BYTES,
        "max_total_bytes": MAX_TOTAL_BYTES,
        "symlinks": "deny",
        "acceptance": "not_evaluated",
    }
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


class FileDiagnostic(Contract):
    path: str = Field(min_length=1, max_length=512)
    status: Literal["passed", "error", "not_run"]
    code: Literal[
        "python_parse_passed",
        "python_syntax_error",
        "unsupported_file_type",
        "path_rejected",
        "file_unavailable",
        "byte_budget_exceeded",
        "parser_resource_limit",
    ]
    content_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    line: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)


class WorkspaceDiagnostics(Contract):
    version: Literal["workspace-diagnostics/v1"] = "workspace-diagnostics/v1"
    scope: Literal["python_parse_only"] = "python_parse_only"
    acceptance: Literal["not_evaluated"] = "not_evaluated"
    profile_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    files: tuple[FileDiagnostic, ...] = Field(max_length=MAX_FILES)
    omitted_file_count: int = Field(ge=0)


def _read_file(root: Path, relative: PurePosixPath, limit: int) -> bytes | None:
    """Open under the authorized root, refusing symlinks and special files.

    Directory descriptors keep component replacement from redirecting the read
    outside that root. Nonblocking open avoids waiting on a substituted FIFO.
    None means the bounded read did not establish a complete file observation.
    """
    if not all(hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")):
        raise OSError("diagnostic reader is unavailable on this platform")
    if os.open not in os.supports_dir_fd:
        raise OSError("diagnostic reader needs directory descriptor support")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in relative.parts[:-1]:
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise OSError("diagnostic target is not a regular file")
            if info.st_size > limit:
                return None
            data = source.read(limit + 1)
            return data if len(data) <= limit else None
    finally:
        os.close(directory)


def observe_workspace_writes(*, root: Path, paths: tuple[str, ...]) -> ModelTextContent:
    files = []
    remaining_bytes = MAX_TOTAL_BYTES
    for path in paths[:MAX_FILES]:
        # SDK result paths are untrusted file names. Never return source lines
        # or parser exception messages; either can contain private file bytes.
        if not path or len(path) > 512:
            continue
        relative = PurePosixPath(path)
        report = {"path": path, "status": "not_run"}
        if relative.is_absolute() or ".." in relative.parts or "\\" in path or "\x00" in path:
            files.append(FileDiagnostic(**report, code="path_rejected"))
            continue
        if relative.suffix != ".py":
            files.append(FileDiagnostic(**report, code="unsupported_file_type"))
            continue
        if remaining_bytes == 0:
            files.append(FileDiagnostic(**report, code="byte_budget_exceeded"))
            continue
        try:
            data = _read_file(
                root.resolve(strict=True), relative, min(MAX_FILE_BYTES, remaining_bytes)
            )
        except (OSError, RuntimeError, ValueError):
            files.append(FileDiagnostic(**report, code="file_unavailable"))
            continue
        if data is None:
            files.append(FileDiagnostic(**report, code="byte_budget_exceeded"))
            continue
        remaining_bytes -= len(data)
        content_digest = "sha256:" + hashlib.sha256(data).hexdigest()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                ast.parse(data, filename=path)
        except SyntaxError as exc:
            files.append(
                FileDiagnostic(
                    path=path,
                    status="error",
                    code="python_syntax_error",
                    content_digest=content_digest,
                    line=max(1, exc.lineno or 1),
                    column=max(1, exc.offset or 1),
                )
            )
        except (ValueError, RecursionError, MemoryError):
            files.append(
                FileDiagnostic(
                    **report,
                    code="parser_resource_limit",
                    content_digest=content_digest,
                )
            )
        else:
            files.append(
                FileDiagnostic(
                    path=path,
                    status="passed",
                    code="python_parse_passed",
                    content_digest=content_digest,
                )
            )
    report = WorkspaceDiagnostics(
        profile_digest=feedback_profile_digest(),
        files=tuple(files),
        omitted_file_count=len(paths) - len(files),
    )
    return ModelTextContent(text=report.model_dump_json(exclude_none=True))
