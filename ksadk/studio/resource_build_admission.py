"""Trusted admission input for immutable platform-resource Build materialization."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from ksadk.plugins.bridges.dsh import DshProfileBuildSnapshot
from ksadk.resource_runtime.contracts import ResourceConfig
from ksadk.resource_runtime.plugin_config import resource_plugin_config
from ksadk.resource_runtime.snapshots import (
    FrozenResourceBinding,
    MemoryRecallPolicy,
    ResourceSnapshot,
)
from ksadk.studio.capabilities import canonical_json, sha256_digest
from ksadk.studio.contracts import AgentDraft, MemorySpec, NativePluginBinding
from ksadk.studio.errors import StudioError
from ksadk.studio.resource_authority import (
    VerifiedResourceAuthority,
    resource_allowed_operations,
)
from ksadk.studio.resource_build_materializer import materialize_resource_build
from ksadk.studio.resource_connections import ResourceConnectionRepository

_RESOURCE_BUNDLES = {
    "knowledge-base": "@kingsoftcloud/dsh-knowledge",
    "memory-instance": "@kingsoftcloud/dsh-memory",
    "skill-space": "@kingsoftcloud/dsh-skill-center",
}


class ResourceAuthority(Protocol):
    def admit(
        self,
        config: ResourceConfig,
        *,
        expected_connection_revision: int | None = None,
    ) -> VerifiedResourceAuthority: ...


@dataclass(frozen=True)
class AdmittedResourceBuild:
    """Credential-free snapshot plus private materialization dependencies."""

    snapshot: ResourceSnapshot
    bindings: tuple[NativePluginBinding, ...]
    memory: MemorySpec
    connections: ResourceConnectionRepository = field(repr=False, compare=False)

    def materialize(self, directory: Path):
        return materialize_resource_build(
            directory,
            bindings=self.bindings,
            memory=self.memory,
            admitted_snapshot=self.snapshot,
            connections=self.connections,
        )


def admit_resource_build(
    draft: AgentDraft,
    *,
    authority: ResourceAuthority | None,
    connections: ResourceConnectionRepository,
    dsh_profile: DshProfileBuildSnapshot | None,
) -> AdmittedResourceBuild | None:
    """Admit enabled official resources without trusting browser-supplied identity."""

    selected: list[tuple[NativePluginBinding, ResourceConfig]] = []
    for binding in draft.spec.bindings.plugins:
        if not binding.enabled:
            continue
        config = resource_plugin_config(
            binding.plugin_ref,
            binding.ecosystem,
            binding.config,
            enabled=True,
        )
        if config is not None:
            selected.append((binding, config))
    if not selected:
        return None
    if authority is None:
        raise StudioError(
            "RESOURCE_AUTHORITY_REQUIRED",
            "平台资源 Build 需要宿主配置可信身份与权限校验",
            status_code=503,
            field="spec.bindings.plugins",
        )
    if dsh_profile is None:
        raise StudioError(
            "RESOURCE_BUILD_PROFILE_REQUIRED",
            "平台资源 Build 需要锁定当前 DSH Profile 安装快照",
            status_code=503,
            field="spec.bindings.plugins",
        )

    required_bundles = {
        _RESOURCE_BUNDLES[config.binding.resource.kind]
        for _, config in selected
    }
    missing_bundles = sorted(required_bundles - set(dsh_profile.projection.bundles))
    if missing_bundles:
        raise StudioError(
            "RESOURCE_PLUGIN_NOT_INSTALLED",
            "当前 DSH Profile 未安装并启用所绑定的平台资源插件",
            status_code=409,
            field="spec.bindings.plugins",
            details={"missingBundles": missing_bundles},
        )

    frozen: list[FrozenResourceBinding] = []
    for binding, config in selected:
        record = connections.get(config.binding.connection_ref)
        grant = authority.admit(config, expected_connection_revision=record.revision)
        current = connections.get(config.binding.connection_ref)
        if current != record:
            raise StudioError(
                "RESOURCE_CONNECTION_CHANGED",
                "资源准入期间连接发生变化，请重新构建",
                status_code=409,
            )
        if (
            grant.connection_ref != record.target.connection_ref
            or grant.connection_revision != record.revision
            or grant.tenant_ref != record.target.tenant_ref
            or grant.resource_principal_ref != record.target.principal_ref
            or grant.resource != config.binding.resource
            or grant.data_endpoint != record.target.endpoint
            or grant.expires_at <= datetime.now(timezone.utc)
            or tuple(grant.allowed_operations) != resource_allowed_operations(config)
        ):
            raise StudioError(
                "RESOURCE_AUTHORITY_INVALID",
                "平台资源准入结果与当前绑定不一致",
                status_code=409,
                field="spec.bindings.plugins",
            )
        frozen.append(
            FrozenResourceBinding(
                config=config,
                connection=record.target,
                connection_revision=record.revision,
                memory_recall=(
                    MemoryRecallPolicy.model_validate(
                        draft.spec.memory.recall.model_dump(exclude={"enabled"})
                    )
                    if config.binding.resource.kind == "memory-instance"
                    and draft.spec.memory.enabled
                    else None
                ),
            )
        )

    native_lock = [
        {
            "pluginRef": binding.plugin_ref,
            "ecosystem": binding.ecosystem,
            "snapshotDigest": binding.snapshot_digest,
            "components": sorted(binding.components),
            "configDigest": config.digest,
        }
        for binding, config in selected
    ]
    native_lock.sort(key=lambda item: item["pluginRef"])
    snapshot = ResourceSnapshot(
        plugin_lock_digest=sha256_digest(canonical_json(native_lock)),
        bindings=tuple(frozen),
        dsh_profile=dsh_profile,
    )
    return AdmittedResourceBuild(
        snapshot=snapshot,
        bindings=tuple(binding for binding, _ in selected),
        memory=draft.spec.memory.model_copy(deep=True),
        connections=connections,
    )


def resource_build_required(draft: AgentDraft) -> bool:
    return any(
        binding.enabled
        and resource_plugin_config(
            binding.plugin_ref,
            binding.ecosystem,
            binding.config,
            enabled=True,
        )
        is not None
        for binding in draft.spec.bindings.plugins
    )


__all__ = ["AdmittedResourceBuild", "admit_resource_build", "resource_build_required"]
