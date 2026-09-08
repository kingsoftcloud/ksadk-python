from __future__ import annotations

import pytest

from ksadk.resource_runtime.managed_projection import (
    apply_managed_platform_resources,
    native_codex_plugin_bindings,
    project_managed_platform_resources,
)


def _binding(kind: str, resource_id: str) -> dict:
    names = {
        "memory-instance": "memory",
        "knowledge-base": "knowledge",
        "skill-space": "skill-center",
    }
    suffix = names[kind]
    config = {
        "schemaVersion": 1,
        "binding": {
            "id": f"binding-{suffix}",
            "connectionRef": "ksyun-platform-default",
            "required": True,
            "resource": {
                "kind": kind,
                "id": resource_id,
                "region": "cn-beijing-6",
            },
        },
    }
    if kind == "knowledge-base":
        config["retrieval"] = {"mode": "tool", "topK": 7, "maxChars": 16000}
    if kind == "skill-space":
        config.update(
            {
                "selectionMode": "discovery",
                "executionMode": "outer-agent",
                "selectedSkills": [],
                "includePublic": False,
            }
        )
    return {
        "plugin_ref": f"plugin://kingsoftcloud.dsh-{suffix}@0.1.0",
        "ecosystem": "dsh",
        "enabled": True,
        "components": [f"dsh-{suffix}"],
        "config": config,
    }


def _manifest() -> dict:
    return {
        "name": "resource-agent",
        "plugins": [
            _binding("memory-instance", "mem-1"),
            _binding("knowledge-base", "kb-1"),
            _binding("skill-space", "space-1"),
            {
                "plugin_ref": "plugin://codex.demo@1.0.0",
                "ecosystem": "codex",
                "enabled": True,
                "components": ["skill:demo"],
            },
        ],
        "memory": {
            "enabled": True,
            "providerRef": "binding://binding-memory",
            "write": {"mode": "explicit_only"},
        },
        "context": {"rollout": {"memoryWrite": "enabled"}},
    }


def test_projects_all_resource_bindings_without_copying_credentials() -> None:
    projected = project_managed_platform_resources(
        _manifest(), process_env={"AGENTENGINE_PLUGIN_AGENT_ID": "ar-123"}
    )

    env = projected["env"]
    assert env["KSADK_LTM_NAMESPACE"] == "mem-1"
    assert env["KSADK_KB_DATASET_ID"] == "kb-1"
    assert env["KSADK_KB_TOP_K"] == "7"
    assert env["KSADK_SKILL_SPACE_IDS"] == "space-1"
    assert env["KSADK_PLATFORM_RESOURCE_SUBJECT"] == "ar-123"
    assert env["KSADK_PLATFORM_RESOURCE_MEMORY_WRITE"] == "true"
    assert "KSYUN_ACCESS_KEY" not in env
    assert "KSYUN_ACCESS_KEY" in projected["mcp_server"]["env_refs"]


def test_resource_bindings_are_not_native_codex_artifacts() -> None:
    assert native_codex_plugin_bindings(_manifest()) == [_manifest()["plugins"][-1]]


def test_apply_rejects_reserved_mcp_collision() -> None:
    manifest = _manifest()
    manifest["mcp_servers"] = [{"name": "platform-resources", "transport": "stdio"}]
    with pytest.raises(ValueError, match="reserved"):
        apply_managed_platform_resources(manifest)


def test_memory_write_is_omitted_when_rollout_is_not_enabled(monkeypatch) -> None:
    manifest = _manifest()
    manifest["context"]["rollout"]["memoryWrite"] = "shadow"
    projection = project_managed_platform_resources(manifest)
    monkeypatch.setenv(
        "KSADK_PLATFORM_RESOURCE_KINDS", projection["env"]["KSADK_PLATFORM_RESOURCE_KINDS"]
    )
    monkeypatch.setenv("KSADK_PLATFORM_RESOURCE_MEMORY_WRITE", "false")
    from ksadk.resource_runtime.mcp_server import create_server

    names = {tool.name for tool in create_server()._tool_manager.list_tools()}
    assert {"load_memory", "search_knowledge_base", "list_skills", "load_skill"} <= names
    assert "save_memory" not in names
    assert "memory_status" not in names


def test_memory_write_and_status_are_exposed_when_policy_is_enabled(monkeypatch) -> None:
    projection = project_managed_platform_resources(_manifest())
    for key, value in projection["env"].items():
        monkeypatch.setenv(key, value)
    from ksadk.resource_runtime.mcp_server import create_server

    names = {tool.name for tool in create_server()._tool_manager.list_tools()}
    assert {"load_memory", "save_memory", "memory_status"} <= names
