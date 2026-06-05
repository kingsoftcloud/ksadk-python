from __future__ import annotations

import os
from uuid import uuid4

from ksadk.skills.loader import LocalSkill
from ksadk.skills.models import SkillRef
from ksadk.skills.runtime.base import SkillRuntimeBackend, normalize_skill_names
from ksadk.skills.service_client import SkillServiceClient
from ksadk.skills.service_env import (
    parse_skill_space_ids,
    public_skill_space_ids,
    resolve_skill_service_url,
    should_resolve_child_skill_service_url,
    skill_space_ids as configured_skill_space_ids,
    user_skill_space_ids,
)

RUNTIME_AGENT_ENV_NAMES = (
    "KSADK_SKILL_SERVICE_URL",
    "KSADK_SKILL_SERVICE_TOKEN",
    "KSADK_SKILL_SERVICE_ACCESS_KEY",
    "KSADK_SKILL_SERVICE_SECRET_KEY",
    "KSADK_SKILL_SERVICE_ACCOUNT_ID",
    "KSADK_SKILL_SERVICE_REGION",
    "KSADK_SKILL_SERVICE_API_VERSION",
    "KSADK_SKILL_SERVICE_SIGN_SERVICE",
    "KSADK_SKILL_CACHE_DIR",
    "KSADK_SKILL_WORKDIR",
    "KSADK_SKILL_ARTIFACT_PROJECT",
    "KSADK_PUBLIC_SKILL_SPACE_IDS",
    "KSADK_PUBLIC_SKILL_ALLOWLIST",
)

_SKILL_SERVICE_SECRET_FALLBACKS = {
    "KSADK_SKILL_SERVICE_ACCESS_KEY": ("KSYUN_ACCESS_KEY", "KS3_ACCESS_KEY"),
    "KSADK_SKILL_SERVICE_SECRET_KEY": ("KSYUN_SECRET_KEY", "KS3_SECRET_KEY"),
}


def resolve_skill_space_ids() -> list[str]:
    return configured_skill_space_ids()


def resolve_user_skill_space_ids() -> list[str]:
    return user_skill_space_ids()


def _parse_skill_space_ids(*raw_values: str) -> list[str]:
    return parse_skill_space_ids(*raw_values)


def runtime_agent_env_from_process(skill_space_ids: list[str] | None = None) -> dict[str, str]:
    env = {
        name: value
        for name in RUNTIME_AGENT_ENV_NAMES
        if (value := os.environ.get(name))
    }
    if "KSADK_SKILL_SERVICE_ACCOUNT_ID" not in env and (
        account_id := os.environ.get("KSYUN_ACCOUNT_ID")
    ):
        env["KSADK_SKILL_SERVICE_ACCOUNT_ID"] = account_id
    if "KSADK_SKILL_SERVICE_REGION" not in env and (region := os.environ.get("KSYUN_REGION")):
        env["KSADK_SKILL_SERVICE_REGION"] = region
    has_spaces = bool(skill_space_ids) or bool(resolve_skill_space_ids())
    if has_spaces:
        for target, sources in _SKILL_SERVICE_SECRET_FALLBACKS.items():
            if target in env:
                continue
            for source in sources:
                value = os.environ.get(source, "").strip()
                if value:
                    env[target] = value
                    break
    if has_spaces and should_resolve_child_skill_service_url() and "KSADK_SKILL_SERVICE_URL" not in env:
        service_url = resolve_skill_service_url(require_spaces=False)
        if service_url:
            env["KSADK_SKILL_SERVICE_URL"] = service_url
    return env


def build_execute_skills_tool(
    *,
    backend: SkillRuntimeBackend,
    skill_space_ids: list[str] | None = None,
    session_id: str | None = None,
):
    spaces = list(skill_space_ids or resolve_user_skill_space_ids())
    default_session_id = session_id or f"ksadk-{uuid4().hex}"

    def execute_skills(workflow_prompt: str, skill_names: list[str] | str | None = None) -> dict:
        """Execute a workflow with the configured Skill Runtime."""

        result = backend.run_workflow(
            workflow_prompt,
            skill_space_ids=spaces,
            skill_names=normalize_skill_names(skill_names),
            session_id=default_session_id,
            env=runtime_agent_env_from_process(spaces),
            timeout=int(os.environ.get("KSADK_SKILL_RUNTIME_TIMEOUT", "900")),
        )
        return result.to_dict()

    execute_skills.__name__ = "execute_skills"
    return execute_skills


def load_remote_skill_manifests(skill_space_ids: list[str] | None = None) -> list[dict[str, str]]:
    spaces = list(skill_space_ids or resolve_skill_space_ids())
    public_spaces = set(public_skill_space_ids())
    user_spaces = [space_id for space_id in spaces if space_id not in public_spaces]
    if not user_spaces and not public_spaces:
        return []

    service_url = resolve_skill_service_url(require_spaces=True)
    if not service_url:
        return []

    client = SkillServiceClient(
        base_url=service_url,
        token=os.environ.get("KSADK_SKILL_SERVICE_TOKEN", ""),
        timeout=float(os.environ.get("KSADK_SKILL_MANIFEST_TIMEOUT", "5")),
    )
    manifests: list[dict[str, str]] = []
    seen: set[str] = set()
    limit = _manifest_limit()
    public_allowlist = {
        name.lower() for name in normalize_skill_names(os.environ.get("KSADK_PUBLIC_SKILL_ALLOWLIST", ""))
    }
    for space_id in user_spaces:
        listing = client.list_skills_by_space_id(space_id)
        for skill in listing.active_skills():
            item = _skill_manifest_item(skill, space_id=space_id)
            name_key = item["name"].lower()
            if not item["name"] or name_key in seen:
                continue
            seen.add(name_key)
            manifests.append(item)
            if len(manifests) >= limit:
                return manifests
    if public_spaces:
        listing = client.list_available_premade_skills()
        for skill in listing.active_skills():
            if public_allowlist and skill.name.lower() not in public_allowlist:
                continue
            item = _skill_manifest_item(skill, space_id=listing.space_id or "public")
            name_key = item["name"].lower()
            if not item["name"] or name_key in seen:
                continue
            seen.add(name_key)
            manifests.append(item)
            if len(manifests) >= limit:
                return manifests
    return manifests


def build_skill_manifest_instruction(manifests: list[dict[str, str]]) -> str:
    if not manifests:
        return ""

    lines = [
        "",
        "Available remote skills are listed below. Use them only when they match the user's task.",
        "When a skill is useful, call execute_skills with the original workflow_prompt and skill_names set to the exact skill name.",
        "Do not assume full skill instructions are already loaded; execute_skills loads selected skills on demand.",
        "",
        "Remote skills:",
    ]
    for item in manifests:
        description = item.get("description") or "No description"
        version = item.get("version") or ""
        suffix = f" ({version})" if version else ""
        lines.append(f"- {item['name']}{suffix}: {description}")
    return "\n".join(lines)


def _skill_manifest_item(skill: SkillRef, *, space_id: str) -> dict[str, str]:
    item = {
        "name": str(skill.name or "").strip(),
        "description": _single_line(skill.description),
        "version": str(skill.version or "").strip(),
        "space_id": str(space_id or "").strip(),
    }
    if skill.aliases:
        item["aliases"] = _single_line(", ".join(skill.aliases))
    if skill.tags:
        item["tags"] = _single_line(", ".join(skill.tags))
    return item


def _single_line(value: str, *, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].rstrip()


def _manifest_limit() -> int:
    try:
        return max(1, int(os.environ.get("KSADK_SKILL_MANIFEST_LIMIT", "30")))
    except ValueError:
        return 30


def build_skills_tool(skills: list[LocalSkill]):
    def skills_tool(action: str = "list") -> dict:
        """List locally loaded skills and their instructions."""

        return {
            "action": action,
            "skills": [
                {
                    "name": skill.name,
                    "description": skill.description,
                    "body": skill.body,
                    "root_dir": str(skill.root_dir),
                }
                for skill in skills
            ],
        }

    skills_tool.__name__ = "skills_tool"
    return skills_tool
