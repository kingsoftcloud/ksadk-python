from __future__ import annotations

import json
import os
import re
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
    multi_edit_workspace_file,
    read_workspace_file,
    search_workspace_files,
    workspace_status,
    write_workspace_file,
    write_workspace_files,
)
from ksadk.toolsets.web import get_web_tools
from ksadk.toolsets.web import web_fetch, web_search, _WEB_TOOL_POLICIES
from ksadk.tools.gateway import ToolPolicy, tool_policy_requires_approval
from ksadk.toolsets._langchain import as_tool

_DEFAULT_GROUPS = ("skill", "workspace", "platform", "sandbox", "web")
_DISPATCHER_TOOL_NAME = "tool_dispatcher"
_LEGACY_DISPATCHER_TOOL_NAME = "agentengine_tool_dispatcher"
_TOOL_SEARCH_NAME = "tool_search"
_FOCUSED_TOOL_NAMES = (
    "workspace_status",
    "list_workspace_files",
    "read_workspace_file",
    "write_workspace_file",
    "write_workspace_files",
    "search_workspace_files",
    "edit_workspace_file",
    "multi_edit_workspace_file",
    "lint_workspace_file",
    "component_status",
    "sandbox_status",
    "run_command",
    "run_code",
    "web_fetch",
    "web_search",
    _TOOL_SEARCH_NAME,
)
_DEFERRED_TOOL_NAMES = (_TOOL_SEARCH_NAME, _DISPATCHER_TOOL_NAME)
_EXTERNAL_TOOL_SPECS: dict[str, dict[str, Any]] = {}

_TOOLSET_FACTORIES = {
    "skill": get_skill_tools,
    "skills": get_skill_tools,
    "workspace": get_workspace_tools,
    "platform": get_platform_tools,
    "sandbox": get_sandbox_tools,
    "web": get_web_tools,
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
        (multi_edit_workspace_file, _WORKSPACE_TOOL_POLICIES["multi_edit_workspace_file"], {"boundary": "workspace_root"}),
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
    "web": (
        (web_fetch, _WEB_TOOL_POLICIES["web_fetch"], {"boundary": "public_http"}),
        (web_search, _WEB_TOOL_POLICIES["web_search"], {"boundary": "search_provider"}),
    ),
}


def get_agentengine_tools(
    include: Iterable[str] | None = None,
    *,
    profile: str = "default",
    mode: str = "direct",
) -> list:
    tools, _ = _select_agentengine_tools(include=include, profile=profile, mode=mode)
    return tools


def get_ksadk_builtin_tools(profile: str = "coding") -> list:
    return get_agentengine_tools(profile=profile, mode="direct")


def describe_agentengine_tools(
    include: Iterable[str] | None = None,
    *,
    profile: str = "default",
    mode: str = "direct",
) -> list[dict[str, Any]]:
    return _select_agentengine_tool_descriptors(include=include, profile=profile, mode=mode)


def register_external_tools(
    tools: Iterable[Any],
    *,
    group: str = "external",
    boundary: str = "framework_managed_external_tool",
    risk_level: str = "medium",
    enabled: bool = True,
) -> list[str]:
    """Register framework-managed tool descriptors for tool_search discovery.

    The registered tools are descriptors only. Execution remains owned by the
    framework or runtime that originally bound the external tool.
    """

    names: list[str] = []
    for tool in tools or []:
        spec = _external_tool_spec(
            tool,
            group=group,
            boundary=boundary,
            risk_level=risk_level,
            enabled=enabled,
        )
        if not spec:
            continue
        name = spec["name"]
        _EXTERNAL_TOOL_SPECS[name] = spec
        names.append(name)
    return names


def clear_external_tools(group: str | None = None) -> None:
    """Clear registered external tool descriptors, primarily for tests."""

    if group is None:
        _EXTERNAL_TOOL_SPECS.clear()
        return
    normalized_group = str(group or "").strip()
    for name, spec in list(_EXTERNAL_TOOL_SPECS.items()):
        if str(spec.get("group") or "") == normalized_group:
            _EXTERNAL_TOOL_SPECS.pop(name, None)


def tool_dispatcher(
    action: str,
    tool_name: str | None = None,
    arguments: dict[str, Any] | str | None = None,
    include: str | Iterable[str] | None = None,
    profile: str = "default",
) -> dict[str, Any]:
    """List, describe, or call ksadk built-in tools through one governed entrypoint."""

    normalized_action = str(action or "").strip().lower()
    requested_include = _normalize_include(include)

    if normalized_action == "list":
        try:
            _, specs = _select_agentengine_tools(
                include=requested_include or _profile_default_include(profile),
                profile=profile,
                include_dispatcher=False,
            )
        except ValueError:
            return _unknown_tool_error(", ".join(requested_include) if requested_include else str(include or ""))
        return {"ok": True, "tools": specs, "tool_count": len(specs)}

    if normalized_action == "describe":
        target_name = _normalize_tool_name(tool_name)
        if not target_name:
            return {"ok": False, "error_type": "missing_tool_name", "error_message": "tool_name is required"}
        if target_name in {_DISPATCHER_TOOL_NAME, _LEGACY_DISPATCHER_TOOL_NAME}:
            return _dispatcher_self_call_error()
        try:
            _, specs = _select_agentengine_tools(include=[target_name], profile=profile, include_dispatcher=False)
        except ValueError:
            return _unknown_tool_error(target_name)
        return {"ok": True, "tool": specs[0]}

    if normalized_action == "call":
        target_name = _normalize_tool_name(tool_name)
        if not target_name:
            return {"ok": False, "error_type": "missing_tool_name", "error_message": "tool_name is required"}
        if target_name in {_DISPATCHER_TOOL_NAME, _LEGACY_DISPATCHER_TOOL_NAME}:
            return _dispatcher_self_call_error()
        try:
            tools, specs = _select_agentengine_tools(include=[target_name], profile=profile, include_dispatcher=False)
        except ValueError:
            return _unknown_tool_error(target_name)
        if specs and specs[0].get("enabled") is False:
            return {"ok": False, "error_type": "tool_disabled", "error_message": f"Tool is disabled: {target_name}", "tool_name": target_name}
        tool_arguments, arguments_error = _normalize_tool_arguments(arguments)
        if arguments_error:
            return arguments_error
        tool_arguments = _normalize_dispatched_tool_arguments(target_name, tool_arguments)
        result = _invoke_tool(tools[0], tool_arguments)
        if isinstance(result, dict) and result.get("type") == "approval_required":
            return {**result, "dispatched_tool_name": target_name}
        if isinstance(result, dict) and result.get("ok") is False:
            return {"ok": False, "tool_name": target_name, "result": result}
        return {"ok": True, "tool_name": target_name, "result": result}

    return {
        "ok": False,
        "error_type": "unknown_action",
        "error_message": "action must be one of: list, describe, call",
        "action": action,
    }


def agentengine_tool_dispatcher(
    action: str,
    tool_name: str | None = None,
    arguments: dict[str, Any] | str | None = None,
    include: str | Iterable[str] | None = None,
    profile: str = "default",
) -> dict[str, Any]:
    """Compatibility alias for tool_dispatcher."""

    return tool_dispatcher(
        action=action,
        tool_name=tool_name,
        arguments=arguments,
        include=include,
        profile=profile,
    )


def tool_search(
    query: str,
    profile: str = "coding",
    max_results: int = 8,
    include_disabled: bool = False,
) -> dict[str, Any]:
    """Search ksadk built-in tool descriptions without executing them."""

    text = str(query or "").strip()
    if not text:
        return {"ok": False, "error_type": "query_required", "error_message": "query is required"}
    specs = [
        *describe_agentengine_tools(include=_profile_default_include(profile), profile=profile),
        *_external_tool_specs(),
    ]
    terms = _search_terms(text)
    scored: list[dict[str, Any]] = []
    for spec in specs:
        if spec["name"] in {_DISPATCHER_TOOL_NAME, _LEGACY_DISPATCHER_TOOL_NAME}:
            continue
        if not include_disabled and spec.get("enabled") is False:
            continue
        haystack = _tool_search_text(spec)
        score = _tool_search_score(terms, haystack, spec)
        if score <= 0:
            continue
        scored.append(
            {
                "name": spec["name"],
                "description": spec.get("description", ""),
                "group": spec.get("group", ""),
                "args": dict(spec.get("args") or {}),
                "risk_level": spec.get("risk_level", "low"),
                "enabled": spec.get("enabled", True),
                "boundary": spec.get("boundary", ""),
                "execution": spec.get("execution", "builtin"),
                "score": score,
                "reason": _tool_search_reason(terms, haystack),
            }
        )
    scored.sort(key=lambda item: (-float(item["score"]), str(item["name"])))
    limit = max(1, min(int(max_results or 8), 50))
    results = scored[:limit]
    return {
        "ok": True,
        "query": text,
        "profile": profile,
        "results": results,
        "deferred_tool_names": [item["name"] for item in results],
    }


def _select_agentengine_tools(
    *,
    include: Iterable[str] | None = None,
    profile: str = "default",
    mode: str = "direct",
    include_dispatcher: bool = True,
) -> tuple[list, list[dict[str, Any]]]:
    normalized_mode = _normalize_mode(mode)
    requested = _normalize_include(include) or _profile_default_include(profile)
    if normalized_mode == "deferred" and include is None:
        requested = list(_DEFERRED_TOOL_NAMES)
    if normalized_mode == "dispatcher" and include is None:
        requested = [_DISPATCHER_TOOL_NAME]
    if normalized_mode == "off":
        requested = []
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


def _select_agentengine_tool_descriptors(
    *,
    include: Iterable[str] | None = None,
    profile: str = "default",
    mode: str = "direct",
    include_dispatcher: bool = True,
) -> list[dict[str, Any]]:
    normalized_mode = _normalize_mode(mode)
    requested = _normalize_include(include) or _profile_default_include(profile)
    if normalized_mode == "deferred" and include is None:
        requested = list(_DEFERRED_TOOL_NAMES)
    if normalized_mode == "dispatcher" and include is None:
        requested = [_DISPATCHER_TOOL_NAME]
    if normalized_mode == "off":
        requested = []
    descriptor_registry = _build_descriptor_registry(include_dispatcher=include_dispatcher)
    selected_names = _expand_requested_descriptor_names(requested, descriptor_registry)
    specs: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for tool_name in selected_names:
        if tool_name in seen_names:
            continue
        spec = descriptor_registry.get(tool_name)
        if spec is None:
            raise ValueError(f"Unknown AgentEngine toolset or tool: {tool_name}")
        seen_names.add(tool_name)
        specs.append(dict(spec))
    return specs


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


def _expand_requested_descriptor_names(requested: list[str], descriptor_registry: Mapping[str, Any]) -> list[str]:
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
        if canonical_name == "mcp":
            names.extend(
                spec["name"]
                for spec in _external_tool_specs()
                if str(spec.get("group") or "").startswith("mcp:")
            )
            continue
        external_group_names = [
            spec["name"]
            for spec in _external_tool_specs()
            if str(spec.get("group") or "") == canonical_name
        ]
        if external_group_names:
            names.extend(external_group_names)
            continue
        if canonical_name in descriptor_registry:
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
        registry[_DISPATCHER_TOOL_NAME] = as_tool(tool_dispatcher)
        registry[_TOOL_SEARCH_NAME] = as_tool(tool_search)
        registry[_LEGACY_DISPATCHER_TOOL_NAME] = as_tool(agentengine_tool_dispatcher)
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
            spec = {
                "name": name,
                "group": "platform",
                "description": str(getattr(platform_tool, "description", "") or ""),
                "risk_level": "low",
                "requires_approval": False,
                "side_effects": [],
                "enabled": True,
            }
            args = _tool_args(platform_tool)
            if args:
                spec["args"] = args
            registry[name] = spec
    if include_dispatcher:
        registry[_DISPATCHER_TOOL_NAME] = _tool_spec(
            group="dispatcher",
            name=_DISPATCHER_TOOL_NAME,
            description=(tool_dispatcher.__doc__ or "").strip(),
            policy=ToolPolicy(risk_level="low"),
            extras={
                "boundary": "local_ksadk_builtin_tools",
                "actions": ["list", "describe", "call"],
            },
        )
        registry[_TOOL_SEARCH_NAME] = _tool_spec(
            group="tools",
            name=_TOOL_SEARCH_NAME,
            description=(tool_search.__doc__ or "").strip(),
            policy=ToolPolicy(risk_level="low"),
            extras={
                "boundary": "local_ksadk_builtin_tool_registry",
                "actions": ["search"],
            },
        )
        registry[_LEGACY_DISPATCHER_TOOL_NAME] = _tool_spec(
            group="dispatcher",
            name=_LEGACY_DISPATCHER_TOOL_NAME,
            description=(agentengine_tool_dispatcher.__doc__ or "").strip(),
            policy=ToolPolicy(risk_level="low"),
            extras={
                "boundary": "local_ksadk_builtin_tools",
                "compat_alias_for": _DISPATCHER_TOOL_NAME,
                "actions": ["list", "describe", "call"],
            },
        )
    registry.update(_external_tool_specs_by_name())
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


def _normalize_tool_arguments(arguments: dict[str, Any] | str | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if arguments is None:
        return {}, None
    if isinstance(arguments, dict):
        return dict(arguments), None
    if isinstance(arguments, str):
        text = arguments.strip()
        if not text:
            return {}, None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            return {}, {
                "ok": False,
                "error_type": "invalid_arguments_json",
                "error_message": f"arguments must be a JSON object string: {exc.msg}",
                "arguments": arguments,
            }
        if isinstance(parsed, dict):
            return parsed, None
        return {}, {
            "ok": False,
            "error_type": "invalid_arguments_type",
            "error_message": "arguments JSON string must decode to an object",
            "arguments_type": type(parsed).__name__,
        }
    return {}, {
        "ok": False,
        "error_type": "invalid_arguments_type",
        "error_message": "arguments must be a dictionary or JSON object string",
        "arguments_type": type(arguments).__name__,
    }


def _normalize_dispatched_tool_arguments(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "save_memory" and "content" not in arguments:
        if "key" in arguments and "value" in arguments:
            return {"content": f"{arguments['key']}: {_stringify_memory_value(arguments['value'])}"}
    return arguments


def _stringify_memory_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _tool_args(tool: Any) -> dict[str, Any]:
    args = getattr(tool, "args", None)
    if isinstance(args, dict):
        return dict(args)
    return {}


def _external_tool_specs() -> list[dict[str, Any]]:
    return [dict(spec) for spec in _EXTERNAL_TOOL_SPECS.values()]


def _external_tool_specs_by_name() -> dict[str, dict[str, Any]]:
    return {name: dict(spec) for name, spec in _EXTERNAL_TOOL_SPECS.items()}


def _external_tool_spec(
    tool: Any,
    *,
    group: str,
    boundary: str,
    risk_level: str,
    enabled: bool,
) -> dict[str, Any] | None:
    if isinstance(tool, Mapping):
        name = str(tool.get("name") or "").strip()
        description = str(tool.get("description") or "").strip()
        args = _external_tool_args(tool)
        spec = dict(tool)
    else:
        name = _tool_name(tool)
        description = str(getattr(tool, "description", "") or "").strip()
        args = _external_tool_args(tool)
        spec = {}
    if not name:
        return None
    return {
        **spec,
        "name": name,
        "group": str(group or "external"),
        "description": description,
        "args": args,
        "risk_level": str(spec.get("risk_level") or risk_level or "medium"),
        "requires_approval": bool(spec.get("requires_approval", False)),
        "side_effects": list(spec.get("side_effects") or []),
        "enabled": bool(spec.get("enabled", enabled)),
        "boundary": str(spec.get("boundary") or boundary),
        "execution": "external",
    }


def _external_tool_args(tool: Any) -> dict[str, Any]:
    if isinstance(tool, Mapping):
        args = tool.get("args") or tool.get("parameters") or tool.get("input_schema")
    else:
        args = (
            getattr(tool, "args", None)
            or getattr(tool, "parameters", None)
            or getattr(tool, "input_schema", None)
        )
    if not isinstance(args, Mapping):
        return {}
    if args.get("type") == "object" and isinstance(args.get("properties"), Mapping):
        return dict(args["properties"])
    return dict(args)


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
        "error_message": "tool_dispatcher cannot call itself",
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


def _profile_default_include(profile: str) -> list[str]:
    normalized = str(profile or "default").strip().lower()
    if normalized == "coding":
        return list(_FOCUSED_TOOL_NAMES)
    return list(_DEFAULT_GROUPS)


def _normalize_mode(mode: str) -> str:
    normalized = str(mode or "direct").strip().lower()
    if normalized in {"off", "none", "disabled"}:
        return "off"
    if normalized in {"dispatcher", "dispatch"}:
        return "dispatcher"
    if normalized in {"deferred", "search"}:
        return "deferred"
    return "direct"


def builtin_tools_mode(default: str = "off") -> str:
    return _normalize_mode(os.environ.get("KSADK_BUILTIN_TOOLS_MODE", default))


def builtin_tools_profile(default: str = "default") -> str:
    return str(os.environ.get("KSADK_BUILTIN_TOOLS_PROFILE") or default).strip() or default


def builtin_tool_descriptors_for_runtime(
    *,
    profile: str | None = None,
    mode: str | None = None,
) -> list[dict[str, Any]]:
    resolved_mode = builtin_tools_mode() if mode is None else _normalize_mode(mode)
    resolved_profile = profile or builtin_tools_profile("default")
    if resolved_mode == "off":
        return []
    return describe_agentengine_tools(profile=resolved_profile, mode=resolved_mode)


def builtin_tools_for_runtime(
    *,
    profile: str | None = None,
    mode: str | None = None,
) -> list:
    resolved_mode = builtin_tools_mode() if mode is None else _normalize_mode(mode)
    resolved_profile = profile or builtin_tools_profile("default")
    if resolved_mode == "off":
        return []
    return get_agentengine_tools(profile=resolved_profile, mode=resolved_mode)


def _search_terms(text: str) -> list[str]:
    terms = re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]+", text.lower())
    synonyms = {
        "edit": ["edit", "replace", "modify", "patch", "write", "file"],
        "file": ["file", "workspace", "read", "list", "search", "edit"],
        "grep": ["grep", "search", "rg", "find"],
        "run": ["run", "command", "shell", "code", "execute"],
        "web": ["web", "fetch", "search", "url", "http"],
        "读": ["read", "file", "workspace"],
        "改": ["edit", "replace", "file"],
        "搜": ["search", "grep", "find"],
    }
    expanded: list[str] = []
    for term in terms:
        expanded.append(term)
        expanded.extend(synonyms.get(term, []))
    return expanded


def _tool_search_text(spec: Mapping[str, Any]) -> str:
    values = [
        str(spec.get("name") or ""),
        str(spec.get("group") or ""),
        str(spec.get("description") or ""),
        " ".join((spec.get("args") or {}).keys()) if isinstance(spec.get("args"), Mapping) else "",
    ]
    return " ".join(values).lower()


def _tool_search_score(terms: list[str], haystack: str, spec: Mapping[str, Any]) -> float:
    score = 0.0
    name = str(spec.get("name") or "").lower()
    for term in terms:
        if not term:
            continue
        if term in name:
            score += 4.0
        elif term in haystack:
            score += 1.0
    if str(spec.get("group") or "") == "workspace":
        score += 0.5
    return score


def _tool_search_reason(terms: list[str], haystack: str) -> str:
    matched = [term for term in terms if term and term in haystack]
    return f"matched: {', '.join(matched[:4])}" if matched else "matched registry metadata"


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
    enabled = bool(spec.get("enabled", True))
    if name == "web_search":
        enabled = bool(os.environ.get("KSADK_WEB_SEARCH_PROVIDER") or os.environ.get("OPENCLAW_WEB_SEARCH_PROVIDER"))
    if name in {"run_command", "run_code", "sandbox_status"}:
        enabled = enabled and _enabled_backend(sandbox_backend_name())
    spec["enabled"] = enabled
    return spec


__all__ = [
    "agentengine_tool_dispatcher",
    "tool_dispatcher",
    "tool_search",
    "describe_agentengine_tools",
    "clear_external_tools",
    "builtin_tool_descriptors_for_runtime",
    "builtin_tools_for_runtime",
    "get_ksadk_builtin_tools",
    "get_agentengine_tools",
    "get_platform_tools",
    "get_sandbox_tools",
    "get_skill_tools",
    "get_workspace_tools",
    "get_web_tools",
    "multi_edit_workspace_file",
    "register_external_tools",
    "web_fetch",
    "web_search",
]
