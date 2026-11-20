from __future__ import annotations

import importlib

import pytest

VALID_MEM0_UUID = "e52b7fac-e641-4b34-b9f7-6b0b9f190cd4"
VALID_KSMEM_ID = "mem-1a2164ae-d5f5-425b-a6cd-f2481eefecfd"


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
        "disabled_plugin_ids": ["openclaw-mem0", "memory-lancedb", "openclaw-ksmem"],
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
    monkeypatch.delenv("KSADK_COMPONENT_ENV", raising=False)
    monkeypatch.delenv("KSADK_TRAFFIC_SOURCE", raising=False)
    monkeypatch.setenv(
        "MEM0_API_KEY",
        f"fixture-user.{VALID_MEM0_UUID}:mem0-secret",
    )
    monkeypatch.setenv("MEM0_USER_ID", "fixture-user")
    monkeypatch.setenv("MEM0_BASE_URL", "http://mem-service.example.com")

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
                            "apiKey": f"fixture-user.{VALID_MEM0_UUID}:mem0-secret",
                            "baseUrl": "http://mem-service.example.com",
                            "userId": "fixture-user",
                            "extraHeaders": {"X-Traffic-Source": "agentkit"},
                        },
                    },
                },
            }
        },
        "required_env": ["MEM0_API_KEY", "MEM0_USER_ID", "MEM0_BASE_URL"],
        "plugin_ids": ["openclaw-mem0"],
        "disabled_plugin_ids": ["memory-lancedb", "openclaw-ksmem"],
        "clear_plugin_slots": [],
    }


def test_render_lancedb_manifest_to_openclaw_patch():
    memory_backend = _memory_backend_module()

    result = memory_backend.render_memory_backend_config(
        {
            "schema_version": "v1",
            "backend_type": "lancedb",
        }
    )

    assert result.model_dump() == {
        "backend_type": "lancedb",
        "config_patch": {
            "plugins": {
                "slots": {
                    "memory": "memory-lancedb",
                },
                "entries": {
                    "memory-lancedb": {
                        "enabled": True,
                        "hooks": {"allowConversationAccess": True},
                    },
                },
            }
        },
        "required_env": [],
        "plugin_ids": ["memory-lancedb"],
        "disabled_plugin_ids": ["openclaw-mem0", "openclaw-ksmem"],
        "clear_plugin_slots": [],
    }


def test_render_ksmem_manifest_to_openclaw_patch(monkeypatch):
    memory_backend = _memory_backend_module()
    monkeypatch.setenv("KSMEM_MEMORY_ID", VALID_KSMEM_ID)

    result = memory_backend.render_memory_backend_config(
        {
            "schema_version": "v1",
            "backend_type": "ksmem",
            "config": {"ksmem_instance_id": VALID_KSMEM_ID},
            "secrets_env": {"memory_id": "KSMEM_MEMORY_ID"},
        }
    )

    assert result.model_dump() == {
        "backend_type": "ksmem",
        "config_patch": {
            "plugins": {
                "slots": {"memory": "openclaw-ksmem"},
                "entries": {
                    "openclaw-ksmem": {
                        "enabled": True,
                        "hooks": {"allowConversationAccess": True},
                        "config": {
                            "memoryIdEnv": "KSMEM_MEMORY_ID",
                            "autoRecall": True,
                            "autoCapture": True,
                            "topK": 5,
                        },
                    }
                },
            }
        },
        "required_env": ["KSMEM_MEMORY_ID"],
        "plugin_ids": ["openclaw-ksmem"],
        "disabled_plugin_ids": ["openclaw-mem0", "memory-lancedb"],
        "clear_plugin_slots": [],
    }


def test_render_ksmem_manifest_requires_runtime_env(monkeypatch):
    memory_backend = _memory_backend_module()
    monkeypatch.delenv("KSMEM_MEMORY_ID", raising=False)

    with pytest.raises(ValueError, match="KSMEM_MEMORY_ID"):
        memory_backend.render_memory_backend_config(
            {
                "schema_version": "v1",
                "backend_type": "ksmem",
                "config": {"ksmem_instance_id": VALID_KSMEM_ID},
            }
        )


def test_render_lancedb_manifest_passes_optional_config_to_plugin():
    memory_backend = _memory_backend_module()

    result = memory_backend.render_memory_backend_config(
        {
            "schema_version": "v1",
            "backend_type": "lancedb",
            "config": {
                "dbPath": "/home/node/.openclaw/memory/lancedb",
            },
        }
    )

    entry = result.config_patch["plugins"]["entries"]["memory-lancedb"]
    assert entry["enabled"] is True
    assert entry["config"]["dbPath"] == "/home/node/.openclaw/memory/lancedb"
    assert entry["hooks"] == {"allowConversationAccess": True}


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


@pytest.mark.parametrize("component_env", [None, "", "prod", "pre", " PRE "])
def test_mem0_pre_route_headers_preserve_credentials(monkeypatch, component_env):
    monkeypatch.setenv("MEM0_API_KEY", "fake-memory-key")
    monkeypatch.setenv("MEM0_USER_ID", "fake-user")
    monkeypatch.setenv("MEM0_BASE_URL", "https://memory.example.test")
    monkeypatch.setenv("KSADK_TRAFFIC_SOURCE", "agentkit")
    if component_env is None:
        monkeypatch.delenv("KSADK_COMPONENT_ENV", raising=False)
    else:
        monkeypatch.setenv("KSADK_COMPONENT_ENV", component_env)
    result = _memory_backend_module().render_memory_backend_config(
        {
            "schema_version": "v1",
            "backend_type": "mem0",
            "config": {"mem0_instance_id": VALID_MEM0_UUID},
        }
    )
    config = result.config_patch["plugins"]["entries"]["openclaw-mem0"]["config"]
    assert config["extraHeaders"].get("X-KSC-CUSTOM-SOURCE") == (
        "pre" if component_env in {"pre", " PRE "} else None
    )
    assert config["extraHeaders"]["X-Traffic-Source"] == "agentkit"
    assert "X-Ksadk-Component-Env" not in config["extraHeaders"]
    assert config["apiKey"] == "fake-memory-key"
    assert config["userId"] == "fake-user"
    assert config["baseUrl"] == "https://memory.example.test"
