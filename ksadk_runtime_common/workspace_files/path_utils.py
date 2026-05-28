"""Workspace files path utilities and security helpers."""

from __future__ import annotations

import os
import posixpath
from collections.abc import Callable
from pathlib import Path

from fastapi import HTTPException

from ksadk_runtime_common.workspace_files.constants import WORKSPACE_PATH_ESCAPE_DETAIL


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


def _resolve_workspace_target(
    root: Path, raw_path: str | None, *, allow_root: bool
) -> tuple[str, Path]:
    """Resolve a path within the workspace root with an escape check."""
    normalized = _normalize_workspace_path(raw_path, allow_root=allow_root)
    target = root if normalized == "." else (root / Path(normalized))
    resolved_target = target.resolve(strict=False)
    if resolved_target != root and root not in resolved_target.parents:
        raise HTTPException(status_code=400, detail=WORKSPACE_PATH_ESCAPE_DETAIL)
    return normalized, resolved_target
