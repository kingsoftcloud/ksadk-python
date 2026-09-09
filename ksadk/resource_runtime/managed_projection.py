"""Project frozen platform-resource bindings into a managed-runtime MCP server.

The projection is provider-neutral: a managed Agent keeps the resource declarations
in its immutable manifest and receives one local MCP connector. Credentials remain
workload environment variables and never enter the manifest or artifact.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Any

from ksadk.resource_runtime.contracts import ResourceConfig, validate_resource_bindings
from ksadk.resource_runtime.plugin_config import resource_plugin_config

_SERVER_NAME = "platform-resources"
_CREDENTIAL_ENV_NAMES = (
    "KSYUN_ACCESS_KEY",
    "KSYUN_SECRET_KEY",
    "KSYUN_SESSION_TOKEN",
    "KSYUN_ACCESS_KEY_ID",
    "KSYUN_SECRET_ACCESS_KEY",
)


def native_codex_plugin_bindings(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return enabled native Codex bindings, excluding DSH resource declarations."""

    bindings: list[dict[str, Any]] = []
    raw = manifest.get("plugins") or []
    if not isinstance(raw, (list, tuple)):
        return bindings
    for item in raw:
        if not isinstance(item, Mapping) or not item.get("enabled", True):
            continue
        if str(item.get("ecosystem") or "").strip().lower() == "codex":
            bindings.append(dict(item))
    return bindings


def _resource_configs(manifest: Mapping[str, Any]) -> tuple[ResourceConfig, ...]:
    resources: list[ResourceConfig] = []
    raw = manifest.get("plugins") or []
    if not isinstance(raw, (list, tuple)):
        return ()
    for item in raw:
        if not isinstance(item, Mapping) or not item.get("enabled", True):
            continue
        parsed = resource_plugin_config(
            str(item.get("plugin_ref") or item.get("pluginRef") or ""),
            str(item.get("ecosystem") or ""),
            item.get("config") if isinstance(item.get("config"), Mapping) else {},
            enabled=True,
        )
        if parsed is not None:
            resources.append(parsed)
    validate_resource_bindings(tuple(resources))
    return tuple(resources)


def _memory_write_enabled(
    manifest: Mapping[str, Any], resources: tuple[ResourceConfig, ...]
) -> bool:
    memory_configs = [item for item in resources if item.binding.resource.kind == "memory-instance"]
    if not memory_configs:
        return False
    memory = manifest.get("memory")
    context = manifest.get("context")
    if not isinstance(memory, Mapping) or not bool(memory.get("enabled")):
        return False
    if str(memory.get("providerRef") or memory.get("provider_ref") or "") != (
        "binding://" + memory_configs[0].binding.id
    ):
        return False
    write = memory.get("write")
    rollout = context.get("rollout") if isinstance(context, Mapping) else None
    mode = str(write.get("mode") or "off") if isinstance(write, Mapping) else "off"
    rollout_mode = (
        str(rollout.get("memoryWrite") or rollout.get("memory_write") or "off")
        if isinstance(rollout, Mapping)
        else "off"
    )
    return mode in {"explicit_only", "candidate"} and rollout_mode == "enabled"


def project_managed_platform_resources(
    manifest: Mapping[str, Any],
    *,
    process_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return runtime config additions for official platform resources."""

    resources = _resource_configs(manifest)
    if not resources:
        return {}
    source_env = process_env if process_env is not None else os.environ
    projected_env: dict[str, str] = {
        "KSADK_AICP_ENDPOINT_MODE": "inner",
        "KSADK_PLATFORM_RESOURCE_KINDS": ",".join(
            sorted(item.binding.resource.kind for item in resources)
        ),
        "KSADK_PLATFORM_RESOURCE_AGENT_ID": str(
            source_env.get("AGENTENGINE_PLUGIN_AGENT_ID")
            or source_env.get("AGENTENGINE_RESOURCE_AGENT_ID")
            or manifest.get("name")
            or "managed-agent"
        ),
        "KSADK_PLATFORM_RESOURCE_SUBJECT": str(
            source_env.get("AGENTENGINE_RESOURCE_ACTOR_REF")
            or source_env.get("AGENTENGINE_PLUGIN_AGENT_ID")
            or manifest.get("name")
            or "managed-agent"
        ),
        "KSADK_PLATFORM_RESOURCE_MEMORY_WRITE": (
            "true" if _memory_write_enabled(manifest, resources) else "false"
        ),
    }
    for item in resources:
        resource = item.binding.resource
        if resource.kind == "memory-instance":
            projected_env.update(
                {
                    "KSADK_LTM_BACKEND": "sdk",
                    "KSADK_LTM_NAMESPACE": resource.id,
                    "KSADK_LTM_REGION": resource.region,
                    "KSADK_LTM_AGENT_ID": projected_env["KSADK_PLATFORM_RESOURCE_AGENT_ID"],
                }
            )
        elif resource.kind == "knowledge-base":
            policy = item.retrieval
            projected_env.update(
                {
                    "KSADK_KB_DATASET_ID": resource.id,
                    "KSADK_KB_REGION": resource.region,
                    "KSADK_KB_TOP_K": str(policy.top_k if policy is not None else 5),
                }
            )
        elif resource.kind == "skill-space":
            projected_env.update(
                {
                    "KSADK_SKILL_SPACE_IDS": resource.id,
                    "KSADK_SKILL_SERVICE_REGION": resource.region,
                }
            )
            if item.include_public:
                projected_env["KSADK_PUBLIC_SKILL_SPACE_IDS"] = "public"

    env_refs = {name: name for name in (*projected_env, *_CREDENTIAL_ENV_NAMES)}
    return {
        "env": projected_env,
        "mcp_server": {
            "name": _SERVER_NAME,
            "transport": "stdio",
            "command": sys.executable,
            "args": ["-m", "ksadk.resource_runtime.mcp_server"],
            "env_refs": env_refs,
        },
    }


def apply_managed_platform_resources(config: dict[str, Any]) -> dict[str, Any]:
    """Apply projection without overwriting user MCP or environment values."""

    projected = project_managed_platform_resources(config)
    if not projected:
        return config
    servers = list(config.get("mcp_servers") or [])
    if any(isinstance(item, Mapping) and item.get("name") == _SERVER_NAME for item in servers):
        raise ValueError("mcp server name 'platform-resources' is reserved")
    servers.append(projected["mcp_server"])
    env = dict(config.get("env") or {})
    if set(env).intersection(projected["env"]):
        raise ValueError("managed platform-resource environment is reserved")
    env.update(projected["env"])
    config["mcp_servers"] = servers
    config["env"] = env
    return config


__all__ = [
    "apply_managed_platform_resources",
    "native_codex_plugin_bindings",
    "project_managed_platform_resources",
]
