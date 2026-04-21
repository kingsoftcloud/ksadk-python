"""Mem0 memory backend provider."""

from __future__ import annotations

import os
from typing import Any

from ksadk_runtime_common.memory_backend.manifest import MemoryBackendManifest
from ksadk_runtime_common.memory_backend.registry import RenderResult


def _resolve_env_name(secrets_env: dict[str, str], key: str, default: str) -> str:
    """Resolve an env var name from secrets_env, falling back to a default."""
    return str(secrets_env.get(key) or default).strip() or default


def _require_env(name: str) -> None:
    """Require a non-empty environment variable in the current process."""
    if not str(os.getenv(name) or "").strip():
        raise ValueError(f"mem0 backend requires environment variable '{name}'")


class Mem0Provider:
    """Provider for the mem0 memory backend."""

    def render(self, manifest: MemoryBackendManifest) -> RenderResult:
        """Render mem0 config for OpenClaw."""
        config = manifest.config
        secrets_env = manifest.secrets_env

        mem0_instance_id = config.get("mem0_instance_id")
        if not mem0_instance_id:
            raise ValueError("mem0 backend requires 'mem0_instance_id' in config")

        api_key_env = _resolve_env_name(secrets_env, "api_key", "MEM0_API_KEY")
        memory_id_env = _resolve_env_name(secrets_env, "memory_id", "MEM0_MEMORY_ID")

        required_env = [api_key_env, memory_id_env]
        for env_name in required_env:
            _require_env(env_name)

        config_patch: dict[str, Any] = {
            "memory": {
                "provider": "mem0",
                "config": {
                    "memoryId": mem0_instance_id,
                },
            },
        }

        if "mem0_region" in config:
            config_patch["memory"]["config"]["region"] = config["mem0_region"]

        return RenderResult(
            backend_type="mem0",
            config_patch=config_patch,
            required_env=required_env,
        )
