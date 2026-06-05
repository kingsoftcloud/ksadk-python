from __future__ import annotations

import os

from ksadk.skills.service_env import public_skill_space_ids, skill_space_ids, user_skill_space_ids
from ksadk.toolsets._langchain import as_tool
from ksadk.toolsets.sandbox import sandbox_backend_name
from ksadk.toolsets.workspace import workspace_root


def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name) or default).strip()


def _skill_execution_backend() -> str:
    explicit = _env("KSADK_SKILL_RUNTIME_BACKEND").lower()
    if explicit:
        return explicit
    if _env("KSADK_SANDBOX_TEMPLATE_ID") or _env("KSADK_SKILL_RUNTIME_TEMPLATE_ID"):
        return "e2b"
    return "disabled"


def component_status() -> dict:
    """Report AgentEngine built-in toolset and runtime binding status."""

    skill_runtime_backend = _skill_execution_backend()
    sandbox_backend = sandbox_backend_name()
    spaces = skill_space_ids()
    return {
        "ok": True,
        "summary": {
            "model": _env("OPENAI_MODEL_NAME") or _env("MODEL_NAME"),
            "knowledge_base_bound": bool(_env("KSADK_KB_DATASET_ID")),
            "long_term_memory_bound": bool(_env("KSADK_LTM_NAMESPACE")),
            "skill_space_bound": bool(spaces),
            "isolated_execution": "enabled" if skill_runtime_backend not in {"", "disabled", "none", "off"} else "not_enabled",
            "sandbox_direct_tools": "enabled" if sandbox_backend not in {"", "disabled", "none", "off"} else "not_enabled",
        },
        "skill_space": {
            "space_ids": spaces,
            "user_space_ids": user_skill_space_ids(),
            "public_space_ids": public_skill_space_ids(),
            "service_url": _env("KSADK_SKILL_SERVICE_URL"),
            "aicp_endpoint_mode": _env("KSADK_AICP_ENDPOINT_MODE", "auto"),
            "tools": ["list_skills", "search_skills", "load_skill", "execute_skills"],
        },
        "skill_runtime": {
            "backend": skill_runtime_backend,
            "enabled": skill_runtime_backend not in {"", "disabled", "none", "off"},
            "template_bound": bool(_env("KSADK_SANDBOX_TEMPLATE_ID") or _env("KSADK_SKILL_RUNTIME_TEMPLATE_ID")),
            "request_protocol": "--request-file JSON envelope",
        },
        "sandbox": {
            "backend": sandbox_backend,
            "enabled": sandbox_backend not in {"", "disabled", "none", "off"},
            "template_bound": bool(_env("KSADK_SANDBOX_TEMPLATE_ID") or _env("KSADK_SKILL_RUNTIME_TEMPLATE_ID")),
            "tools": ["sandbox_status", "run_command", "run_code"],
        },
        "workspace": {
            "root": str(workspace_root()),
            "tools": [
                "workspace_status",
                "list_workspace_files",
                "read_workspace_file",
                "write_workspace_file",
                "write_workspace_files",
                "search_workspace_files",
                "delete_workspace_file",
            ],
            "boundary": "Workspace tools are confined to the AgentEngine UI workspace directory.",
        },
    }


def get_platform_tools() -> list:
    tools = [as_tool(component_status)]
    try:
        from ksadk.knowledge_base.langchain_tool import search_knowledge_base

        tools.append(search_knowledge_base)
    except Exception:
        pass
    try:
        from ksadk.memory.langchain_tool import load_memory, save_memory

        tools.extend([load_memory, save_memory])
    except Exception:
        pass
    return tools
