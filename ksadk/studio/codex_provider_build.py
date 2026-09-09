"""Local Codex Provider admission and Bundle references, separate from cloud YAML.

A local Provider Bundle binds the existing immutable Codex record; native
marketplace bytes remain governed by that record's original plugin lock.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import Field

from ksadk.plugins.providers.codex_dsh import (
    SHIPPED_CODEX_PROVIDER_ID,
    SHIPPED_CODEX_PROVIDER_VERSION,
)
from ksadk.studio.contracts import ContractModel, RuntimeRef
from ksadk.studio.errors import StudioError
from ksadk.studio.resource_build_admission import (
    admit_resource_build,
    resource_build_required,
)

CODEX_PROVIDER_REF = f"plugin://{SHIPPED_CODEX_PROVIDER_ID}@{SHIPPED_CODEX_PROVIDER_VERSION}"


def _digest(value: Any) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
    )


class CodexProviderBuildReference(ContractModel):
    provider_ref: str = Field(pattern=r"^plugin://io\.ksadk\.codex-provider@[^/]+$")
    registration_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    bundle_build_id: str = Field(pattern=r"^build_[0-9a-f]+$")
    bundle_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @property
    def digest(self) -> str:
        return _digest(self.model_dump(by_alias=True))


class CodexProviderBuildManager:
    def __init__(self, studio: Any):
        self.studio = studio

    def _registration_digest(self) -> str:
        manifest = self.studio._active_provider_manifests.get(CODEX_PROVIDER_REF)
        if manifest is None:
            raise StudioError(
                "AGENT_PROVIDER_NOT_REGISTERED",
                "Codex Provider 未注册；请在插件中心安装并启用官方 Codex Provider，"
                "确认 DSH 工具链可用后重试",
                status_code=409,
            )
        return _digest(manifest.model_dump(by_alias=True, mode="json", exclude_none=True))

    def prepare(
        self,
        snapshot,
        model_profiles: dict,
        model_profile_ids: list[str],
        native_plugin_lock_digest: str | None,
    ) -> CodexProviderBuildReference:
        registration_digest = self._registration_digest()
        # The current Provider owns one native client connection per activation.
        # Model names may vary, but a different endpoint/credential needs a new
        # activation contract; never silently send it to the default connection.
        connections = {
            (
                p.get("provider", "openai-compatible"),
                p.get("endpointUrl"),
                p.get("baseUrl"),
                p.get("credentialRef"),
                p.get("wireApi"),
            )
            for p in model_profiles.values()
        }
        missing_profiles = set(snapshot.manifest.allowed_models) - set(model_profiles)
        if len(connections) > 1 or (model_profiles and missing_profiles):
            raise StudioError(
                "CODEX_PROVIDER_MODEL_CONNECTION_UNSUPPORTED",
                "当前 Codex Provider Build 的模型必须具有完整且相同的连接快照；"
                "请补齐模型配置，或拆分不同连接的 Agent",
                status_code=422,
            )
        draft = self.studio.codex_agents._project(snapshot).model_copy(deep=True)
        draft.spec.runtime = RuntimeRef(
            type="plugin",
            provider_ref=CODEX_PROVIDER_REF,
            version=snapshot.manifest.runtime.version,
            provider_config={
                "studioManifestDigest": "sha256:" + snapshot.manifest_sha256,
                "studioBuildFingerprint": _digest(
                    {
                        "manifest": snapshot.manifest_sha256,
                        "profiles": model_profiles,
                        "resourceIds": sorted(model_profile_ids),
                        "pluginLockDigest": native_plugin_lock_digest,
                    }
                ),
            },
        )
        # This Bundle describes the Python AgentProvider. Codex-native marketplace
        # selections stay in the manifest lock, while official DSH platform
        # resources remain in this projected Draft and pass the same trusted
        # resource admission used by framework runtimes.
        draft.spec.bindings.plugins = [
            binding
            for binding in draft.spec.bindings.plugins
            if binding.ecosystem == "dsh"
        ]
        manifest = self.studio._active_provider_manifests[CODEX_PROVIDER_REF]
        missing_permissions = sorted(
            set(manifest.spec.permissions) - set(draft.spec.security.allowed_permissions)
        )
        if missing_permissions:
            raise StudioError(
                "AGENT_PROVIDER_PERMISSION_DENIED",
                "请打开 Agent 编辑页，在 Codex 本地执行权限中确认 Provider 请求的权限并保存；"
                "安装插件时的同意不代替 Agent 授权",
                status_code=422,
                field="spec.security.allowedPermissions",
                details={"missingPermissions": missing_permissions},
            )
        composition = self.studio.plugin_compositions.compile(draft)
        resource_build = None
        if resource_build_required(draft):
            resource_build = admit_resource_build(
                draft,
                authority=self.studio.resource_authority,
                connections=self.studio.resource_connections,
                dsh_profile=(
                    self.studio.resource_dsh_capabilities.capture_resource_build_snapshot()
                ),
            )
        built = self.studio.builder.build(
            draft,
            composition=composition,
            resource_build=resource_build,
        )
        # Refuse a registration change during staging, before saving success.
        if self._registration_digest() != registration_digest:
            raise StudioError(
                "CODEX_PROVIDER_CHANGED", "Provider 构建期间发生变更", status_code=409
            )
        return CodexProviderBuildReference(
            provider_ref=CODEX_PROVIDER_REF,
            registration_digest=registration_digest,
            bundle_build_id=built.id,
            bundle_digest=built.bundle_digest,
        )

    def bundle_root(self, record):
        reference = record.provider_bundle
        if reference is None:
            return None
        if reference.provider_ref != CODEX_PROVIDER_REF or (
            reference.registration_digest != self._registration_digest()
        ):
            raise StudioError(
                "CODEX_PROVIDER_CHANGED", "Provider 注册已变更，请重新构建", status_code=409
            )
        try:
            built = self.studio.builds.get(reference.bundle_build_id)
        except StudioError as error:
            raise StudioError(
                "CODEX_PROVIDER_BUNDLE_UNAVAILABLE", "Provider Bundle 记录不可用，请重新构建",
                status_code=409,
            ) from error
        if built.agent_id != record.agent_name or built.bundle_digest != reference.bundle_digest:
            raise StudioError(
                "CODEX_PROVIDER_BUILD_MISMATCH", "Provider Bundle 与 Build 不一致", status_code=409
            )
        root = self.studio.plugin_runs._bundle_root(built)
        bundle = self.studio.plugin_runs._resolve_bundle(root)
        if (
            bundle.bundle_digest != reference.bundle_digest
            or (bundle.composition.profile.agent_provider.ref != reference.provider_ref)
            or bundle.composition.profile.agent_provider.config.get("studioManifestDigest")
            != ("sha256:" + record.manifest_sha256)
            or bundle.composition.profile.agent_provider.config.get("studioBuildFingerprint")
            != _digest(
                {
                    "manifest": record.manifest_sha256,
                    "profiles": record.model_profiles,
                    "resourceIds": sorted(record.model_profile_ids or []),
                    "pluginLockDigest": record.plugin_lock_digest,
                }
            )
        ):
            raise StudioError(
                "CODEX_PROVIDER_BUILD_MISMATCH", "Provider Bundle 引用校验失败", status_code=409
            )
        return root

    def native_launch(self, bundle):
        """Trusted host callback; no model arguments or serialized secrets accepted."""
        marker = bundle.composition.profile.agent_provider.config.get("studioManifestDigest")
        if marker is None:
            return None
        registration_digest = self._registration_digest()
        matches = [
            record
            for record in self.studio.codex_builds.list()
            if record.provider_bundle is not None
            and record.provider_bundle.bundle_digest == bundle.bundle_digest
            and record.provider_bundle.registration_digest == registration_digest
        ]
        if len(matches) != 1:
            raise StudioError(
                "CODEX_PROVIDER_BUILD_MISMATCH",
                "Provider Bundle 缺少唯一 Codex Build",
                status_code=409,
            )
        record = matches[0]
        self.bundle_root(record)
        # Retain native bootstrap, credentials, exact build HOME and workspace
        # from the existing resolver. No cloud schema or second launch policy.
        return self.studio.codex_runs.resolve(record.id).launch_context
