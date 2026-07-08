"""LanceDB memory backend provider."""

from __future__ import annotations

from typing import Any

from ksadk_runtime_common.memory_backend.manifest import MemoryBackendManifest
from ksadk_runtime_common.memory_backend.registry import RenderResult


class LanceDBProvider:
    """Provider for the in-process LanceDB OpenClaw memory plugin."""

    def render(self, manifest: MemoryBackendManifest) -> RenderResult:
        """Render LanceDB plugin config for OpenClaw.

        Maps the platform manifest config fields onto the memory-lancedb
        plugin schema. This must stay in sync with the inline renderer in
        ``deploy/openclaw/bootstrap.sh`` so both code paths produce the same
        plugin entry.
        """
        entry: dict[str, Any] = {"enabled": True}
        config = self._render_plugin_config(manifest.config or {})
        if config:
            entry["config"] = config

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

    @staticmethod
    def _first(config: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            value = config.get(key)
            if isinstance(value, str):
                value = value.strip()
                if value:
                    return value
            elif value is not None:
                return value
        return None

    @classmethod
    def _render_plugin_config(cls, config: dict[str, Any]) -> dict[str, Any]:
        if not config:
            return {}

        output: dict[str, Any] = {}

        embedding: dict[str, Any] = {}
        for target_key, *source_keys in (
            ("provider", "provider", "embedding_provider"),
            ("model", "model", "embedding_model"),
            ("apiKey", "apiKey", "api_key", "embedding_api_key"),
            ("baseUrl", "baseUrl", "base_url", "embedding_base_url"),
        ):
            value = cls._first(config, *source_keys)
            if value is not None:
                embedding[target_key] = value
        dimensions = cls._first(
            config, "dimensions", "embedding_dimensions"
        )
        if isinstance(dimensions, int) or (
            isinstance(dimensions, str) and dimensions.strip().isdigit()
        ):
            embedding["dimensions"] = int(dimensions)
        if embedding:
            output["embedding"] = embedding

        db_path = cls._first(
            config, "dbPath", "db_path", "data_path",
            "database_uri", "databaseUri", "database_url",
        )
        if db_path is not None:
            output["dbPath"] = db_path

        for target_key, *source_keys in (
            ("autoCapture", "autoCapture", "auto_capture"),
            ("autoRecall", "autoRecall", "auto_recall"),
        ):
            value = cls._first(config, *source_keys)
            if isinstance(value, bool):
                output[target_key] = value
            elif isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in {"1", "true", "yes", "on"}:
                    output[target_key] = True
                elif normalized in {"0", "false", "no", "off"}:
                    output[target_key] = False

        for target_key, *source_keys in (
            ("captureMaxChars", "captureMaxChars", "capture_max_chars"),
            ("recallMaxChars", "recallMaxChars", "recall_max_chars"),
        ):
            value = cls._first(config, *source_keys)
            if isinstance(value, int) and not isinstance(value, bool):
                output[target_key] = value
            elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
                output[target_key] = int(value)

        custom_triggers = config.get("customTriggers", config.get("custom_triggers"))
        if isinstance(custom_triggers, list):
            output["customTriggers"] = custom_triggers

        for target_key, *source_keys in (
            ("dreaming", "dreaming"),
            ("storageOptions", "storageOptions", "storage_options"),
        ):
            value = cls._first(config, *source_keys)
            if isinstance(value, dict):
                output[target_key] = value

        storage_options = output.get("storageOptions")
        if not isinstance(storage_options, dict):
            storage_options = {}
        for target_key, *source_keys in (
            ("endpoint", "storage_endpoint", "storageEndpoint"),
            ("region", "storage_region", "storageRegion"),
            ("bucket", "storage_bucket", "storageBucket"),
            ("prefix", "storage_prefix", "storagePrefix"),
            ("accessKeyId", "storage_access_key_id", "storageAccessKeyId"),
            ("secretAccessKey", "storage_secret_access_key", "storageSecretAccessKey"),
        ):
            value = cls._first(config, *source_keys)
            if value is not None:
                storage_options[target_key] = value
        allow_http = cls._first(config, "storage_allow_http", "storageAllowHttp")
        if isinstance(allow_http, bool):
            storage_options["allowHttp"] = "true" if allow_http else "false"
        elif isinstance(allow_http, str):
            normalized = allow_http.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                storage_options["allowHttp"] = "true"
            elif normalized in {"0", "false", "no", "off"}:
                storage_options["allowHttp"] = "false"
        if storage_options:
            output["storageOptions"] = storage_options

        return output
