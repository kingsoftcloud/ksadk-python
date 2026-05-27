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
from ksadk.skills.models import SkillRef
from ksadk.skills.package_store import PackageStore, SkillPackageError
from ksadk.skills.runtime.base import normalize_skill_names
from ksadk.skills.service_client import SkillServiceClient


def _skill_space_ids() -> list[str]:
    user_raw = os.environ.get("KSADK_SKILL_SPACE_IDS") or os.environ.get("SKILL_SPACE_ID") or ""
    public_raw = os.environ.get("KSADK_PUBLIC_SKILL_SPACE_IDS") or ""
    return _parse_space_ids(user_raw, public_raw)


def _user_skill_space_ids() -> list[str]:
    return _parse_space_ids(os.environ.get("KSADK_SKILL_SPACE_IDS") or os.environ.get("SKILL_SPACE_ID") or "")


def _public_skill_space_ids() -> list[str]:
    return _parse_space_ids(os.environ.get("KSADK_PUBLIC_SKILL_SPACE_IDS") or "")


def _parse_space_ids(*raw_values: str) -> list[str]:
    spaces: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        for part in raw.split(","):
            space_id = part.strip()
            if not space_id or space_id in seen:
                continue
            seen.add(space_id)
            spaces.append(space_id)
    return spaces


@dataclass
class WorkflowExecution:
    status: str
    executed_skill: str = ""
    output_files: list[str] = field(default_factory=list)
    commands: list[dict[str, object]] = field(default_factory=list)


_SKILL_LOAD_WARNINGS: list[str] = []


def _load_skills(
    *,
    prompt: str = "",
    skill_names: list[str] | None = None,
    service_transport: httpx.BaseTransport | None = None,
) -> list[LocalSkill]:
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
    selected_refs: list[SkillRef] = []
    seen_names: set[str] = {skill.name.lower() for skill in skills if skill.name}
    for space_id in _user_skill_space_ids():
        listing = client.list_skills_by_space_id(space_id)
        selected_refs.extend(_dedupe_skill_refs(
            _select_remote_skill_refs(
                listing.active_skills(),
                prompt,
                skill_names=skill_names,
            ),
            seen_names=seen_names,
        ))

    for space_id in _public_skill_space_ids():
        listing = client.list_skills_by_space_id(space_id)
        selected_refs.extend(_dedupe_skill_refs(
            _select_public_skill_refs(listing.active_skills()),
            seen_names=seen_names,
        ))

    for skill in selected_refs:
        package = store.get_cached(skill)
        if package is None:
            archive = client.download_skill_archive(skill)
            try:
                package = store.store_archive(skill, archive)
            except SkillPackageError as exc:
                if not _allow_hash_mismatch():
                    raise
                package = _store_unverified_archive(store, skill, archive)
                _SKILL_LOAD_WARNINGS.append(str(exc))
        skills.append(load_local_skill(package.root_dir))
    return skills


def _dedupe_skill_refs(skill_refs: list[SkillRef], *, seen_names: set[str]) -> list[SkillRef]:
    selected: list[SkillRef] = []
    seen_keys: set[str] = set()
    for skill in skill_refs:
        name_key = skill.name.lower() if skill.name else ""
        key = skill.cache_key or skill.skill_id or skill.name
        if not name_key or name_key in seen_names or key in seen_keys:
            continue
        seen_names.add(name_key)
        seen_keys.add(key)
        selected.append(skill)
    return selected


def _select_public_skill_refs(skill_refs: list[SkillRef]) -> list[SkillRef]:
    allowlist = {name.lower() for name in normalize_skill_names(os.environ.get("KSADK_PUBLIC_SKILL_ALLOWLIST", ""))}
    if not allowlist:
        return [skill for skill in skill_refs if skill.name]
    return [
        skill
        for skill in skill_refs
        if skill.name and skill.name.lower() in allowlist
    ]


def _allow_hash_mismatch() -> bool:
    return os.environ.get("KSADK_SKILL_ALLOW_HASH_MISMATCH", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _store_unverified_archive(store: PackageStore, skill: SkillRef, archive: bytes):
    skill_dir = store.cache_dir / f"unverified-{skill.cache_key or skill.name or 'skill'}"
    if skill_dir.exists():
        shutil.rmtree(skill_dir)
    skill_dir.mkdir(parents=True, exist_ok=True)
    archive_path = skill_dir / "archive.zip"
    extract_dir = skill_dir / "extracted"
    archive_path.write_bytes(archive)
    extract_dir.mkdir(parents=True, exist_ok=True)
    store._safe_extract(archive_path, extract_dir)
    return type("UnverifiedSkillPackage", (), {
        "ref": skill,
        "archive_path": archive_path,
        "extract_dir": extract_dir,
        "root_dir": store._find_skill_root(extract_dir),
        "cache_hit": False,
    })()


def _select_remote_skill_refs(
    skill_refs: list[SkillRef],
    prompt: str,
    *,
    skill_names: list[str] | None = None,
) -> list[SkillRef]:
    requested_names = {name.lower() for name in normalize_skill_names(skill_names)}
    if not prompt and not requested_names:
        return []
    normalized_prompt = prompt.lower()
    selected: list[SkillRef] = []
    seen: set[str] = set()
    for skill in skill_refs:
        if not skill.name:
            continue
        skill_name = skill.name.lower()
        if requested_names:
            if skill_name not in requested_names:
                continue
        elif skill_name not in normalized_prompt:
            continue
        key = skill.cache_key or skill.skill_id or skill.name
        if key in seen:
            continue
        seen.add(key)
        selected.append(skill)
    return selected


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
    _SKILL_LOAD_WARNINGS.clear()
    args = list(argv if argv is not None else sys.argv[1:])
    prompt = _resolve_prompt(args)
    selected_skill_names = _selected_skill_names()
    loaded_skills = _load_skills(
        prompt=prompt,
        skill_names=selected_skill_names,
        service_transport=service_transport,
    )
    execution = _execute_workflow(prompt, loaded_skills)
    print(f"workflow={prompt}")
    print(f"skill_spaces={','.join(_skill_space_ids())}")
    print(f"loaded_skills={','.join(skill.name for skill in loaded_skills)}")
    if _SKILL_LOAD_WARNINGS:
        print(f"skill_warnings={json.dumps(_SKILL_LOAD_WARNINGS, ensure_ascii=False, sort_keys=True)}")
    print(f"workflow_result={json.dumps(asdict(execution), sort_keys=True)}")
    return 0 if execution.status != "failed" else 1


def _selected_skill_names() -> list[str]:
    return normalize_skill_names(os.environ.get("KSADK_SELECTED_SKILL_NAMES", ""))


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
