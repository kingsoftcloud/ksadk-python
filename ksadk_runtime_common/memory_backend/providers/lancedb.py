"""LanceDB memory backend provider."""

from __future__ import annotations

from typing import Any

from ksadk_runtime_common.memory_backend.manifest import MemoryBackendManifest
from ksadk_runtime_common.memory_backend.registry import RenderResult


class LanceDBProvider:
    """Provider for the in-process LanceDB OpenClaw memory plugin."""

    def render(self, manifest: MemoryBackendManifest) -> RenderResult:
        """Render LanceDB plugin config for OpenClaw."""
        entry: dict[str, Any] = {"enabled": True}
        if manifest.config:
            entry["config"] = dict(manifest.config)

        return RenderResult(
            backend_type="lancedb",
            config_patch={
                "plugins": {
                    "slots": {
                        "memory": "memory-lancedb",
                    },
                    "entries": {
                        "memory-lancedb": entry,
                    },
                },
            },
            plugin_ids=["memory-lancedb"],
            disabled_plugin_ids=["openclaw-mem0"],
        )
