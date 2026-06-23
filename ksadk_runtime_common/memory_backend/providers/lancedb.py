"""LanceDB memory backend provider."""

from __future__ import annotations

import os
from typing import Any

from ksadk_runtime_common.memory_backend.manifest import MemoryBackendManifest
from ksadk_runtime_common.memory_backend.registry import RenderResult


def _resolve_env_name(secrets_env: dict[str, str], key: str, default: str) -> str:
    """Resolve an env var name from secrets_env, falling back to a default."""
    return str(secrets_env.get(key) or default).strip() or default


class LanceDBProvider:
    """Provider for the LanceDB memory backend."""

    def render(self, manifest: MemoryBackendManifest) -> RenderResult:
        """Render lancedb config for OpenClaw."""
        config = manifest.config
        secrets_env = manifest.secrets_env

        embedding_model = config.get("embedding_model")
        if not embedding_model:
            raise ValueError("lancedb backend requires 'embedding_model' in config")

        database_uri = config.get("database_uri")
        if not database_uri:
            raise ValueError("lancedb backend requires 'database_uri' in config")

        embedding_provider = config.get("embedding_provider", "ollama")
        embedding_base_url = config.get("embedding_base_url", "http://127.0.0.1:11434/v1")
        embedding_dimensions = config.get("embedding_dimensions")

        # Resolve secrets from environment
        embedding_api_key_env = _resolve_env_name(secrets_env, "embedding_api_key", "LANCEDB_EMBEDDING_API_KEY")
        access_key_env = _resolve_env_name(secrets_env, "storage_access_key_id", "LANCEDB_ACCESS_KEY_ID")
        secret_key_env = _resolve_env_name(secrets_env, "storage_secret_access_key", "LANCEDB_SECRET_ACCESS_KEY")
        session_token_env = _resolve_env_name(secrets_env, "storage_session_token", "AWS_SESSION_TOKEN")

        required_env = []
        embedding_api_key = str(os.getenv(embedding_api_key_env) or "").strip()
        access_key = str(os.getenv(access_key_env) or "").strip()
        secret_key = str(os.getenv(secret_key_env) or "").strip()
        session_token = str(os.getenv(session_token_env) or "").strip()

        if embedding_api_key:
            required_env.append(embedding_api_key_env)
        if access_key:
            required_env.append(access_key_env)
        if secret_key:
            required_env.append(secret_key_env)
        if session_token:
            required_env.append(session_token_env)

        # Build plugin config matching @openclaw/memory-lancedb schema
        # Top-level keys: dbPath, embedding (object), storageOptions?
        embedding_obj: dict[str, Any] = {
            "provider": embedding_provider,
            "model": embedding_model,
        }
        if embedding_api_key:
            embedding_obj["apiKey"] = embedding_api_key
        if embedding_base_url:
            embedding_obj["baseUrl"] = embedding_base_url
        if embedding_dimensions is not None:
            embedding_obj["dimensions"] = embedding_dimensions

        plugin_config: dict[str, Any] = {
            "dbPath": database_uri,
            "embedding": embedding_obj,
        }

        # S3/MinIO storage options (optional)
        storage_options: dict[str, Any] = {}
        if access_key:
            storage_options["accessKeyId"] = access_key
        if secret_key:
            storage_options["secretAccessKey"] = secret_key
        storage_endpoint = config.get("storage_endpoint")
        if storage_endpoint:
            storage_options["endpoint"] = storage_endpoint
        storage_region = config.get("storage_region")
        if storage_region:
            storage_options["region"] = storage_region
        storage_allow_http = config.get("storage_allow_http", "true")
        storage_options["allowHttp"] = storage_allow_http

        if storage_options:
            plugin_config["storageOptions"] = storage_options

        config_patch: dict[str, Any] = {
            "plugins": {
                "slots": {
                    "memory": "memory-lancedb",
                },
                "entries": {
                    "memory-lancedb": {
                        "enabled": True,
                        "config": plugin_config,
                    },
                },
            },
        }

        return RenderResult(
            backend_type="lancedb",
            config_patch=config_patch,
            required_env=required_env,
            plugin_ids=["memory-lancedb"],
        )
