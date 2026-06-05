from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from ksadk.skills.loader import load_local_skill
from ksadk.skills.package_store import PackageStore, SkillPackageError
from ksadk.skills.runtime import loader as runtime_loader
from ksadk.skills.runtime.base import SkillRuntimeError
from ksadk.skills.runtime.factory import create_skill_runtime_backend
from ksadk.skills.runtime.registry import match_skill_refs, select_public_skill_refs
from ksadk.skills.service_client import SkillServiceClient
from ksadk.skills.service_env import (
    public_skill_space_ids,
    resolve_skill_service_url,
    user_skill_space_ids,
)
from ksadk.skills.tool_defs import (
    build_execute_skills_tool as build_runtime_execute_skills_tool,
    load_remote_skill_manifests,
    resolve_skill_space_ids,
)
from ksadk.tools.gateway import ToolPolicy, default_tool_gateway
from ksadk.toolsets._langchain import as_tool


_SKILL_TOOL_POLICIES = {
    "list_skills": ToolPolicy(risk_level="low"),
    "search_skills": ToolPolicy(risk_level="low"),
    "load_skill": ToolPolicy(risk_level="low", side_effects=("skill_cache_write",)),
    "execute_skills": ToolPolicy(risk_level="high", side_effects=("isolated_runtime_execution",)),
}


def _gateway():
    return default_tool_gateway(_SKILL_TOOL_POLICIES)


def list_skills() -> dict[str, Any]:
    """List skills discoverable from configured Skill Spaces."""

    try:
        manifests = load_remote_skill_manifests(resolve_skill_space_ids())
        return {
            "ok": True,
            "skills": manifests,
            "skill_space_ids": resolve_skill_space_ids(),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "skill_space_ids": resolve_skill_space_ids(),
        }


def load_skill(skill_name: str) -> dict[str, Any]:
    """Download and load a skill's SKILL.md instructions from configured Skill Spaces."""

    target = str(skill_name or "").strip().lower()
    if not target:
        return {"ok": False, "error_message": "skill_name is required"}
    try:
        client = _skill_service_client()
        available: list[str] = []
        for space_id, skill in _iter_remote_skills(client):
            if not skill.name:
                continue
            available.append(skill.name)
            if skill.name.strip().lower() != target:
                continue
            package = _get_or_download_package(client, skill)
            local_skill = load_local_skill(package.root_dir)
            return {
                "ok": True,
                "execution_context": "outer_agent",
                "name": local_skill.name,
                "description": local_skill.description,
                "space_id": space_id,
                "skill_id": skill.skill_id,
                "version_id": skill.version_id,
                "version": skill.version,
                "cache_hit": bool(getattr(package, "cache_hit", False)),
                "root_dir": str(local_skill.root_dir),
                "has_scripts_dir": (local_skill.root_dir / "scripts").is_dir(),
                "script_files": _script_files(local_skill.root_dir),
                "instructions": local_skill.body,
                "usage": "Read these instructions and complete the user's task in the outer agent unless the skill explicitly requires isolated execution.",
            }
        return {
            "ok": False,
            "error_message": f"Skill not found: {skill_name}",
            "available_skills": available,
        }
    except Exception as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error_message": str(exc)}


def search_skills(query: str, max_results: int = 10) -> dict[str, Any]:
    """Search skills by name, aliases, tags, description, and examples."""

    query_text = str(query or "").strip()
    if not query_text:
        return {"ok": False, "error_message": "query is required"}
    try:
        client = _skill_service_client()
        refs = [skill for _, skill in _iter_remote_skills(client)]
        matches = match_skill_refs(refs, query_text, limit=max_results)
        return {
            "ok": True,
            "query": query,
            "results": [match.to_dict() for match in matches],
        }
    except Exception as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error_message": str(exc)}


def execute_skills(
    workflow_prompt: str,
    skill_names: list[str] | str | None = None,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a workflow through the configured Skill Runtime."""

    return _gateway().invoke(
        "execute_skills",
        _execute_skills_impl,
        workflow_prompt,
        skill_names,
        approval=approval,
    )


def _execute_skills_impl(workflow_prompt: str, skill_names: list[str] | str | None = None) -> dict[str, Any]:
    try:
        backend = create_skill_runtime_backend()
        raw_tool = build_runtime_execute_skills_tool(backend=backend)
        result = raw_tool(workflow_prompt, skill_names)
        if isinstance(result, dict):
            result.setdefault("execution_context", f"skill-runtime/{_skill_execution_backend()}")
        return result
    except SkillRuntimeError as exc:
        return {
            "ok": False,
            "error_type": "skill_runtime_disabled",
            "error_message": str(exc),
            "hint": "Configure KSADK_SKILL_RUNTIME_BACKEND=local_process/e2b or KSADK_SANDBOX_TEMPLATE_ID for isolated Skill workflow execution.",
        }
    except Exception as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error_message": str(exc)}


def get_skill_tools() -> list:
    return [as_tool(list_skills), as_tool(search_skills), as_tool(load_skill), as_tool(execute_skills)]


def _skill_execution_backend() -> str:
    explicit = os.environ.get("KSADK_SKILL_RUNTIME_BACKEND", "").strip().lower()
    if explicit:
        return explicit
    if os.environ.get("KSADK_SANDBOX_TEMPLATE_ID") or os.environ.get("KSADK_SKILL_RUNTIME_TEMPLATE_ID"):
        return "e2b"
    return "disabled"


def _skill_service_client() -> SkillServiceClient:
    service_url = resolve_skill_service_url(require_spaces=True)
    if not service_url:
        raise ValueError("Skill Service URL is not configured")
    return SkillServiceClient(
        base_url=service_url,
        token=os.environ.get("KSADK_SKILL_SERVICE_TOKEN", ""),
        timeout=float(os.environ.get("KSADK_SKILL_MANIFEST_TIMEOUT", "10")),
    )


def _iter_remote_skills(client: SkillServiceClient):
    for space_id in user_skill_space_ids():
        listing = client.list_skills_by_space_id(space_id)
        for skill in listing.active_skills():
            yield space_id, skill
    if public_skill_space_ids():
        listing = client.list_available_premade_skills()
        for skill in select_public_skill_refs(listing.active_skills()):
            yield listing.space_id or "public", skill


def _get_or_download_package(client: SkillServiceClient, skill):
    cache_dir = Path(
        os.environ.get("KSADK_SKILL_CACHE_DIR")
        or Path(tempfile.gettempdir()) / "ksadk-skill-cache"
    )
    store = PackageStore(cache_dir=cache_dir)
    package = store.get_cached(skill)
    if package is not None:
        return package
    archive = client.download_skill_archive(skill)
    try:
        return store.store_archive(skill, archive)
    except SkillPackageError:
        if not runtime_loader._allow_hash_mismatch():
            raise
        return runtime_loader._store_unverified_archive(store, skill, archive)


def _script_files(root_dir: Path) -> list[str]:
    scripts_dir = root_dir / "scripts"
    if not scripts_dir.is_dir():
        return []
    return [
        str(path.relative_to(root_dir))
        for path in sorted(scripts_dir.rglob("*"))
        if path.is_file()
    ][:20]
