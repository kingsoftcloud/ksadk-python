"""Ksmem memory backend renderer for OpenClaw."""

from __future__ import annotations

import os
from typing import Any

from ksadk_runtime_common.memory_backend.manifest import MemoryBackendManifest
from ksadk_runtime_common.memory_backend.registry import RenderResult


class KsmemProvider:
    """Render the OpenClaw adapter that delegates storage to KsADK."""

    def render(self, manifest: MemoryBackendManifest) -> RenderResult:
        config = manifest.config
        if not str(config.get("ksmem_instance_id") or "").strip():
            raise ValueError("ksmem backend requires 'ksmem_instance_id' in config")

        memory_id_env = str(
            manifest.secrets_env.get("memory_id") or "KSMEM_MEMORY_ID"
        ).strip() or "KSMEM_MEMORY_ID"
        if not str(os.environ.get(memory_id_env) or "").strip():
            raise ValueError(
                f"ksmem backend requires environment variable '{memory_id_env}'"
            )

        plugin_config: dict[str, Any] = {
            "memoryIdEnv": memory_id_env,
            "autoRecall": True,
            "autoCapture": True,
            "topK": 5,
        }
        for source, target in (
            ("auto_recall", "autoRecall"),
            ("auto_capture", "autoCapture"),
            ("top_k", "topK"),
            ("user_id", "userId"),
        ):
            if source in config:
                plugin_config[target] = config[source]

        return RenderResult(
            backend_type="ksmem",
            config_patch={
                "plugins": {
                    "slots": {"memory": "openclaw-ksmem"},
                    "entries": {
                        "openclaw-ksmem": {
                            "enabled": True,
                            "hooks": {"allowConversationAccess": True},
                            "config": plugin_config,
                        }
                    },
                }
            },
            required_env=[memory_id_env],
            plugin_ids=["openclaw-ksmem"],
            disabled_plugin_ids=["openclaw-mem0", "memory-lancedb"],
        )
