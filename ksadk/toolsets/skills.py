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
    _skill_manifest_item,
    load_remote_skill_manifests,
    resolve_skill_space_ids,
)
from ksadk.skills.tool_defs import (
    build_execute_skills_tool as build_runtime_execute_skills_tool,
)
from ksadk.tools.gateway import ToolPolicy, default_tool_gateway
from ksadk.toolsets._langchain import as_tool

_SKILL_TOOL_POLICIES = {
    "list_skill_spaces": ToolPolicy(risk_level="low"),
    "list_skills": ToolPolicy(risk_level="low"),
    "search_skills": ToolPolicy(risk_level="low"),
    "load_skill": ToolPolicy(risk_level="low", side_effects=("skill_cache_write",)),
    "execute_skills": ToolPolicy(risk_level="high", side_effects=("isolated_runtime_execution",)),
}


def _gateway():
    return default_tool_gateway(_SKILL_TOOL_POLICIES)


def _dict_result(value: Any, *, tool_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{tool_name} returned {type(value).__name__}, expected dict")
    return dict(value)


def list_skill_spaces() -> dict[str, Any]:
    """List visible Skill Spaces with id/name/description, so the model can pick one by intent.

    多 space 场景下,先调用本工具了解可选 space,再按用户意图用
    ``search_skills(query, space_id=...)`` 或 ``load_skill(name, space_id=...)`` 定向查找。
    """

    configured = resolve_skill_space_ids()
    try:
        client = _skill_service_client()
        payload = client.list_skill_spaces()
        spaces = _parse_skill_spaces(payload)
        configured_set = set(configured)
        for space in spaces:
            space["configured"] = space["space_id"] in configured_set
        if not spaces:
            # 服务不支持 ListSkillSpaces 时,退化为返回已配置的 space id。
            spaces = [
                {"space_id": sid, "space_name": "", "description": "", "configured": True}
                for sid in configured
            ]
        return {
            "ok": True,
            "configured_space_ids": configured,
            "spaces": spaces,
            "usage": (
                "根据用户意图从这些 space 中选一个 space_id,再用 "
                "search_skills(query, space_id=...) 在该 space 内搜索,或用 "
                "load_skill(skill_name, space_id=...) 精确加载该 space 下的 skill。"
            ),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "configured_space_ids": configured,
            "spaces": [
                {"space_id": sid, "space_name": "", "description": "", "configured": True}
                for sid in configured
            ],
        }


def _parse_skill_spaces(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("Data") or payload.get("data") or {}
    raw = (
        data.get("SkillSpaces")
        or data.get("skill_spaces")
        or data.get("Spaces")
        or data.get("spaces")
        or data.get("Items")
        or data.get("items")
        or []
    )
    spaces: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        spaces.append(
            {
                "space_id": str(
                    item.get("SkillSpaceId")
                    or item.get("skill_space_id")
                    or item.get("SpaceId")
                    or item.get("space_id")
                    or ""
                ),
                "space_name": str(
                    item.get("SkillSpaceName")
                    or item.get("skill_space_name")
                    or item.get("SpaceName")
                    or item.get("space_name")
                    or ""
                ),
                "description": str(item.get("Description") or item.get("description") or ""),
            }
        )
    return spaces


def list_skills(space_id: str | None = None) -> dict[str, Any]:
    """List skills discoverable from configured Skill Spaces, optionally limited to one space.

    ``space_id`` 指定时只列该 space(user space id 或 ``"public"``);缺省列出全部已配置 space。
    """

    space_ids = [space_id] if space_id else resolve_skill_space_ids()
    try:
        if space_id:
            client = _skill_service_client()
            manifests = [
                _skill_manifest_item(skill, space_id=sid)
                for sid, skill in _iter_remote_skills(client, space_id=space_id)
                if skill.name
            ]
        else:
            manifests = load_remote_skill_manifests(space_ids)
        return {
            "ok": True,
            "skills": manifests,
            "skill_space_ids": space_ids,
            "space_id": space_id,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "skill_space_ids": space_ids,
            "space_id": space_id,
        }


def load_skill(skill_name: str, space_id: str | None = None) -> dict[str, Any]:
    """Download and load a skill's SKILL.md instructions from configured Skill Spaces.

    ``space_id`` 指定时只在该 space 内按名匹配,用于多 space 下精确定位、避免同名冲突;
    缺省按配置顺序(user space 优先于 public)取第一个匹配。
    """

    target = str(skill_name or "").strip().lower()
    if not target:
        return {"ok": False, "error_message": "skill_name is required"}
    try:
        client = _skill_service_client()
        available: list[str] = []
        for sid, skill in _iter_remote_skills(client, space_id=space_id):
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
                "space_id": sid,
                "skill_id": skill.skill_id,
                "version_id": skill.version_id,
                "version": skill.version,
                "cache_hit": bool(getattr(package, "cache_hit", False)),
                "root_dir": str(local_skill.root_dir),
                "has_scripts_dir": (local_skill.root_dir / "scripts").is_dir(),
                "script_files": _script_files(local_skill.root_dir),
                "instructions": local_skill.body,
                "usage": (
                    "Read these instructions and complete the user's task in the outer "
                    "agent unless the skill explicitly requires isolated execution."
                ),
            }
        return {
            "ok": False,
            "error_message": f"Skill not found: {skill_name}",
            "space_id": space_id,
            "available_skills": available,
        }
    except Exception as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error_message": str(exc)}


def search_skills(query: str, max_results: int = 10, space_id: str | None = None) -> dict[str, Any]:
    """Search skills by name, aliases, tags, description, and examples.

    ``space_id`` 指定时只在该 space 内搜索;缺省聚合全部已配置 space。结果带 ``space_id``,
    便于后续 ``load_skill(name, space_id=...)`` 精确定位。
    """

    query_text = str(query or "").strip()
    if not query_text:
        return {"ok": False, "error_message": "query is required"}
    try:
        client = _skill_service_client()
        pairs = list(_iter_remote_skills(client, space_id=space_id))
        # 建立 skill 标识 -> space_id 映射,给结果回填来源 space(cache_key 与 match 去重一致)。
        space_of: dict[str, str] = {}
        for sid, skill in pairs:
            space_of.setdefault(skill.cache_key or skill.skill_id or skill.name, sid)
        refs = [skill for _, skill in pairs]
        matches = match_skill_refs(refs, query_text, limit=max_results)
        results = []
        for match in matches:
            item = match.to_dict()
            item["space_id"] = space_of.get(
                match.skill.cache_key or match.skill.skill_id or match.skill.name, ""
            )
            results.append(item)
        return {
            "ok": True,
            "query": query,
            "space_id": space_id,
            "results": results,
        }
    except Exception as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error_message": str(exc)}


def execute_skills(
    workflow_prompt: str,
    skill_names: list[str] | str | None = None,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a workflow through the configured Skill Runtime."""

    return _dict_result(
        _gateway().invoke(
            "execute_skills",
            _execute_skills_impl,
            workflow_prompt,
            skill_names,
            approval=approval,
        ),
        tool_name="execute_skills",
    )


def _execute_skills_impl(
    workflow_prompt: str, skill_names: list[str] | str | None = None
) -> dict[str, Any]:
    try:
        backend = create_skill_runtime_backend()
        raw_tool = build_runtime_execute_skills_tool(backend=backend)
        result = raw_tool(workflow_prompt, skill_names)
        if not isinstance(result, dict):
            return {
                "ok": False,
                "error_type": "invalid_skill_runtime_result",
                "error_message": (f"skill runtime returned {type(result).__name__}, expected dict"),
            }
        result.setdefault("execution_context", f"skill-runtime/{_skill_execution_backend()}")
        return dict(result)
    except SkillRuntimeError as exc:
        return {
            "ok": False,
            "error_type": "skill_runtime_disabled",
            "error_message": str(exc),
            "hint": (
                "Configure KSADK_SKILL_RUNTIME_BACKEND=local_process/e2b or "
                "KSADK_SANDBOX_TEMPLATE_ID for isolated Skill workflow execution."
            ),
        }
    except Exception as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error_message": str(exc)}


def get_skill_tools() -> list:
    return [
        as_tool(list_skills),
        as_tool(list_skill_spaces),
        as_tool(search_skills),
        as_tool(load_skill),
        as_tool(execute_skills),
    ]


def _skill_execution_backend() -> str:
    explicit = os.environ.get("KSADK_SKILL_RUNTIME_BACKEND", "").strip().lower()
    if explicit:
        return explicit
    if os.environ.get("KSADK_SANDBOX_TEMPLATE_ID") or os.environ.get(
        "KSADK_SKILL_RUNTIME_TEMPLATE_ID"
    ):
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


def _iter_space_skills(client: SkillServiceClient, space_id: str):
    """遍历单个 space 的 active skill;space_id="public" 走 premade 列表。"""

    if space_id == "public":
        listing = client.list_available_premade_skills()
        for skill in select_public_skill_refs(listing.active_skills()):
            yield listing.space_id or "public", skill
    else:
        listing = client.list_skills_by_space_id(space_id)
        for skill in listing.active_skills():
            yield space_id, skill


def _iter_remote_skills(client: SkillServiceClient, space_id: str | None = None):
    """遍历配置的 space;space_id 指定时只遍历该 space(user 或 "public")。"""

    if space_id:
        yield from _iter_space_skills(client, space_id)
        return
    for user_space_id in user_skill_space_ids():
        yield from _iter_space_skills(client, user_space_id)
    if public_skill_space_ids():
        yield from _iter_space_skills(client, "public")


def _get_or_download_package(client: SkillServiceClient, skill):
    cache_dir = Path(
        os.environ.get("KSADK_SKILL_CACHE_DIR") or Path(tempfile.gettempdir()) / "ksadk-skill-cache"
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
        str(path.relative_to(root_dir)) for path in sorted(scripts_dir.rglob("*")) if path.is_file()
    ][:20]
