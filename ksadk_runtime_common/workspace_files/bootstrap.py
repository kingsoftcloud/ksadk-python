"""Workspace files bootstrap configuration builder."""

from __future__ import annotations

import os

from ksadk_runtime_common.workspace_files.constants import (
    DEFAULT_WORKSPACE_MAX_UPLOAD_BYTES,
    WORKSPACE_CONTENT_PATH,
    WORKSPACE_ENTRY_ACTION,
    WORKSPACE_UPLOAD_ACTION,
)


def _env_flag(name: str, default: bool) -> bool:
    """Parse boolean environment variable."""
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def workspace_files_enabled(*, default: bool = True) -> bool:
    """Check if workspace files feature is enabled."""
    return _env_flag("KSADK_WORKSPACE_FILES_ENABLED", default)


def workspace_files_root_label() -> str:
    """Get workspace root label for UI display."""
    return str(os.getenv("KSADK_WORKSPACE_ROOT_LABEL") or "workspace").strip() or "workspace"


def workspace_files_max_upload_bytes() -> int:
    """Get max upload size in bytes."""
    raw = str(os.getenv("KSADK_WORKSPACE_MAX_UPLOAD_BYTES") or "").strip()
    if not raw:
        return DEFAULT_WORKSPACE_MAX_UPLOAD_BYTES
    try:
        return max(int(raw), 1)
    except ValueError:
        return DEFAULT_WORKSPACE_MAX_UPLOAD_BYTES


def build_workspace_files_bootstrap(*, enabled: bool) -> dict[str, bool | int | str] | None:
    """Build bootstrap configuration for workspace files."""
    if not enabled:
        return None
    return {
        "Enabled": True,
        "MaxUploadBytes": workspace_files_max_upload_bytes(),
        "SupportsDelete": True,
        "RootLabel": workspace_files_root_label(),
        "EntryAction": WORKSPACE_ENTRY_ACTION,
        "UploadAction": WORKSPACE_UPLOAD_ACTION,
        "ContentPath": WORKSPACE_CONTENT_PATH,
    }
