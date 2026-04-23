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


def _require_env_value(name: str) -> str:
    """Return a required env var value after validating it is non-empty."""
    _require_env(name)
    return str(os.getenv(name) or "").strip()


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
        user_id_env = _resolve_env_name(secrets_env, "user_id", "MEM0_USER_ID")
        base_url_env = _resolve_env_name(secrets_env, "base_url", "MEM0_BASE_URL")

        required_env = [api_key_env, user_id_env, base_url_env]
        api_key = _require_env_value(api_key_env)
        user_id = _require_env_value(user_id_env)
        base_url = _require_env_value(base_url_env)

        config_patch: dict[str, Any] = {
            "plugins": {
                "slots": {
                    "memory": "openclaw-mem0",
                },
                "entries": {
                    "openclaw-mem0": {
                        "enabled": True,
                        "config": {
                            "mode": "platform",
                            "apiKey": api_key,
                            "baseUrl": base_url,
                            "userId": user_id,
                        },
                    },
                },
            },
        }

        return RenderResult(
            backend_type="mem0",
            config_patch=config_patch,
            required_env=required_env,
            plugin_ids=["openclaw-mem0"],
        )
