"""Credential-free identities of the actual loaded companion execution inputs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from ksadk.plugins.bridges.dsh import DshPluginInventory, DshProfileBuildSnapshot


def _digest(value: object) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class DshCompanionComponent:
    component: str
    package: str
    version: str
    source_digest: str | None


@dataclass(frozen=True)
class DshCompanionArtifact:
    plugin_id: str
    profile: str
    components: tuple[DshCompanionComponent, ...]
    dependency_lock_digest: str
    installation_digest: str
    source_digest: str
    profile_digest: str
    host_version: str

    @property
    def plugin_digest(self) -> str:
        # Deliberately conservative: the whole dependency closure is bound.
        # Runtime generations, temporary endpoints and secrets never enter it.
        return _digest(asdict(self))


def companion_source_digest(sources: Sequence[Path]) -> str:
    """Hash trusted host-selected source bytes, excluding caches and file paths.

    The caller defines an ordered source list. File names distinguish its roles
    while an absolute checkout path does not affect a relocated installation.
    """
    rows = []
    for source in sources:
        if source.is_symlink() or not source.is_file() or source.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("Companion source must be a bounded regular file")
        rows.append((source.name, hashlib.sha256(source.read_bytes()).hexdigest()))
    return _digest(rows)


def companion_artifact(
    *,
    plugin_id: str,
    components: Mapping[str, str],
    snapshot: DshProfileBuildSnapshot,
    inventory: Sequence[DshPluginInventory],
    sources: Sequence[Path],
) -> DshCompanionArtifact:
    installed = {item.name: item for item in inventory}
    rows = []
    for component, package in sorted(components.items()):
        item = installed.get(package)
        if item is None or not item.enabled or package not in snapshot.projection.bundles:
            raise ValueError("Companion requires its entire installed and enabled package graph")
        rows.append(DshCompanionComponent(component, package, item.version, item.source_digest))
    return DshCompanionArtifact(
        plugin_id=plugin_id,
        profile=snapshot.projection.profile,
        components=tuple(rows),
        dependency_lock_digest=snapshot.dependency_lock_digest,
        installation_digest=snapshot.installation_digest,
        source_digest=companion_source_digest(sources),
        profile_digest=snapshot.projection.config_digest,
        host_version=snapshot.projection.host_version,
    )
