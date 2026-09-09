"""Transport and consume exact Skill archives without directory discovery.

The trusted caller authorizes the package set before staging. This contract
verifies bytes at the consumer; it is not an authorization or sandbox boundary.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ksadk.skills.loader import LocalSkill, load_local_skill
from ksadk.skills.models import ContentHash, SkillRef
from ksadk.skills.package_store import PackageStore, SkillPackage, SkillPackageError

MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_PACKAGES = 32
PINNED_PACKAGE_PROTOCOL_VERSION = 1


class PinnedSkillArchive(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    skill_id: str = Field(min_length=1, max_length=256, strict=True)
    version_id: str = Field(min_length=1, max_length=256, strict=True)
    name: str = Field(min_length=1, max_length=256, strict=True)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$", strict=True)
    archive_name: str = Field(pattern=r"^[0-9a-f]{64}\.zip$", strict=True)

    def skill_ref(self) -> SkillRef:
        return SkillRef(
            skill_id=self.skill_id,
            version_id=self.version_id,
            version="",
            name=self.name,
            content_hash=ContentHash.parse(self.content_hash),
        )


def read_archive(path: Path) -> bytes:
    """Read a bounded regular archive, rejecting a symlink at the final component."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARCHIVE_BYTES:
            raise SkillPackageError("Pinned Skill archive is not a bounded regular file")
        content = stream.read(MAX_ARCHIVE_BYTES + 1)
    if len(content) > MAX_ARCHIVE_BYTES:
        raise SkillPackageError("Pinned Skill archive exceeds size limit")
    return content


def validate_package_set(entries: tuple[PinnedSkillArchive, ...]) -> None:
    if len(entries) > MAX_PACKAGES:
        raise SkillPackageError("Too many pinned Skill packages")
    if len({entry.skill_id for entry in entries}) != len(entries) or len(
        {entry.name.casefold() for entry in entries}
    ) != len(entries):
        raise SkillPackageError("Pinned Skill identities and names must be unique")


def stage_packages(packages: list[SkillPackage], directory: Path) -> tuple[PinnedSkillArchive, ...]:
    """Copy verified archives into a private request delivery directory."""
    if len(packages) > MAX_PACKAGES:
        raise SkillPackageError("Too many pinned Skill packages")
    entries = tuple(
        PinnedSkillArchive(
            skill_id=package.ref.skill_id,
            version_id=package.ref.version_id,
            name=package.ref.name,
            content_hash=package.ref.content_hash.render() if package.ref.content_hash else "",
            archive_name=(package.ref.content_hash.value if package.ref.content_hash else "")
            + ".zip",
        )
        for package in packages
    )
    validate_package_set(entries)
    for package, entry in zip(packages, entries, strict=True):
        content = read_archive(package.archive_path)
        if "sha256:" + hashlib.sha256(content).hexdigest() != entry.content_hash:
            raise SkillPackageError("Pinned Skill archive digest mismatch")
        target = directory / entry.archive_name
        if target.exists():
            if read_archive(target) != content:
                raise SkillPackageError("Pinned Skill archive destination conflict")
            continue
        with target.open("xb") as output:
            os.chmod(target, 0o600)
            output.write(content)
    return entries


def load_pinned_packages(
    entries: tuple[PinnedSkillArchive, ...], directory: Path
) -> list[LocalSkill]:
    """Use only supplied archives; never read environment Skill directories or APIs."""
    validate_package_set(entries)
    store = PackageStore(
        directory / "extracted-packages", namespace="pinned-request", require_hash=True
    )
    skills = []
    for entry in entries:
        package = store.store_archive(
            entry.skill_ref(), read_archive(directory / entry.archive_name)
        )
        skill = load_local_skill(package.root_dir)
        if skill.name != entry.name:
            raise SkillPackageError("Pinned Skill name does not match its manifest")
        skills.append(skill)
    return skills
