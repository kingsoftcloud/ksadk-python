from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Mapping
from typing import Any

from ksadk.toolsets.platform import get_platform_tools
from ksadk.toolsets.platform import component_status
from ksadk.toolsets.sandbox import get_sandbox_tools
from ksadk.toolsets.sandbox import (
    _SANDBOX_TOOL_POLICIES,
    run_code,
    run_command,
    sandbox_backend_name,
    sandbox_status,
)
from ksadk.toolsets.skills import get_skill_tools
from ksadk.toolsets.skills import (
    _SKILL_TOOL_POLICIES,
    _skill_execution_backend,
    execute_skills,
    list_skills,
    load_skill,
    search_skills,
)
from ksadk.toolsets.workspace import get_workspace_tools
from ksadk.toolsets.workspace import (
    _WORKSPACE_TOOL_POLICIES,
    delete_workspace_file,
    list_workspace_files,
    read_workspace_file,
    search_workspace_files,
    workspace_status,
    write_workspace_file,
    write_workspace_files,
)
from ksadk.tools.gateway import ToolPolicy, tool_policy_requires_approval

_TOOLSET_FACTORIES = {
    "skill": get_skill_tools,
    "skills": get_skill_tools,
    "workspace": get_workspace_tools,
    "platform": get_platform_tools,
    "sandbox": get_sandbox_tools,
}

_TOOLSET_DESCRIPTORS = {
    "skill": (
        (list_skills, _SKILL_TOOL_POLICIES["list_skills"], {}),
        (search_skills, _SKILL_TOOL_POLICIES["search_skills"], {}),
        (load_skill, _SKILL_TOOL_POLICIES["load_skill"], {}),
        (
            execute_skills,
            _SKILL_TOOL_POLICIES["execute_skills"],
            {
                "backend": lambda: _skill_execution_backend(),
                "enabled": lambda: _enabled_backend(_skill_execution_backend()),
                "boundary": "isolated_skill_runtime",
            },
        ),
    ),
    "workspace": (
        (workspace_status, _WORKSPACE_TOOL_POLICIES["workspace_status"], {"boundary": "workspace_root"}),
        (list_workspace_files, _WORKSPACE_TOOL_POLICIES["list_workspace_files"], {"boundary": "workspace_root"}),
        (read_workspace_file, _WORKSPACE_TOOL_POLICIES["read_workspace_file"], {"boundary": "workspace_root"}),
        (write_workspace_file, _WORKSPACE_TOOL_POLICIES["write_workspace_file"], {"boundary": "workspace_root"}),
        (write_workspace_files, _WORKSPACE_TOOL_POLICIES["write_workspace_files"], {"boundary": "workspace_root"}),
        (search_workspace_files, _WORKSPACE_TOOL_POLICIES["search_workspace_files"], {"boundary": "workspace_root"}),
        (delete_workspace_file, _WORKSPACE_TOOL_POLICIES["delete_workspace_file"], {"boundary": "workspace_root"}),
    ),
    "platform": (
        (component_status, ToolPolicy(risk_level="low"), {}),
    ),
    "sandbox": (
        (
            sandbox_status,
            _SANDBOX_TOOL_POLICIES["sandbox_status"],
            {
                "backend": lambda: sandbox_backend_name(),
                "enabled": lambda: _enabled_backend(sandbox_backend_name()),
                "boundary": "isolated_sandbox",
            },
        ),
        (
            run_command,
            _SANDBOX_TOOL_POLICIES["run_command"],
            {
                "backend": lambda: sandbox_backend_name(),
                "enabled": lambda: _enabled_backend(sandbox_backend_name()),
                "boundary": "isolated_sandbox",
            },
        ),
        (
            run_code,
            _SANDBOX_TOOL_POLICIES["run_code"],
            {
                "backend": lambda: sandbox_backend_name(),
                "enabled": lambda: _enabled_backend(sandbox_backend_name()),
                "boundary": "isolated_sandbox",
            },
        ),
    ),
}


def get_agentengine_tools(include: Iterable[str] | None = None) -> list:
    requested = list(include or ("skill", "workspace", "platform", "sandbox"))
    tools = []
    seen_names: set[str] = set()
    for name in requested:
        factory = _TOOLSET_FACTORIES.get(str(name).strip().lower())
        if factory is None:
            raise ValueError(f"Unknown AgentEngine toolset: {name}")
        for tool in factory():
            tool_name = getattr(tool, "name", None) or getattr(tool, "__name__", "")
            if tool_name in seen_names:
                continue
            seen_names.add(tool_name)
            tools.append(tool)
    return tools


def describe_agentengine_tools(include: Iterable[str] | None = None) -> list[dict[str, Any]]:
    requested = list(include or ("skill", "workspace", "platform", "sandbox"))
    specs: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for group_name in requested:
        canonical_group = _canonical_toolset_group(group_name)
        descriptors = _TOOLSET_DESCRIPTORS.get(canonical_group)
        if descriptors is None:
            raise ValueError(f"Unknown AgentEngine toolset: {group_name}")
        for func, policy, extras in descriptors:
            name = getattr(func, "__name__", "")
            if name in seen_names:
                continue
            seen_names.add(name)
            specs.append(
                _tool_spec(
                    group=canonical_group,
                    name=name,
                    description=(getattr(func, "__doc__", "") or "").strip(),
                    policy=policy,
                    extras=extras,
                )
            )
    return specs


def _canonical_toolset_group(name: object) -> str:
    value = str(name).strip().lower()
    if value == "skills":
        return "skill"
    return value


def _enabled_backend(backend: str) -> bool:
    return backend not in {"", "disabled", "none", "off"}


def _tool_spec(
    *,
    group: str,
    name: str,
    description: str,
    policy: ToolPolicy,
    extras: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    spec = {
        "name": name,
        "group": group,
        "description": description,
        "risk_level": policy.risk_level,
        "requires_approval": tool_policy_requires_approval(policy),
        "side_effects": list(policy.side_effects),
        "enabled": True,
    }
    for key, value in dict(extras or {}).items():
        spec[key] = value() if callable(value) else value
    return spec


__all__ = [
    "describe_agentengine_tools",
    "get_agentengine_tools",
    "get_platform_tools",
    "get_sandbox_tools",
    "get_skill_tools",
    "get_workspace_tools",
]
