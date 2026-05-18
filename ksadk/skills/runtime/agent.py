from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from ksadk.skills.loader import LocalSkill, load_local_skill
from ksadk.skills.package_store import PackageStore
from ksadk.skills.service_client import SkillServiceClient


def _skill_space_ids() -> list[str]:
    raw = os.environ.get("KSADK_SKILL_SPACE_IDS") or os.environ.get("SKILL_SPACE_ID") or ""
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass
class WorkflowExecution:
    status: str
    executed_skill: str = ""
    output_files: list[str] = field(default_factory=list)
    commands: list[dict[str, object]] = field(default_factory=list)


def _load_skills(*, service_transport: httpx.BaseTransport | None = None) -> list[LocalSkill]:
    skills: list[LocalSkill] = []
    skills.extend(_load_local_skills())

    service_url = os.environ.get("KSADK_SKILL_SERVICE_URL", "").strip()
    if not service_url:
        return skills

    cache_dir = Path(
        os.environ.get("KSADK_SKILL_CACHE_DIR")
        or Path(tempfile.gettempdir()) / "ksadk-skill-cache"
    )
    client = SkillServiceClient(
        base_url=service_url,
        token=os.environ.get("KSADK_SKILL_SERVICE_TOKEN", ""),
        transport=service_transport,
    )
    store = PackageStore(cache_dir=cache_dir)
    for space_id in _skill_space_ids():
        listing = client.list_skills_by_space_id(space_id)
        for skill in listing.active_skills():
            package = store.get_cached(skill)
            if package is None:
                archive = client.download_skill_archive(skill)
                package = store.store_archive(skill, archive)
            skills.append(load_local_skill(package.root_dir))
    return skills


def _load_local_skills() -> list[LocalSkill]:
    skills_dir = os.environ.get("KSADK_LOCAL_SKILLS_DIR", "").strip()
    if not skills_dir:
        return []
    root = Path(skills_dir)
    if not root.exists():
        return []
    return [
        load_local_skill(path)
        for path in sorted(root.iterdir())
        if path.is_dir() and (path / "SKILL.md").exists()
    ]


def run_agent(
    argv: list[str] | None = None,
    *,
    service_transport: httpx.BaseTransport | None = None,
) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    prompt = _resolve_prompt(args)
    loaded_skills = _load_skills(service_transport=service_transport)
    execution = _execute_workflow(prompt, loaded_skills)
    print(f"workflow={prompt}")
    print(f"skill_spaces={','.join(_skill_space_ids())}")
    print(f"loaded_skills={','.join(skill.name for skill in loaded_skills)}")
    print(f"workflow_result={json.dumps(asdict(execution), sort_keys=True)}")
    return 0 if execution.status != "failed" else 1


def _execute_workflow(prompt: str, skills: list[LocalSkill]) -> WorkflowExecution:
    if not skills:
        return WorkflowExecution(status="no_skills")
    for skill in skills:
        if _can_run_web_artifacts_builder(skill, prompt):
            return _run_web_artifacts_builder(skill)
    return WorkflowExecution(status="skipped")


def _can_run_web_artifacts_builder(skill: LocalSkill, prompt: str) -> bool:
    if skill.name != "web-artifacts-builder":
        return False
    init_script = skill.root_dir / "scripts" / "init-artifact.sh"
    bundle_script = skill.root_dir / "scripts" / "bundle-artifact.sh"
    if not init_script.exists() or not bundle_script.exists():
        return False
    normalized = prompt.lower()
    return any(marker in normalized for marker in ("web-artifacts-builder", "artifact", "bundle", "html", "react"))


def _run_web_artifacts_builder(skill: LocalSkill) -> WorkflowExecution:
    workdir = Path(
        os.environ.get("KSADK_SKILL_WORKDIR")
        or Path(tempfile.gettempdir()) / "ksadk-skill-workflow"
    )
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
        commands=commands,
    )


def _run_command(args: list[str], *, cwd: Path, timeout: int) -> dict[str, object]:
    env = os.environ.copy()
    env.setdefault("CI", "1")
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


def _resolve_prompt(args: list[str]) -> str:
    if not args:
        return ""
    if args[0] == "--prompt-file" and len(args) > 1:
        return Path(args[1]).read_text(encoding="utf-8")
    return args[0]


def main(argv: list[str] | None = None) -> int:
    return run_agent(argv)


if __name__ == "__main__":
    raise SystemExit(main())
