from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

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


def load_skills(
    *,
    prompt: str = "",
    skill_names: list[str] | None = None,
    service_transport: httpx.BaseTransport | None = None,
) -> SkillLoadResult:
    warnings: list[str] = []
    skills: list[LocalSkill] = []
    skills.extend(load_local_skills())

    service_url = resolve_skill_service_url(require_spaces=True)
    if not service_url:
        return SkillLoadResult(skills=skills, warnings=warnings)

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
    for space_id in registry.user_skill_space_ids():
        listing = client.list_skills_by_space_id(space_id)
        selected_refs.extend(
            registry.dedupe_skill_refs(
                registry.select_remote_skill_refs(
                    listing.active_skills(),
                    prompt,
                    skill_names=skill_names,
                ),
                seen_names=seen_names,
            )
        )

    if registry.public_skill_space_ids():
        listing = client.list_available_premade_skills()
        selected_refs.extend(
            registry.dedupe_skill_refs(
                registry.select_public_skill_refs(listing.active_skills()),
                seen_names=seen_names,
            )
        )

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
                warnings.append(str(exc))
        skills.append(load_local_skill(package.root_dir))
    return SkillLoadResult(skills=skills, warnings=warnings)


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
