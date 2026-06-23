"""Memory backend config renderer for runtime bootstrap integration."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

from ksadk_runtime_common.memory_backend.manifest import (
    MemoryBackendManifest,
    parse_manifest,
)
from ksadk_runtime_common.memory_backend.registry import (
    RenderResult,
    get_provider,
)

MANIFEST_ENV_VAR = "MEMORY_BACKEND_MANIFEST"
MEMORY_BACKEND_PLUGIN_IDS = ["openclaw-mem0", "memory-lancedb"]
MEMORY_BACKEND_PLUGIN_SLOTS = ["memory"]


def render_memory_backend_config(
    manifest: MemoryBackendManifest | str | Mapping[str, Any] | None = None,
) -> RenderResult:
    """Render memory backend config from a manifest."""
    if manifest is None:
        manifest_raw = os.getenv(MANIFEST_ENV_VAR)
        if not manifest_raw:
            return RenderResult(
                backend_type="openclaw_default",
                config_patch={},
                required_env=[],
                plugin_ids=[],
            )
        manifest = manifest_raw

    parsed = parse_manifest(manifest)
    if parsed is None:
        return RenderResult(
            backend_type="openclaw_default",
            config_patch={},
            required_env=[],
            plugin_ids=[],
        )

    if parsed.backend_type == "openclaw_default":
        return RenderResult(
            backend_type="openclaw_default",
            config_patch={},
            required_env=[],
            disabled_plugin_ids=MEMORY_BACKEND_PLUGIN_IDS,
            clear_plugin_slots=MEMORY_BACKEND_PLUGIN_SLOTS,
        )

    provider = get_provider(parsed.backend_type)
    if provider is None:
        raise ValueError(f"Unknown backend_type: {parsed.backend_type}")

    return provider.render(parsed)


def render_to_json(manifest: MemoryBackendManifest | str | dict[str, Any] | None = None) -> str:
    """Render memory backend config to a JSON string."""
    result = render_memory_backend_config(manifest)
    return json.dumps(result.model_dump())


if __name__ == "__main__":
    print(render_to_json())
