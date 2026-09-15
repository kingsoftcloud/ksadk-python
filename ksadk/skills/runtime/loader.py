from __future__ import annotations

import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ksadk.skills.events import SkillEvent, SkillEventSink
from ksadk.skills.loader import LocalSkill, load_local_skill
from ksadk.skills.models import SkillRef
from ksadk.skills.package_store import PackageStore, SkillPackageError
from ksadk.skills.runtime import registry
from ksadk.skills.service_client import SkillServiceClient
from ksadk.skills.service_env import resolve_skill_service_url


@dataclass
class SkillLoadResult:
    skills: list[LocalSkill] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skill_refs: dict[str, SkillRef] = field(default_factory=dict)
    skill_invocation_ids: dict[str, str] = field(default_factory=dict)


def load_skills(
    *,
    prompt: str = "",
    skill_names: list[str] | None = None,
    service_transport: httpx.BaseTransport | None = None,
    event_sink: SkillEventSink | None = None,
    planned_invocations: dict[str, str] | None = None,
) -> SkillLoadResult:
    warnings: list[str] = []
    skills: list[LocalSkill] = []
    skills.extend(load_local_skills())

    service_url = resolve_skill_service_url(require_spaces=True)
    if not service_url:
        return SkillLoadResult(skills=skills, warnings=warnings)

    cache_dir = Path(
        os.environ.get("KSADK_SKILL_CACHE_DIR") or Path(tempfile.gettempdir()) / "ksadk-skill-cache"
    )
    client = SkillServiceClient(
        base_url=service_url,
        token=os.environ.get("KSADK_SKILL_SERVICE_TOKEN", ""),
        transport=service_transport,
    )
    store = PackageStore(cache_dir=cache_dir)
    selected_refs: list[SkillRef] = []
    seen_names: set[str] = {skill.name.lower() for skill in skills if skill.name}
    for space_id in registry.user_skill_space_ids():
        listing = client.list_skills_by_space_id(space_id)
        if listing.truncated:
            warnings.append(
                "Skill directory is incomplete; selection covers only retrieved entries"
            )
        selected_refs.extend(
            registry.dedupe_skill_refs(
                registry.select_remote_skill_refs(
                    listing.active_skills(allow_partial=True),
                    prompt,
                    skill_names=skill_names,
                ),
                seen_names=seen_names,
            )
        )

    if registry.public_skill_space_ids():
        listing = client.list_available_premade_skills()
        if listing.truncated:
            warnings.append(
                "Public Skill directory is incomplete; selection covers only retrieved entries"
            )
        selected_refs.extend(
            registry.dedupe_skill_refs(
                registry.select_public_skill_refs(listing.active_skills(allow_partial=True)),
                seen_names=seen_names,
            )
        )

    skill_refs: dict[str, SkillRef] = {}
    skill_invocation_ids: dict[str, str] = {}
    for skill in selected_refs:
        if planned_invocations is not None and skill.skill_id not in planned_invocations:
            continue
        invocation_id = (planned_invocations or {}).get(skill.skill_id)
        invocation_id = invocation_id or f"skill_inv_{uuid.uuid4().hex}"
        package = store.get_cached(skill)
        if package is None:
            downloaded_at = time.time()
            archive = client.download_skill_archive(skill)
            _emit(
                event_sink,
                "skill.package.downloaded",
                skill_ref=skill,
                skill_invocation_id=invocation_id,
                status="completed",
                started_at=downloaded_at,
                ended_at=time.time(),
            )
            try:
                package = store.store_archive(
                    skill,
                    archive,
                    event_sink=event_sink,
                    skill_invocation_id=invocation_id,
                )
            except SkillPackageError as exc:
                if not _allow_hash_mismatch():
                    raise
                package = _store_unverified_archive(store, skill, archive)
                warnings.append(str(exc))
        else:
            _emit(
                event_sink,
                "skill.package.cache_hit",
                skill_ref=skill,
                skill_invocation_id=invocation_id,
                status="completed",
                attributes={"cache_hit": True},
            )
        _emit(
            event_sink,
            "skill.load.started",
            skill_ref=skill,
            skill_invocation_id=invocation_id,
            status="running",
        )
        try:
            local_skill = load_local_skill(package.root_dir)
        except Exception as exc:
            _emit(
                event_sink,
                "skill.load.failed",
                skill_ref=skill,
                skill_invocation_id=invocation_id,
                status="failed",
                error_category=type(exc).__name__,
            )
            raise
        _emit(
            event_sink,
            "skill.manifest.parsed",
            skill_ref=skill,
            skill_invocation_id=invocation_id,
            status="completed",
            attributes={"has_description": bool(local_skill.description)},
        )
        _emit(
            event_sink,
            "skill.load.completed",
            skill_ref=skill,
            skill_invocation_id=invocation_id,
            status="completed",
        )
        skills.append(local_skill)
        skill_refs[local_skill.name] = skill
        skill_invocation_ids[local_skill.name] = invocation_id
    return SkillLoadResult(
        skills=skills,
        warnings=warnings,
        skill_refs=skill_refs,
        skill_invocation_ids=skill_invocation_ids,
    )


def load_local_skills() -> list[LocalSkill]:
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


def _allow_hash_mismatch() -> bool:
    return os.environ.get("KSADK_SKILL_ALLOW_HASH_MISMATCH", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _emit(
    event_sink: SkillEventSink | None,
    event_type: str,
    *,
    status: str,
    skill_ref: SkillRef | None = None,
    skill_invocation_id: str = "",
    attributes: dict[str, object] | None = None,
    error_category: str = "",
    started_at: float | None = None,
    ended_at: float | None = None,
) -> None:
    if event_sink is not None:
        event_sink.emit(
            SkillEvent.create(
                event_type,
                status=status,
                skill_ref=skill_ref,
                skill_invocation_id=skill_invocation_id,
                attributes=attributes,
                error_category=error_category,
                started_at=started_at,
                ended_at=ended_at,
            )
        )


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
    return type(
        "UnverifiedSkillPackage",
        (),
        {
            "ref": skill,
            "archive_path": archive_path,
            "extract_dir": extract_dir,
            "root_dir": store._find_skill_root(extract_dir),
            "cache_hit": False,
        },
    )()
