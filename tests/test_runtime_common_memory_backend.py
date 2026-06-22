from __future__ import annotations

import importlib

import pytest


VALID_MEM0_UUID = "e52b7fac-e641-4b34-b9f7-6b0b9f190cd4"


def _memory_backend_module():
    return importlib.import_module("ksadk_runtime_common.memory_backend")


def _manifest_module():
    return importlib.import_module("ksadk_runtime_common.memory_backend.manifest")


def test_runtime_common_package_is_importable():
    module = importlib.import_module("ksadk_runtime_common")

    assert hasattr(module, "create_workspace_files_router")
    assert hasattr(module, "workspace_files_enabled")


def test_render_openclaw_default_manifest_returns_empty_patch():
    memory_backend = _memory_backend_module()

    result = memory_backend.render_memory_backend_config(
        {
            "schema_version": "v1",
            "backend_type": "openclaw_default",
        }
    )

    assert result.model_dump() == {
        "backend_type": "openclaw_default",
        "config_patch": {},
        "required_env": [],
        "plugin_ids": [],
        "disabled_plugin_ids": ["openclaw-mem0"],
        "clear_plugin_slots": ["memory"],
    }


def test_render_mem0_manifest_requires_runtime_env(monkeypatch):
    memory_backend = _memory_backend_module()
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    monkeypatch.delenv("MEM0_USER_ID", raising=False)
    monkeypatch.delenv("MEM0_BASE_URL", raising=False)

    with pytest.raises(ValueError, match="MEM0_API_KEY"):
        memory_backend.render_memory_backend_config(
            {
                "schema_version": "v1",
                "backend_type": "mem0",
                "config": {
                    "mem0_instance_id": VALID_MEM0_UUID,
                },
            }
        )


def test_render_mem0_manifest_to_openclaw_patch(monkeypatch):
    memory_backend = _memory_backend_module()
    monkeypatch.setenv(
        "MEM0_API_KEY",
        f"2000104981.{VALID_MEM0_UUID}:mem0-secret",
    )
    monkeypatch.setenv("MEM0_USER_ID", "2000104981")
    monkeypatch.setenv("MEM0_BASE_URL", "https://mem-service.example.invalid")

    result = memory_backend.render_memory_backend_config(
        {
            "schema_version": "v1",
            "backend_type": "mem0",
            "config": {
                "mem0_instance_id": VALID_MEM0_UUID,
                "mem0_region": "cn-qingyangtest-1",
            },
            "secrets_env": {
                "api_key": "MEM0_API_KEY",
                "user_id": "MEM0_USER_ID",
                "base_url": "MEM0_BASE_URL",
            },
        }
    )

    assert result.model_dump() == {
        "backend_type": "mem0",
        "config_patch": {
            "plugins": {
                "slots": {
                    "memory": "openclaw-mem0",
                },
                "entries": {
                    "openclaw-mem0": {
                        "enabled": True,
                        "config": {
                            "mode": "platform",
                            "apiKey": f"2000104981.{VALID_MEM0_UUID}:mem0-secret",
                            "baseUrl": "https://mem-service.example.invalid",
                            "userId": "2000104981",
                        },
                    },
                },
            }
        },
        "required_env": ["MEM0_API_KEY", "MEM0_USER_ID", "MEM0_BASE_URL"],
        "plugin_ids": ["openclaw-mem0"],
        "disabled_plugin_ids": [],
        "clear_plugin_slots": [],
    }


def test_manifest_model_instances_are_revalidated_against_schema():
    memory_backend = _memory_backend_module()
    manifest_module = _manifest_module()
    manifest = manifest_module.MemoryBackendManifest(
        schema_version="v1",
        backend_type="mem0",
        config={"mem0_instance_id": "not-a-uuid"},
    )

    with pytest.raises(ValueError, match="mem0_instance_id"):
        memory_backend.render_memory_backend_config(manifest)
