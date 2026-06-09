from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import httpx

from ksadk.skills.loader import LocalSkill
from ksadk.skills.models import SkillRef
from ksadk.skills.package_store import PackageStore
from ksadk.skills.runtime.base import normalize_skill_names
from ksadk.skills.runtime.executor import (
    WorkflowExecution,
    execute_workflow,
    _can_run_web_artifacts_builder as _executor_can_run_web_artifacts_builder,
    _run_command as _executor_run_command,
    _run_web_artifacts_builder as _executor_run_web_artifacts_builder,
    _runtime_timeout as _executor_runtime_timeout,
    _safe_project_name as _executor_safe_project_name,
    _tail as _executor_tail,
)
from ksadk.skills.runtime.request import (
    SkillWorkflowRequest,
    SkillWorkflowRequestError,
    parse_workflow_request,
)
from ksadk.skills.runtime import loader as runtime_loader
from ksadk.skills.runtime import registry as runtime_registry


def _skill_space_ids() -> list[str]:
    return runtime_registry.skill_space_ids()


def _user_skill_space_ids() -> list[str]:
    return runtime_registry.user_skill_space_ids()


def _public_skill_space_ids() -> list[str]:
    return runtime_registry.public_skill_space_ids()


def _parse_space_ids(*raw_values: str) -> list[str]:
    return runtime_registry.parse_space_ids(*raw_values)


_SKILL_LOAD_WARNINGS: list[str] = []


def _load_skills(
    *,
    prompt: str = "",
    skill_names: list[str] | None = None,
    service_transport: httpx.BaseTransport | None = None,
) -> list[LocalSkill]:
    result = runtime_loader.load_skills(
        prompt=prompt,
        skill_names=skill_names,
        service_transport=service_transport,
    )
    _SKILL_LOAD_WARNINGS.extend(result.warnings)
    return result.skills


def _dedupe_skill_refs(skill_refs: list[SkillRef], *, seen_names: set[str]) -> list[SkillRef]:
    return runtime_registry.dedupe_skill_refs(skill_refs, seen_names=seen_names)


def _select_public_skill_refs(skill_refs: list[SkillRef]) -> list[SkillRef]:
    return runtime_registry.select_public_skill_refs(skill_refs)


def _allow_hash_mismatch() -> bool:
    return runtime_loader._allow_hash_mismatch()


def _store_unverified_archive(store: PackageStore, skill: SkillRef, archive: bytes):
    return runtime_loader._store_unverified_archive(store, skill, archive)


def _select_remote_skill_refs(
    skill_refs: list[SkillRef],
    prompt: str,
    *,
    skill_names: list[str] | None = None,
) -> list[SkillRef]:
    return runtime_registry.select_remote_skill_refs(skill_refs, prompt, skill_names=skill_names)


def _load_local_skills() -> list[LocalSkill]:
    return runtime_loader.load_local_skills()


def run_agent(
    argv: list[str] | None = None,
    *,
    service_transport: httpx.BaseTransport | None = None,
) -> int:
    _SKILL_LOAD_WARNINGS.clear()
    args = list(argv if argv is not None else sys.argv[1:])
    try:
        request = _resolve_request(args)
    except SkillWorkflowRequestError as exc:
        execution = WorkflowExecution(status="failed", error=str(exc))
        print("workflow=")
        print(f"skill_spaces={','.join(_skill_space_ids())}")
        print("loaded_skills=")
        print(f"workflow_result={json.dumps(asdict(execution), ensure_ascii=False, sort_keys=True)}")
        return 1

    prompt = request.workflow_prompt
    selected_skill_names = request.skill_names or _selected_skill_names()
    loaded_skills = _load_skills(
        prompt=prompt,
        skill_names=selected_skill_names,
        service_transport=service_transport,
    )
    execution = _execute_workflow(prompt, loaded_skills, selected_skill_names=selected_skill_names)
    print(f"workflow={prompt}")
    print(f"skill_spaces={','.join(_skill_space_ids())}")
    print(f"loaded_skills={','.join(skill.name for skill in loaded_skills)}")
    print(f"selected_skills={json.dumps(selected_skill_names, ensure_ascii=False, sort_keys=True)}")
    if _SKILL_LOAD_WARNINGS:
        print(f"skill_warnings={json.dumps(_SKILL_LOAD_WARNINGS, ensure_ascii=False, sort_keys=True)}")
    print(f"workflow_result={json.dumps(asdict(execution), ensure_ascii=False, sort_keys=True)}")
    return 0 if execution.status != "failed" else 1


def _resolve_request(args: list[str]) -> SkillWorkflowRequest:
    return parse_workflow_request(args)


def _selected_skill_names() -> list[str]:
    return normalize_skill_names(os.environ.get("KSADK_SELECTED_SKILL_NAMES", ""))


def _execute_workflow(
    prompt: str,
    skills: list[LocalSkill],
    *,
    selected_skill_names: list[str] | None = None,
) -> WorkflowExecution:
    return execute_workflow(prompt, skills, selected_skill_names=selected_skill_names)


def _can_run_web_artifacts_builder(skill: LocalSkill, prompt: str) -> bool:
    return _executor_can_run_web_artifacts_builder(skill, prompt)


def _run_web_artifacts_builder(skill: LocalSkill) -> WorkflowExecution:
    return _executor_run_web_artifacts_builder(skill)


def _run_command(args: list[str], *, cwd: Path, timeout: int) -> dict[str, object]:
    return _executor_run_command(args, cwd=cwd, timeout=timeout)


def _runtime_timeout() -> int:
    return _executor_runtime_timeout()


def _safe_project_name(raw: str) -> str:
    return _executor_safe_project_name(raw)


def _tail(value: str | bytes, limit: int = 4000) -> str:
    return _executor_tail(value, limit=limit)


def _resolve_prompt(args: list[str]) -> str:
    return _resolve_request(args).workflow_prompt


def main(argv: list[str] | None = None) -> int:
    return run_agent(argv)


if __name__ == "__main__":
    raise SystemExit(main())
