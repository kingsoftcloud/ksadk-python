from __future__ import annotations

import os
from uuid import uuid4

from ksadk.skills.loader import LocalSkill
from ksadk.skills.runtime.base import SkillRuntimeBackend

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
)


def resolve_skill_space_ids() -> list[str]:
    raw = os.environ.get("KSADK_SKILL_SPACE_IDS") or os.environ.get("SKILL_SPACE_ID") or ""
    return [part.strip() for part in raw.split(",") if part.strip()]


def runtime_agent_env_from_process() -> dict[str, str]:
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
    return env


def build_execute_skills_tool(
    *,
    backend: SkillRuntimeBackend,
    skill_space_ids: list[str] | None = None,
    session_id: str | None = None,
):
    spaces = list(skill_space_ids or resolve_skill_space_ids())
    default_session_id = session_id or f"ksadk-{uuid4().hex}"

    def execute_skills(workflow_prompt: str) -> dict:
        """Execute a workflow with the configured Skill Runtime."""

        result = backend.run_workflow(
            workflow_prompt,
            skill_space_ids=spaces,
            session_id=default_session_id,
            env=runtime_agent_env_from_process(),
            timeout=int(os.environ.get("KSADK_SKILL_RUNTIME_TIMEOUT", "900")),
        )
        return result.to_dict()

    execute_skills.__name__ = "execute_skills"
    return execute_skills


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
