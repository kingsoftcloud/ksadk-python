"""Owned, activation-visible pinned Skill files for an outer-agent consumer.

This prepares files, not native discovery or sandbox isolation. The executor must
own the workspace lifetime and verify its runtime's discovery and context rules.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ksadk.resource_runtime.build_artifacts import ResourceBuildReference, restore_resource_build
from ksadk.skills.package_store import SkillPackageError


@dataclass(frozen=True)
class PreparedSkill:
    skill_id: str
    version_id: str
    content_hash: str
    relative_directory: str


@dataclass(frozen=True)
class SkillWorkspace:
    directory: Path
    skills: tuple[PreparedSkill, ...]


def _identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    if path.is_symlink() or not path.is_dir():
        raise SkillPackageError("Skill workspace must remain an owned directory")
    return info.st_dev, info.st_ino


@contextmanager
def prepare_pinned_skill_workspace(
    build_directory: Path,
    reference: ResourceBuildReference,
    *,
    binding_id: str,
    workspace_directory: Path,
) -> Iterator[SkillWorkspace]:
    """Restore a Build into a new private workspace; clean only that owned root.

    The trusted executor must admit the current activation and binding first;
    possession of Build bytes is not a grant to consume a platform resource.
    Never merges into an existing project or a global Skill directory. Existing
    user files require an executor-owned workspace composition step. The returned
    paths are relative to the actual consumer cwd, not Worker cache locations.
    """
    requested = Path(workspace_directory)
    if not requested.is_absolute():
        raise ValueError("Skill consumer workspace must be absolute")
    # Normalize platform aliases such as macOS /var while retaining exclusive
    # creation of the final path. A pre-existing symlink or directory is rejected.
    directory = requested.parent.resolve(strict=True) / requested.name
    with tempfile.TemporaryDirectory(prefix=".skill-consumer-", dir=directory.parent) as temporary:
        manifest, packages = restore_resource_build(
            build_directory,
            reference,
            cache_directory=Path(temporary),
        )
        config = manifest.snapshot.binding(binding_id).config
        if (
            config.binding.resource.kind != "skill-space"
            or config.selection_mode == "discovery"
            or config.execution_mode == "isolated"
        ):
            raise SkillPackageError(
                "Native file preparation requires an outer-agent pinned binding"
            )
        selected = packages.get(binding_id, [])
        names = [package.ref.name for package in selected]
        for package in selected:
            for path in package.extract_dir.rglob("*"):
                if path.is_file() and not path.is_relative_to(package.root_dir):
                    raise SkillPackageError(
                        "Archive contains files outside its selected Skill root"
                    )
        if len(set(name.casefold() for name in names)) != len(names) or any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name) for name in names
        ):
            raise SkillPackageError(
                "Pinned Skill names cannot form unique safe consumer directories"
            )
        directory.mkdir(mode=0o700)
        owned_identity = _identity(directory)
        try:
            skills_root = directory / ".agents" / "skills"
            skills_root.mkdir(parents=True, mode=0o700)
            prepared = []
            for package in selected:
                destination = skills_root / package.ref.name
                # The verified extraction belongs exclusively to this preparation.
                # Moving it retains complete resources and executable bits without
                # copying from any mutable profile or user Skill directory.
                package.root_dir.rename(destination)
                prepared.append(
                    PreparedSkill(
                        package.ref.skill_id,
                        package.ref.version_id,
                        package.ref.content_hash.render(),
                        destination.relative_to(directory).as_posix(),
                    )
                )
            yield SkillWorkspace(directory, tuple(prepared))
        finally:
            if directory.exists() or directory.is_symlink():
                if _identity(directory) != owned_identity:
                    raise SkillPackageError("Skill workspace ownership changed; cleanup refused")
                shutil.rmtree(directory)
