"""Runtime-safe helpers for reading local AgentEngine state."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


STATE_FILE_NAME = ".agentengine.state"


def load_state(project_dir: Path) -> dict[str, Any]:
    """Load .agentengine.state without importing deployment-only modules."""

    state_file = Path(project_dir) / STATE_FILE_NAME
    if not state_file.exists():
        return {}

    try:
        with state_file.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except Exception:
        return {}
