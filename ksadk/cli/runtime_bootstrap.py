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
    """Build discovery first, then the full composition after platform readiness.

    ``KSADK_A2A_RUNTIME_ID`` is available before registration so the control
    plane can fetch the discovery card.  Operator sets the separate bootstrap
    marker only after the complete managed projection and Secret mounts exist.
    """
    if not os.getenv("KSADK_A2A_RUNTIME_ID", "").strip():
        return None
    if os.getenv("KSADK_A2A_MANAGED_BOOTSTRAP_ENABLED", "").strip().lower() == "true":
        from ksadk.a2a.platform import build_managed_a2a_bootstrap_from_env

        return build_managed_a2a_bootstrap_from_env()
    from ksadk.managed_a2a_card import build_managed_a2a_card_if_configured

    return build_managed_a2a_card_if_configured()


def create_runtime_web_app(detection: Any, agent_path: Path) -> FastAPI:
    """Compose one detected project around the canonical RuntimeExecutor."""

    config = dict(getattr(detection, "raw_config", None) or {})
    managed_runtime = os.getenv("AGENTENGINE_MANAGED_RUNTIME") == "1"
    if managed_runtime:
        from ksadk.resource_runtime.managed_projection import native_codex_plugin_bindings

    if managed_runtime and native_codex_plugin_bindings(config):
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
    if managed_runtime:
        from ksadk.resource_runtime.managed_projection import apply_managed_platform_resources

        apply_managed_platform_resources(config)
    if str(detection.type.value) == "codex":
        from ksadk.plugins.providers.codex import _bundle_a2a_experts
        experts = _bundle_a2a_experts(config)
        if "a2a_experts" in config and config["a2a_experts"] != experts:
            raise ValueError("A2A_BINDING_MANIFEST_CONFLICT")
        if experts:
            config["a2a_experts"] = experts
    context = RuntimeLaunchContext(
        runtime_type=str(detection.type.value),
        project_dir=agent_path,
        detection=detection,
        config=config,
    )
    managed_a2a = _managed_a2a_card()
    executor = RuntimeExecutor(build_default_runtime_registry())
    adapter = None
    if (
        managed_a2a is not None
        and os.getenv("KSADK_A2A_MANAGED_BOOTSTRAP_ENABLED", "").strip().lower() == "true"
    ):
        executor.configure_managed_a2a(managed_a2a)
        adapter = executor.create_adapter(context)
    return create_runtime_app(
        RuntimeAppConfig(
            runtime_executor=executor,
            launch_context=context,
            agui=agui_config_for_detection(detection),
            a2a=managed_a2a,
            a2a_runtime_adapter=adapter,
        ),
        configure_runtime_app,
    )


__all__ = ["create_runtime_web_app"]
