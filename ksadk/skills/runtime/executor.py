from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ksadk.skills.events import SKILL_EVENT_FILE_ENV, SkillEvent, SkillEventSink
from ksadk.skills.loader import LocalSkill
from ksadk.skills.models import SkillRef
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
    event_sink: SkillEventSink | None = None,
    skill_refs: dict[str, SkillRef] | None = None,
    skill_invocation_ids: dict[str, str] | None = None,
) -> WorkflowExecution:
    selected = normalize_skill_names(selected_skill_names)
    loaded = [skill.name for skill in skills]
    if not skills:
        return WorkflowExecution(status="no_skills", selected_skills=selected, loaded_skills=loaded)

    for skill in _candidate_skills(skills, selected):
        if _can_run_web_artifacts_builder(skill, prompt):
            invocation_id, skill_ref = _execution_identity(skill, skill_refs, skill_invocation_ids)
            _emit_execution(
                event_sink, "skill.execution.started", "running", skill_ref, invocation_id
            )
            result = _run_web_artifacts_builder(skill)
            _attach_context(result, selected=selected, loaded=loaded)
            _emit_execution_result(event_sink, result, skill_ref, invocation_id)
            return result

    for skill in _candidate_skills(skills, selected):
        if _can_run_generic_workflow(skill):
            invocation_id, skill_ref = _execution_identity(skill, skill_refs, skill_invocation_ids)
            _emit_execution(
                event_sink, "skill.execution.started", "running", skill_ref, invocation_id
            )
            result = _run_generic_workflow(skill, prompt)
            _attach_context(result, selected=selected, loaded=loaded)
            _emit_execution_result(event_sink, result, skill_ref, invocation_id)
            return result

    for skill in _candidate_skills(skills, selected):
        invocation_id, skill_ref = _execution_identity(skill, skill_refs, skill_invocation_ids)
        _emit_execution(
            event_sink, "skill.execution.completed", "skipped", skill_ref, invocation_id
        )
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


def _execution_identity(
    skill: LocalSkill,
    skill_refs: dict[str, SkillRef] | None,
    skill_invocation_ids: dict[str, str] | None,
) -> tuple[str, SkillRef | None]:
    skill_ref = (skill_refs or {}).get(skill.name)
    invocation_id = (skill_invocation_ids or {}).get(skill.name) or f"skill_inv_{uuid.uuid4().hex}"
    return invocation_id, skill_ref


def _emit_execution(
    event_sink: SkillEventSink | None,
    event_type: str,
    status: str,
    skill_ref: SkillRef | None,
    invocation_id: str,
    *,
    error_category: str = "",
) -> None:
    if event_sink is not None:
        event_sink.emit(
            SkillEvent.create(
                event_type,
                status=status,
                skill_ref=skill_ref,
                skill_invocation_id=invocation_id,
                error_category=error_category,
            )
        )


def _emit_execution_result(
    event_sink: SkillEventSink | None,
    result: WorkflowExecution,
    skill_ref: SkillRef | None,
    invocation_id: str,
) -> None:
    status = "completed" if result.status == "ok" else "failed"
    error_category = (
        "timeout" if any(command.get("timed_out") for command in result.commands) else ""
    )
    _emit_execution(
        event_sink,
        f"skill.execution.{status}",
        status,
        skill_ref,
        invocation_id,
        error_category=error_category,
    )
    if event_sink is None:
        return
    for index, artifact in enumerate(result.artifacts):
        artifact_path = Path(artifact)
        event_sink.emit(
            SkillEvent.create(
                "skill.artifact.created",
                status="completed",
                skill_ref=skill_ref,
                skill_invocation_id=invocation_id,
                attributes={
                    "artifact_ref": f"{invocation_id}:{index}",
                    "artifact_name": artifact_path.name,
                    "size_bytes": artifact_path.stat().st_size if artifact_path.exists() else 0,
                },
            )
        )


def _can_run_web_artifacts_builder(skill: LocalSkill, prompt: str) -> bool:
    if skill.name != "web-artifacts-builder":
        return False
    init_script = skill.root_dir / "scripts" / "init-artifact.sh"
    bundle_script = skill.root_dir / "scripts" / "bundle-artifact.sh"
    if not init_script.exists() or not bundle_script.exists():
        return False
    normalized = prompt.lower()
    return any(
        marker in normalized
        for marker in ("web-artifacts-builder", "artifact", "bundle", "html", "react")
    )


def _can_run_generic_workflow(skill: LocalSkill) -> bool:
    return (skill.root_dir / "scripts" / "run-workflow.sh").exists()


def _run_web_artifacts_builder(skill: LocalSkill) -> WorkflowExecution:
    workdir = _skill_workdir()
    project_name = _safe_project_name(
        os.environ.get("KSADK_SKILL_ARTIFACT_PROJECT") or "ksadk-artifact"
    )
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
    output_files = (
        [str(project_dir / "bundle.html")] if (project_dir / "bundle.html").exists() else []
    )
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
    env.pop(SKILL_EVENT_FILE_ENV, None)
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
