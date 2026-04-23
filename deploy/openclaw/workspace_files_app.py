from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI

from ksadk_runtime_common.workspace_files import (
    create_workspace_files_router,
    workspace_files_enabled,
)


def _workspace_root() -> Path:
    configured = (
        os.getenv("KSADK_WORKSPACE_ROOT")
        or os.getenv("OPENCLAW_WORKSPACE_DIR")
        or "/home/node/.openclaw/workspace"
    )
    return Path(configured).expanduser().resolve()


app = FastAPI(title="OpenClaw Workspace Files", version="1.0.0")
app.include_router(
    create_workspace_files_router(
        root_getter=_workspace_root,
        enabled_getter=lambda: workspace_files_enabled(default=True),
    )
)
