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
    edit_workspace_file,
    lint_workspace_file,
    list_workspace_files,
    read_workspace_file,
    search_workspace_files,
    workspace_status,
    write_workspace_file,
    write_workspace_files,
)
from ksadk.tools.gateway import ToolPolicy, tool_policy_requires_approval
from ksadk.toolsets._langchain import as_tool

_DEFAULT_GROUPS = ("skill", "workspace", "platform", "sandbox")
_DISPATCHER_TOOL_NAME = "agentengine_tool_dispatcher"
_FOCUSED_TOOL_NAMES = (
    "list_skills",
    "search_skills",
    "load_skill",
    "workspace_status",
    "search_workspace_files",
    "edit_workspace_file",
    "lint_workspace_file",
    "component_status",
    "sandbox_status",
)

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
        (edit_workspace_file, _WORKSPACE_TOOL_POLICIES["edit_workspace_file"], {"boundary": "workspace_root"}),
        (lint_workspace_file, _WORKSPACE_TOOL_POLICIES["lint_workspace_file"], {"boundary": "workspace_root"}),
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
    tools, _ = _select_agentengine_tools(include=include)
    return tools


def describe_agentengine_tools(include: Iterable[str] | None = None) -> list[dict[str, Any]]:
    _, specs = _select_agentengine_tools(include=include)
    return specs


def agentengine_tool_dispatcher(
    action: str,
    tool_name: str | None = None,
    arguments: dict[str, Any] | None = None,
    include: Iterable[str] | str | None = None,
) -> dict[str, Any]:
    """List, describe, or call less frequently bound AgentEngine built-in tools."""

    normalized_action = str(action or "").strip().lower()
    requested_include = _normalize_include(include)

    if normalized_action == "list":
        try:
            _, specs = _select_agentengine_tools(include=requested_include or _DEFAULT_GROUPS, include_dispatcher=False)
        except ValueError:
            return _unknown_tool_error(", ".join(requested_include) if requested_include else str(include or ""))
        return {"ok": True, "tools": specs, "tool_count": len(specs)}

    if normalized_action == "describe":
        target_name = _normalize_tool_name(tool_name)
        if not target_name:
            return {"ok": False, "error_type": "missing_tool_name", "error_message": "tool_name is required"}
        if target_name == _DISPATCHER_TOOL_NAME:
            return _dispatcher_self_call_error()
        try:
            _, specs = _select_agentengine_tools(include=[target_name], include_dispatcher=False)
        except ValueError:
            return _unknown_tool_error(target_name)
        return {"ok": True, "tool": specs[0]}

    if normalized_action == "call":
        target_name = _normalize_tool_name(tool_name)
        if not target_name:
            return {"ok": False, "error_type": "missing_tool_name", "error_message": "tool_name is required"}
        if target_name == _DISPATCHER_TOOL_NAME:
            return _dispatcher_self_call_error()
        try:
            tools, _ = _select_agentengine_tools(include=[target_name], include_dispatcher=False)
        except ValueError:
            return _unknown_tool_error(target_name)
        result = _invoke_tool(tools[0], dict(arguments or {}))
        if isinstance(result, dict) and result.get("type") == "approval_required":
            return {**result, "dispatched_tool_name": target_name}
        return {"ok": True, "tool_name": target_name, "result": result}

    return {
        "ok": False,
        "error_type": "unknown_action",
        "error_message": "action must be one of: list, describe, call",
        "action": action,
    }


def _select_agentengine_tools(
    *,
    include: Iterable[str] | None = None,
    include_dispatcher: bool = True,
) -> tuple[list, list[dict[str, Any]]]:
    requested = _normalize_include(include) or list(_DEFAULT_GROUPS)
    tool_registry = _build_tool_registry(include_dispatcher=include_dispatcher)
    descriptor_registry = _build_descriptor_registry(include_dispatcher=include_dispatcher)
    selected_names = _expand_requested_names(requested, tool_registry)
    tools = []
    specs: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for tool_name in selected_names:
        if tool_name in seen_names:
            continue
        tool = tool_registry.get(tool_name)
        spec = descriptor_registry.get(tool_name)
        if tool is None or spec is None:
            raise ValueError(f"Unknown AgentEngine toolset or tool: {tool_name}")
        seen_names.add(tool_name)
        tools.append(tool)
        specs.append(spec)
    return tools, specs


def _expand_requested_names(requested: list[str], tool_registry: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    for name in requested:
        canonical_name = _canonical_toolset_group(name)
        if canonical_name in {"focused", "core"}:
            names.extend(_FOCUSED_TOOL_NAMES)
            continue
        if canonical_name in _TOOLSET_FACTORIES:
            for tool in _TOOLSET_FACTORIES[canonical_name]():
                names.append(_tool_name(tool))
            continue
        if canonical_name in tool_registry:
            names.append(canonical_name)
            continue
        raise ValueError(f"Unknown AgentEngine toolset or tool: {name}")
    return names


def _build_tool_registry(*, include_dispatcher: bool) -> dict[str, Any]:
    registry: dict[str, Any] = {}
    for group_name in _DEFAULT_GROUPS:
        for tool in _TOOLSET_FACTORIES[group_name]():
            name = _tool_name(tool)
            if name and name not in registry:
                registry[name] = tool
    if include_dispatcher:
        registry[_DISPATCHER_TOOL_NAME] = as_tool(agentengine_tool_dispatcher)
    return registry


def _build_descriptor_registry(*, include_dispatcher: bool) -> dict[str, dict[str, Any]]:
    registry: dict[str, dict[str, Any]] = {}
    for group_name in _DEFAULT_GROUPS:
        for func, policy, extras in _TOOLSET_DESCRIPTORS[group_name]:
            name = getattr(func, "__name__", "")
            if name and name not in registry:
                registry[name] = _tool_spec(
                    group=group_name,
                    name=name,
                    description=(getattr(func, "__doc__", "") or "").strip(),
                    policy=policy,
                    extras=extras,
                )
    for platform_tool in get_platform_tools():
        name = _tool_name(platform_tool)
        if name and name not in registry:
            registry[name] = {
                "name": name,
                "group": "platform",
                "description": str(getattr(platform_tool, "description", "") or ""),
                "risk_level": "low",
                "requires_approval": False,
                "side_effects": [],
                "enabled": True,
            }
    if include_dispatcher:
        registry[_DISPATCHER_TOOL_NAME] = _tool_spec(
            group="dispatcher",
            name=_DISPATCHER_TOOL_NAME,
            description=(agentengine_tool_dispatcher.__doc__ or "").strip(),
            policy=ToolPolicy(risk_level="low"),
            extras={
                "boundary": "local_ksadk_builtin_tools",
                "actions": ["list", "describe", "call"],
            },
        )
    return registry


def _canonical_toolset_group(name: object) -> str:
    value = str(name).strip().lower()
    if value == "skills":
        return "skill"
    return value


def _normalize_include(include: Iterable[str] | str | None) -> list[str]:
    if include is None:
        return []
    if isinstance(include, str):
        return [include]
    return [str(item) for item in include]


def _normalize_tool_name(tool_name: str | None) -> str:
    return str(tool_name or "").strip()


def _tool_name(tool: Any) -> str:
    return str(getattr(tool, "name", None) or getattr(tool, "__name__", "") or "")


def _invoke_tool(tool: Any, arguments: dict[str, Any]) -> Any:
    if hasattr(tool, "invoke"):
        return tool.invoke(arguments)
    return tool(**arguments)


def _dispatcher_self_call_error() -> dict[str, Any]:
    return {
        "ok": False,
        "error_type": "dispatcher_self_call",
        "error_message": "agentengine_tool_dispatcher cannot call itself",
        "tool_name": _DISPATCHER_TOOL_NAME,
    }


def _unknown_tool_error(tool_name: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error_type": "unknown_tool",
        "error_message": f"Unknown AgentEngine tool: {tool_name}",
        "tool_name": tool_name,
    }


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
    "agentengine_tool_dispatcher",
    "describe_agentengine_tools",
    "get_agentengine_tools",
    "get_platform_tools",
    "get_sandbox_tools",
    "get_skill_tools",
    "get_workspace_tools",
]
