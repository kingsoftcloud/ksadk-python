"""Produce resource files inside a caller-owned Build staging directory.

The trusted Build adapter supplies a snapshot after identity, permission, plugin
lock and engine admission. This producer cannot authorize a browser declaration
and is deliberately not exposed as a standalone API or artifact repository.
"""

from __future__ import annotations

import tempfile
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from ksadk.resource_runtime.build_artifacts import ResourceBuildReference, write_resource_build
from ksadk.resource_runtime.plugin_config import resource_plugin_config
from ksadk.resource_runtime.snapshots import MemoryRecallPolicy, ResourceSnapshot
from ksadk.skills.models import ContentHash
from ksadk.skills.package_store import PackageStore, SkillPackageError
from ksadk.skills.service_client import SkillServiceClient
from ksadk.studio.contracts import MemorySpec, NativePluginBinding
from ksadk.studio.resource_connections import ResourceConnectionRepository


def materialize_resource_build(
    directory: Path,
    *,
    bindings: Sequence[NativePluginBinding],
    memory: MemorySpec | None,
    admitted_snapshot: ResourceSnapshot,
    connections: ResourceConnectionRepository,
) -> ResourceBuildReference:
    """Validate declaration/admission equality, then retain verified pinned bytes.

    Connections are rechecked before and after downloads. No test writes or Skill
    execution occur, and no credential or download URL enters the saved artifact.
    The enclosing Build must still lock dependencies and publish atomically.
    """
    snapshot = ResourceSnapshot.model_validate(admitted_snapshot.model_dump())
    if snapshot.dsh_profile is None:
        raise ValueError("Resource Build requires its admitted DSH profile snapshot")
    configs = []
    for value in bindings:
        binding = NativePluginBinding.model_validate(value.model_dump())
        if not binding.enabled:
            continue
        config = resource_plugin_config(
            binding.plugin_ref,
            binding.ecosystem,
            binding.config,
            enabled=True,
        )
        if config is not None:
            configs.append(config)
    if len(configs) != len(snapshot.bindings) or {
        item.binding.id: item.digest for item in configs
    } != {item.config.binding.id: item.config.digest for item in snapshot.bindings}:
        raise ValueError("Resource declaration does not match admitted Build snapshot")
    credentials = {}
    for frozen in snapshot.bindings:
        config = frozen.config
        if config.include_public:
            raise ValueError("Public Skill Build admission is not implemented")
        if config.execution_mode == "isolated" and frozen.skill_execution is None:
            raise ValueError("Isolated Skill Build requires an admitted execution target")
        recall = None
        if config.binding.resource.kind == "memory-instance" and memory and memory.enabled:
            if memory.provider_ref != f"binding://{config.binding.id}":
                raise ValueError("Memory policy does not match admitted binding")
            recall = MemoryRecallPolicy.model_validate(
                memory.recall.model_dump(exclude={"enabled"})
            )
        if frozen.memory_recall != recall:
            raise ValueError("Memory recall policy does not match admitted Build snapshot")
        credentials[config.binding.id] = connections.resolve_credentials(
            frozen.connection,
            expected_revision=frozen.connection_revision,
        )
        if frozen.skill_execution is not None:
            connections.resolve_credentials(frozen.skill_execution.connection)

    directory = Path(directory)
    if directory.exists():
        raise FileExistsError("Resource Build staging destination already exists")
    directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".resource-packages-", dir=directory.parent) as cache:
        store = PackageStore(Path(cache), namespace=snapshot.digest, require_hash=True)
        packages = {}
        for frozen in snapshot.bindings:
            config = frozen.config
            if (
                config.binding.resource.kind != "skill-space"
                or config.selection_mode == "discovery"
                or not config.selected_skills
            ):
                continue
            secret = credentials[config.binding.id]
            if secret.session_token is not None:
                raise ValueError("Skill client does not support STS Build downloads")

            def reveal(value):
                return value.get_secret_value() if value is not None else ""

            client = SkillServiceClient(
                base_url=frozen.connection.endpoint,
                region=config.binding.resource.region,
                access_key=reveal(secret.access_key),
                secret_key=reveal(secret.secret_key),
                token=reveal(secret.token),
                allow_env_fallback=False,
            )
            try:
                listing = client.list_skills_by_space_id(config.binding.resource.id)
                if listing.space_id != config.binding.resource.id:
                    raise SkillPackageError("Skill catalog does not match admitted space")
                catalog = {}
                for ref in listing.active_skills(allow_partial=True):
                    if ref.skill_id in catalog:
                        raise SkillPackageError("Skill catalog contains duplicate identities")
                    catalog[ref.skill_id] = ref
                selected_packages = []
                for selected in config.selected_skills:
                    ref = catalog.get(selected.skill_id)
                    if ref is None:
                        raise SkillPackageError("Selected Skill is absent from admitted catalog")
                    # The catalog establishes space membership; the selected version
                    # and hash establish package identity, including historical pins.
                    algorithm, digest = selected.content_hash.split(":", 1)
                    ref = replace(
                        ref,
                        version_id=selected.version_id,
                        version="",
                        archive_uri="",
                        content_hash=ContentHash(algorithm, digest),
                    )
                    selected_packages.append(
                        store.store_archive(ref, client.download_skill_archive(ref))
                    )
                packages[config.binding.id] = selected_packages
            finally:
                client.close()
        for frozen in snapshot.bindings:
            connections.resolve_credentials(
                frozen.connection,
                expected_revision=frozen.connection_revision,
            )
            if frozen.skill_execution is not None:
                connections.resolve_credentials(frozen.skill_execution.connection)
        return write_resource_build(directory, snapshot, packages)
