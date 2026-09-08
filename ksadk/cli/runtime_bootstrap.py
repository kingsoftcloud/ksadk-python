"""Shared RuntimeAdapter-first application composition for CLI entrypoints."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from ksadk.agui.config import agui_config_for_detection
from ksadk.runtime import RuntimeExecutor, RuntimeLaunchContext
from ksadk.runtime.factory import build_default_runtime_registry
from ksadk.server.composition import configure_runtime_app
from ksadk.server.factory import RuntimeAppConfig, create_runtime_app


def _managed_a2a_card() -> Any:
    """Build the managed discovery-only card when the runtime opts in.

    Import is deferred so the A2A package is only loaded when
    ``KSADK_A2A_RUNTIME_ID`` is configured.
    """
    if not os.getenv("KSADK_A2A_RUNTIME_ID", "").strip():
        return None
    from ksadk.managed_a2a_card import build_managed_a2a_card_if_configured

    return build_managed_a2a_card_if_configured()


def create_runtime_web_app(detection: Any, agent_path: Path) -> FastAPI:
    """Compose one detected project around the canonical RuntimeExecutor."""

    config = dict(getattr(detection, "raw_config", None) or {})
    if os.getenv("AGENTENGINE_MANAGED_RUNTIME") == "1" and any(
        b.get("enabled", True) for b in config.get("plugins", [])
    ):
        import hashlib
        import json

        from ksadk.builders.managed_runtime_builder import serialize_managed_runtime_manifest

        launch = json.loads((agent_path / ".agentkit/cloud-plugin-launch.json").read_text())
        if (
            launch["manifest_sha256"]
            != hashlib.sha256(serialize_managed_runtime_manifest(config)).hexdigest()
        ):
            raise ValueError("Restored plugin launch does not match the runtime declaration")
        config.update(launch["config"])
    context = RuntimeLaunchContext(
        runtime_type=str(detection.type.value),
        project_dir=agent_path,
        detection=detection,
        config=config,
    )
    return create_runtime_app(
        RuntimeAppConfig(
            runtime_executor=RuntimeExecutor(build_default_runtime_registry()),
            launch_context=context,
            agui=agui_config_for_detection(detection),
            a2a=_managed_a2a_card(),
        ),
        configure_runtime_app,
    )


__all__ = ["create_runtime_web_app"]
