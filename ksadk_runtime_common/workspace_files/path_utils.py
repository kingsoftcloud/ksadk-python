"""Workspace files path utilities and security helpers."""

from __future__ import annotations

import os
import posixpath
import re
from collections.abc import Callable
from pathlib import Path

from fastapi import HTTPException

from ksadk_runtime_common.workspace_files.constants import WORKSPACE_PATH_ESCAPE_DETAIL

_SAFE_WORKSPACE_SEGMENT_RE = re.compile(r"^[^/\x00]+$")


def _env_flag(name: str, default: bool) -> bool:
    """Parse boolean environment variable."""
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _normalize_workspace_path(raw_path: str | None, *, allow_root: bool) -> str:
    """Normalize and validate a workspace path."""
    raw = str(raw_path or ".").strip().replace("\\", "/")
    if raw in {"", "."}:
        if allow_root:
            return "."
        raise HTTPException(status_code=400, detail="workspace file path must not be empty")
    if raw.startswith("/"):
        raise HTTPException(status_code=400, detail=WORKSPACE_PATH_ESCAPE_DETAIL)

    normalized = posixpath.normpath(raw)
    if normalized in {"", "."}:
        if allow_root:
            return "."
        raise HTTPException(status_code=400, detail="workspace file path must not be empty")
    if normalized == ".." or normalized.startswith("../"):
        raise HTTPException(status_code=400, detail=WORKSPACE_PATH_ESCAPE_DETAIL)
    return normalized


def _resolve_workspace_root(root_getter: Callable[[], Path]) -> Path:
    """Resolve and create the workspace root directory."""
    root = Path(root_getter()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _symlink_allowlist(root: Path) -> tuple[Path, ...]:
    """Load allowed symlink target prefixes from ``<root>/.symlink-allowlist``.

    One absolute path prefix per line (``#`` comments and blank lines ignored).
    The file lives inside the workspace root itself, so whoever can write the
    workspace decides which outside targets its symlinks may point at — the
    escape check stays authoritative for everything else.
    """
    entries: list[Path] = []
    allow_file = root / ".symlink-allowlist"
    try:
        for line in allow_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            candidate = Path(line)
            if candidate.is_absolute():
                # Normalize the allowlist prefix the same way targets are
                # resolved, so symlinked parent dirs (e.g. /var on macOS)
                # cannot break prefix matching.
                entries.append(candidate.resolve(strict=False))
    except OSError:
        return ()
    return tuple(entries)


def _target_in_allowlist(resolved_target: Path, allowlist: tuple[Path, ...]) -> bool:
    return any(
        resolved_target == allowed
        or resolved_target.is_relative_to(allowed)
        for allowed in allowlist
    )


def _resolve_workspace_target(
    root: Path, raw_path: str | None, *, allow_root: bool
) -> tuple[str, Path]:
    """Resolve a path within the workspace root with an escape check."""
    normalized = _normalize_workspace_path(raw_path, allow_root=allow_root)
    if normalized == ".":
        target = root
    else:
        segments = tuple(part for part in normalized.split("/") if part)
        if not segments or any(
            part in {".", ".."} or not _SAFE_WORKSPACE_SEGMENT_RE.fullmatch(part)
            for part in segments
        ):
            raise HTTPException(status_code=400, detail=WORKSPACE_PATH_ESCAPE_DETAIL)
        target = root.joinpath(*segments)
    resolved_target = target.resolve(strict=False)
    if resolved_target != root and root not in resolved_target.parents:
        # Symlinks legitimately point outside the workspace (e.g. workspace
        # convenience links to the profile's config.yaml/.env). Allow them only
        # when the link target is registered in the workspace's own allowlist.
        if target.is_symlink() and _target_in_allowlist(
            resolved_target, _symlink_allowlist(root)
        ):
            return normalized, resolved_target
        raise HTTPException(status_code=400, detail=WORKSPACE_PATH_ESCAPE_DETAIL)
    return normalized, resolved_target
