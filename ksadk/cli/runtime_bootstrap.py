"""Shared RuntimeAdapter-first application composition for CLI entrypoints."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI

from ksadk.runtime import RuntimeExecutor, RuntimeLaunchContext
from ksadk.runtime.factory import build_default_runtime_registry
from ksadk.server.composition import configure_runtime_app
from ksadk.server.factory import RuntimeAppConfig, create_runtime_app


def create_runtime_web_app(detection: Any, agent_path: Path) -> FastAPI:
    """Compose one detected project around the canonical RuntimeExecutor."""

    context = RuntimeLaunchContext(
        runtime_type=str(detection.type.value),
        project_dir=agent_path,
        detection=detection,
        config=dict(getattr(detection, "raw_config", None) or {}),
    )
    return create_runtime_app(
        RuntimeAppConfig(
            runtime_executor=RuntimeExecutor(build_default_runtime_registry()),
            launch_context=context,
        ),
        configure_runtime_app,
    )


__all__ = ["create_runtime_web_app"]
