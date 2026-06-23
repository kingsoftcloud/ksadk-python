from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ksadk.skills.loader import LocalSkill
from ksadk.skills.runtime.artifacts import (
    collect_output_dir_artifacts,
    merge_artifacts,
    parse_artifact_lines,
)
from ksadk.skills.runtime.base import normalize_skill_names


@dataclass
class WorkflowExecution:
    status: str
    executed_skill: str = ""
    output_files: list[str] = field(default_factory=list)
    commands: list[dict[str, object]] = field(default_factory=list)
    selected_skills: list[str] = field(default_factory=list)
    loaded_skills: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""


def execute_workflow(
    prompt: str,
    skills: list[LocalSkill],
    *,
    selected_skill_names: list[str] | None = None,
) -> WorkflowExecution:
    selected = normalize_skill_names(selected_skill_names)
    loaded = [skill.name for skill in skills]
    if not skills:
        return WorkflowExecution(status="no_skills", selected_skills=selected, loaded_skills=loaded)

    for skill in _candidate_skills(skills, selected):
        if _can_run_web_artifacts_builder(skill, prompt):
            result = _run_web_artifacts_builder(skill)
            _attach_context(result, selected=selected, loaded=loaded)
            return result

    for skill in _candidate_skills(skills, selected):
        if _can_run_generic_workflow(skill):
            result = _run_generic_workflow(skill, prompt)
            _attach_context(result, selected=selected, loaded=loaded)
            return result

    return WorkflowExecution(
        status="skipped",
        selected_skills=selected,
        loaded_skills=loaded,
        warnings=["No loaded skill exposes an executable workflow entrypoint."],
    )


def _candidate_skills(skills: list[LocalSkill], selected: list[str]) -> list[LocalSkill]:
    if not selected:
        return skills
    selected_keys = {name.lower() for name in selected}
    return [skill for skill in skills if skill.name.lower() in selected_keys]


def _attach_context(result: WorkflowExecution, *, selected: list[str], loaded: list[str]) -> None:
    result.selected_skills = selected
    result.loaded_skills = loaded
    if not result.artifacts:
        result.artifacts = list(result.output_files)


def _can_run_web_artifacts_builder(skill: LocalSkill, prompt: str) -> bool:
    if skill.name != "web-artifacts-builder":
        return False
    init_script = skill.root_dir / "scripts" / "init-artifact.sh"
    bundle_script = skill.root_dir / "scripts" / "bundle-artifact.sh"
    if not init_script.exists() or not bundle_script.exists():
        return False
    normalized = prompt.lower()
    return any(marker in normalized for marker in ("web-artifacts-builder", "artifact", "bundle", "html", "react"))


def _can_run_generic_workflow(skill: LocalSkill) -> bool:
    return (skill.root_dir / "scripts" / "run-workflow.sh").exists()


def _run_web_artifacts_builder(skill: LocalSkill) -> WorkflowExecution:
    workdir = _skill_workdir()
    project_name = _safe_project_name(os.environ.get("KSADK_SKILL_ARTIFACT_PROJECT") or "ksadk-artifact")
    project_dir = workdir / project_name
    workdir.mkdir(parents=True, exist_ok=True)
    if project_dir.exists():
        shutil.rmtree(project_dir)

    timeout = _runtime_timeout()
    commands: list[dict[str, object]] = []
    init_result = _run_command(
        ["bash", str(skill.root_dir / "scripts" / "init-artifact.sh"), project_name],
        cwd=workdir,
        timeout=timeout,
    )
    commands.append(init_result)
    if init_result["exit_code"] != 0:
        return WorkflowExecution(status="failed", executed_skill=skill.name, commands=commands)

    bundle_result = _run_command(
        ["bash", str(skill.root_dir / "scripts" / "bundle-artifact.sh")],
        cwd=project_dir,
        timeout=timeout,
    )
    commands.append(bundle_result)
    output_files = [str(project_dir / "bundle.html")] if (project_dir / "bundle.html").exists() else []
    status = "ok" if bundle_result["exit_code"] == 0 and output_files else "failed"
    return WorkflowExecution(
        status=status,
        executed_skill=skill.name,
        output_files=output_files,
        artifacts=list(output_files),
        commands=commands,
    )


def _run_generic_workflow(skill: LocalSkill, prompt: str) -> WorkflowExecution:
    workdir = _skill_workdir()
    workdir.mkdir(parents=True, exist_ok=True)
    output_dir = workdir / "artifacts"
    timeout = _runtime_timeout()
    command = _run_command(
        ["bash", str(skill.root_dir / "scripts" / "run-workflow.sh")],
        cwd=workdir,
        timeout=timeout,
        extra_env={
            "KSADK_WORKFLOW_PROMPT": prompt,
            "KSADK_SKILL_WORKDIR": str(workdir),
            "KSADK_SKILL_OUTPUT_DIR": str(output_dir),
            "KSADK_SKILL_ROOT_DIR": str(skill.root_dir),
        },
    )
    artifacts = merge_artifacts(
        parse_artifact_lines(str(command.get("stdout") or "")),
        collect_output_dir_artifacts(output_dir),
    )
    status = "ok" if command["exit_code"] == 0 else "failed"
    return WorkflowExecution(
        status=status,
        executed_skill=skill.name,
        output_files=list(artifacts),
        artifacts=list(artifacts),
        commands=[command],
    )


def _skill_workdir() -> Path:
    return Path(
        os.environ.get("KSADK_SKILL_WORKDIR")
        or Path(tempfile.gettempdir()) / "ksadk-skill-workflow"
    )


def _run_command(
    args: list[str],
    *,
    cwd: Path,
    timeout: int,
    extra_env: dict[str, str] | None = None,
) -> dict[str, object]:
    env = os.environ.copy()
    env.setdefault("CI", "1")
    env.update(extra_env or {})
    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=env,
        )
        return {
            "command": " ".join(args),
            "cwd": str(cwd),
            "exit_code": completed.returncode,
            "stdout": _tail(completed.stdout),
            "stderr": _tail(completed.stderr),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": " ".join(args),
            "cwd": str(cwd),
            "exit_code": None,
            "timed_out": True,
            "stdout": _tail(exc.stdout or ""),
            "stderr": _tail(exc.stderr or ""),
        }


def _runtime_timeout() -> int:
    try:
        return int(os.environ.get("KSADK_SKILL_RUNTIME_TIMEOUT", "900"))
    except ValueError:
        return 900


def _safe_project_name(raw: str) -> str:
    sanitized = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in raw).strip("-_")
    return sanitized[:80] or "ksadk-artifact"


def _tail(value: str | bytes, limit: int = 4000) -> str:
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    if len(text) <= limit:
        return text
    return text[-limit:]
