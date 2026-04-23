"""Shared runtime components bundled inside ksadk-python."""

__version__ = "0.1.0"

from ksadk_runtime_common.workspace_files.bootstrap import (
    build_workspace_files_bootstrap,
    workspace_files_enabled,
    workspace_files_max_upload_bytes,
    workspace_files_root_label,
)
from ksadk_runtime_common.workspace_files.router import create_workspace_files_router

__all__ = [
    "__version__",
    "create_workspace_files_router",
    "build_workspace_files_bootstrap",
    "workspace_files_enabled",
    "workspace_files_root_label",
    "workspace_files_max_upload_bytes",
]
