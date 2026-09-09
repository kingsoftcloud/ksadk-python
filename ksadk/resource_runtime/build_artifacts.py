"""Resource binding and pinned Skill files retained as one verifiable build input.

The caller owns the enclosing Build and authorizes its resolved resource snapshot.
This module writes into its staging directory; it creates no alternate registry,
authorization store, or cloud upload path.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Literal, Mapping

from pydantic import field_validator, model_validator

from ksadk.plugins.contracts import PluginContractModel
from ksadk.resource_runtime.contracts import Digest, Identifier
from ksadk.resource_runtime.snapshots import ResourceSnapshot
from ksadk.skills.loader import load_local_skill
from ksadk.skills.package_store import PackageStore, SkillPackage, SkillPackageError
from ksadk.skills.runtime.pinned import PinnedSkillArchive, read_archive, stage_packages


class LockedSkillPackage(PluginContractModel):
    binding_id: Identifier
    archive: PinnedSkillArchive


class ResourceBuildManifest(PluginContractModel):
    schema_version: Literal[1] = 1
    snapshot: ResourceSnapshot
    packages: tuple[LockedSkillPackage, ...] = ()

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Resource Build schema version must be an integer")
        return value

    @model_validator(mode="after")
    def exact_selected_packages(self) -> ResourceBuildManifest:
        expected = set()
        for binding in self.snapshot.bindings:
            config = binding.config
            if (
                config.binding.resource.kind == "skill-space"
                and config.selection_mode != "discovery"
            ):
                expected.update(
                    (config.binding.id, skill.skill_id, skill.version_id, skill.content_hash)
                    for skill in config.selected_skills
                )
        actual = [
            (
                item.binding_id,
                item.archive.skill_id,
                item.archive.version_id,
                item.archive.content_hash,
            )
            for item in self.packages
        ]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError("Build packages must exactly match pinned resource selections")
        return self

    def canonical_bytes(self) -> bytes:
        # ResourceSnapshot already normalizes config defaults for its digest.
        payload = {
            "schemaVersion": self.schema_version,
            "snapshotDigest": self.snapshot.digest,
            "packages": [
                item.model_dump(by_alias=True, mode="json")
                for item in sorted(
                    self.packages, key=lambda item: (item.binding_id, item.archive.skill_id)
                )
            ],
        }
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()


class ResourceBuildReference(PluginContractModel):
    """Persist this digest in the enclosing immutable Build record."""

    digest: Digest
    snapshot_digest: Digest


def write_resource_build(
    directory: Path,
    snapshot: ResourceSnapshot,
    packages: Mapping[str, list[SkillPackage]],
) -> ResourceBuildReference:
    """Populate a new caller-owned staging directory, retaining complete ZIPs."""
    if directory.exists():
        raise FileExistsError("Resource Build destination already exists")
    # This private directory is not published until all references and bytes pass;
    # the caller publishes its enclosing Build only on success.
    directory.mkdir(parents=True, mode=0o700)
    locked = []
    try:
        for binding_id, values in packages.items():
            binding = snapshot.binding(binding_id)
            if binding.config.binding.resource.kind != "skill-space":
                raise ValueError("Skill packages require a Skill binding")
            entries = stage_packages(values, directory)
            locked.extend(
                LockedSkillPackage(binding_id=binding_id, archive=entry) for entry in entries
            )
        manifest = ResourceBuildManifest(snapshot=snapshot, packages=tuple(locked))
        (directory / "resource-build.json").write_text(
            manifest.model_dump_json(by_alias=True), encoding="utf-8"
        )
        (directory / "resource-build.json").chmod(0o600)
        reference = ResourceBuildReference(digest=manifest.digest, snapshot_digest=snapshot.digest)
        # Build admission checks extraction as well as the archive hash. Keep the
        # verified ZIPs as the portable artifact, not temporary absolute paths.
        with tempfile.TemporaryDirectory(prefix=".resource-verify-", dir=directory.parent) as cache:
            restore_resource_build(directory, reference, cache_directory=Path(cache))
        return reference
    except Exception:
        import shutil

        shutil.rmtree(directory)
        raise


def restore_resource_build(
    directory: Path,
    reference: ResourceBuildReference,
    *,
    cache_directory: Path,
) -> tuple[ResourceBuildManifest, dict[str, list[SkillPackage]]]:
    """Restore from saved bytes, without credentials, catalog queries, or env defaults."""
    path = directory / "resource-build.json"
    if directory.is_symlink() or path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise SkillPackageError("Invalid Resource Build manifest file")
    manifest = ResourceBuildManifest.model_validate_json(path.read_bytes())
    if manifest.digest != reference.digest or manifest.snapshot.digest != reference.snapshot_digest:
        raise SkillPackageError("Resource Build digest mismatch")
    expected_files = {
        "resource-build.json",
        *(item.archive.archive_name for item in manifest.packages),
    }
    if {item.name for item in directory.iterdir()} != expected_files:
        raise SkillPackageError("Resource Build file set mismatch")
    # Namespace is derived from the frozen binding/identity snapshot, not runtime env.
    store = PackageStore(cache_directory, namespace=manifest.snapshot.digest, require_hash=True)
    result: dict[str, list[SkillPackage]] = {}
    for item in manifest.packages:
        package = store.store_archive(
            item.archive.skill_ref(), read_archive(directory / item.archive.archive_name)
        )
        if load_local_skill(package.root_dir).name != item.archive.name:
            raise SkillPackageError("Build Skill name does not match package metadata")
        result.setdefault(item.binding_id, []).append(package)
    return manifest, result
