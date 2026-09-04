"""Studio Agent revision -> immutable PluginHost composition binding.

This is the production build seam for Phase 2.  It admits built-in providers
or one exact AgentProvider registration emitted by the active DSH host,
then delegates the deterministic Profile/Lock construction to
``CompositionCompiler``.  Legacy ADK/LangGraph drafts intentionally bypass
this module and retain their established source-build behavior.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace

from ksadk.harness.tools import HARNESS_SANDBOX_TOOL_NAMES
from ksadk.plugins.builtins import (
    BUILTIN_PLUGIN_VERSION,
    CORE_RENDERER_PLUGIN_ID,
    READ_ONLY_CONTEXT_PLUGIN_ID,
    SQLITE_SESSION_STORE_PLUGIN_ID,
    WORKSPACE_MCP_PLUGIN_ID,
    WORKSPACE_SKILL_PLUGIN_ID,
    builtin_capability_manifests,
)
from ksadk.plugins.composition import (
    CompositionCompileError,
    CompositionCompiler,
    CompositionPolicy,
    PluginCapabilitySelection,
    ResourcePluginMaterialization,
    RuntimePluginSelection,
)
from ksadk.plugins.contracts import PluginManifest
from ksadk.plugins.providers.dsh_mcp import (
    DSH_PROFILE_MCP_PLUGIN_ID,
    DSH_PROFILE_MCP_PLUGIN_VERSION,
    DSH_PROFILE_TOOL_PERMISSION,
    dsh_harness_tool_alias,
    dsh_profile_mcp_manifest,
)
from ksadk.plugins.providers.legacy_catalog import (
    BUILTIN_PROVIDER_VERSION,
    KSADK_HARNESS_AGENT_PROVIDER_PLUGIN_ID,
    builtin_agent_provider_manifests,
)
from ksadk.plugins.resolver import PluginRegistry, ResolvedComposition
from ksadk.studio.capabilities import canonical_json, sha256_digest
from ksadk.studio.contracts import AgentDraft, CapabilityBinding
from ksadk.studio.errors import StudioError
from ksadk.studio.resource_catalog import LocalResourceCatalog
from ksadk.studio.workspace import Workspace

_COMPOSED_RUNTIME_TYPES = frozenset({"harness", "plugin"})
_PROVIDER_DEFINITION = "agent.provider/v1"
_PROVIDER_SLOT = "agent.execution"


def _plugin_ref(plugin_id: str, version: str) -> str:
    return f"plugin://{plugin_id}@{version}"


def _selection(
    plugin_id: str,
    definition: str,
    slot: str,
    *,
    config: Mapping[str, object] | None = None,
) -> PluginCapabilitySelection:
    return PluginCapabilitySelection(
        ref=_plugin_ref(plugin_id, BUILTIN_PLUGIN_VERSION),
        definition=definition,
        slot=slot,
        config=deepcopy(dict(config or {})),
    )


class StudioPluginCompositionCompiler:
    """Bind a Studio revision to installed/built-in provider manifests."""

    def __init__(
        self,
        workspace: Workspace,
        catalog: LocalResourceCatalog,
        *,
        provider_manifests: Mapping[str, PluginManifest] | None = None,
    ) -> None:
        self._workspace = workspace
        self._catalog = catalog
        self._provider_manifests = dict(provider_manifests or {})

    def replace_provider_registrations(
        self, provider_manifests: Mapping[str, PluginManifest]
    ) -> None:
        """Bind the exact startup registration snapshot before any build."""

        self._provider_manifests = dict(provider_manifests)

    @staticmethod
    def required_for(draft: AgentDraft) -> bool:
        runtime = draft.spec.runtime
        return runtime is not None and runtime.type in _COMPOSED_RUNTIME_TYPES

    def compile_if_required(self, draft: AgentDraft) -> ResolvedComposition | None:
        if not self.required_for(draft):
            return None
        return self.compile(draft)

    def compile(self, draft: AgentDraft) -> ResolvedComposition:
        runtime = draft.spec.runtime
        if runtime is None or runtime.type not in _COMPOSED_RUNTIME_TYPES:
            raise StudioError(
                "PLUGIN_COMPOSITION_NOT_REQUIRED",
                "当前 Runtime 继续使用既有构建链，不应声明 PluginHost composition",
                status_code=422,
                field="spec.runtime.type",
            )

        manifests = [
            *builtin_agent_provider_manifests(),
            *builtin_capability_manifests(),
            dsh_profile_mcp_manifest(),
        ]
        harness_ref = _plugin_ref(
            KSADK_HARNESS_AGENT_PROVIDER_PLUGIN_ID,
            BUILTIN_PROVIDER_VERSION,
        )
        harness_manifest = self._provider_manifests.get(harness_ref)
        if runtime.type == "harness":
            if harness_manifest is None:
                raise StudioError(
                    "AGENT_PROVIDER_NOT_REGISTERED",
                    "KsADK Harness 尚未由受管理 DSH Profile 完成预检与注册",
                    status_code=503,
                    field="spec.runtime.type",
                )
            manifests.append(harness_manifest)
        providers: dict[str, RuntimePluginSelection] = {}
        if runtime.type == "plugin":
            assert runtime.provider_ref is not None  # RuntimeRef validates this boundary.
            external = self._registered_provider(runtime.provider_ref, draft)
            manifests.append(external)
            providers[runtime.provider_ref] = RuntimePluginSelection(
                provider_ref=runtime.provider_ref,
            )

        policy = CompositionPolicy(
            runtimes={"harness": RuntimePluginSelection(provider_ref=harness_ref)},
            providers=providers,
            session_store=_selection(
                SQLITE_SESSION_STORE_PLUGIN_ID,
                "session.event-store/v1",
                "session.events",
            ),
            resource_materializations=self._resource_materializations(draft),
            context_contributors={
                "workspace_rules": _selection(
                    READ_ONLY_CONTEXT_PLUGIN_ID,
                    "context.contributor/v1",
                    "context.bundle",
                    config={
                        "paths": ["instructions/soul.md"]
                        if draft.spec.soul is not None
                        else [],
                        "maxChars": draft.spec.context.max_input_tokens * 4,
                    },
                )
            },
            renderers=(
                _selection(
                    CORE_RENDERER_PLUGIN_ID,
                    "session.item.renderer/v1",
                    "renderer.core",
                ),
            ),
            default_runtime="harness",
        )
        try:
            resolved = CompositionCompiler(
                PluginRegistry(manifests), self._catalog, policy
            ).compile(draft)
            source_payload = draft.model_dump(
                by_alias=True,
                exclude_none=True,
                mode="json",
            )
            return replace(
                resolved,
                source_digest=sha256_digest(canonical_json(source_payload)),
            )
        except CompositionCompileError as error:
            raise StudioError(
                error.code.upper(),
                str(error),
                status_code=422,
                field=error.field,
            ) from error

    def bind_build(self, composition: ResolvedComposition, *, agent_id: str, build_id: str) -> None:
        """Keep the API seam while DSH owns package/Profile references."""

        del composition, agent_id, build_id

    def unbind_builds(self, builds: list[object]) -> tuple[tuple[str, str, str], ...]:
        """Remove exact Bundle bindings before deleting an Agent.

        The returned tokens let the caller restore protection if its filesystem
        deletion transaction fails after the receipt updates.
        """

        del builds
        return ()

    def restore_bindings(self, bindings: tuple[tuple[str, str, str], ...]) -> None:
        """Best-effort-safe rollback for a failed Agent deletion."""

        del bindings

    @staticmethod
    def build_reference(agent_id: str, build_id: str) -> str:
        return f"bundle://{agent_id}@{build_id}"

    def _registered_provider(self, provider_ref: str, draft: AgentDraft) -> PluginManifest:
        plugin_id, version = provider_ref.removeprefix("plugin://").rsplit("@", 1)
        if plugin_id == KSADK_HARNESS_AGENT_PROVIDER_PLUGIN_ID:
            raise StudioError(
                "AGENT_PROVIDER_REFERENCE_RESERVED",
                "Harness Runtime 必须选择受管理的默认 Provider 引用",
                status_code=422,
                field="spec.runtime.providerRef",
            )
        manifest = self._provider_manifests.get(provider_ref)
        if manifest is None:
            raise StudioError(
                "AGENT_PROVIDER_NOT_REGISTERED",
                "所选 AgentProvider 尚未由当前 DSH Profile 完成预检与注册",
                status_code=422,
                field="spec.runtime.providerRef",
            )
        if (
            manifest.metadata.id != plugin_id
            or manifest.metadata.version != version
        ):
            raise StudioError(
                "AGENT_PROVIDER_REGISTRATION_MISMATCH",
                "DSH AgentProvider 注册信息与精确引用不一致",
                status_code=409,
                field="spec.runtime.providerRef",
            )
        offers = [
            offer
            for offer in manifest.spec.provides
            if offer.definition == _PROVIDER_DEFINITION
        ]
        if (
            len(offers) != 1
            or offers[0].slot != _PROVIDER_SLOT
            or offers[0].mode != "unique"
        ):
            raise StudioError(
                "AGENT_PROVIDER_MANIFEST_INVALID",
                "插件必须唯一提供 agent.provider/v1 的 agent.execution 槽位",
                status_code=422,
                field="spec.runtime.providerRef",
            )
        if manifest.spec.isolation != "sidecar":
            raise StudioError(
                "AGENT_PROVIDER_ISOLATION_INVALID",
                "DSH AgentProvider 必须使用 sidecar 隔离",
                status_code=422,
                field="spec.runtime.providerRef",
            )
        missing_permissions = sorted(
            set(manifest.spec.permissions)
            - set(draft.spec.security.allowed_permissions)
        )
        if missing_permissions:
            raise StudioError(
                "AGENT_PROVIDER_PERMISSION_DENIED",
                "Agent 未批准 Provider 请求的权限",
                status_code=422,
                field="spec.security.allowedPermissions",
                details={"missingPermissions": missing_permissions},
            )
        return manifest

    def _resource_materializations(
        self, draft: AgentDraft
    ) -> dict[str, ResourcePluginMaterialization]:
        materializations: dict[str, ResourcePluginMaterialization] = {}
        groups: tuple[tuple[str, list[CapabilityBinding]], ...] = (
            ("mcp", draft.spec.bindings.mcp_servers),
            ("skill", draft.spec.bindings.skills),
        )
        for kind, bindings in groups:
            for binding in bindings:
                if not binding.enabled:
                    continue
                # Catalog lookup is deliberate even though CompositionCompiler
                # repeats the final kind/readiness check: it makes this product
                # materializer fail at the authoritative Studio resource seam.
                descriptor = self._catalog.get(binding.resource_id)
                if descriptor.kind != kind:
                    raise StudioError(
                        "RESOURCE_KIND_INVALID",
                        f"{kind.upper()} binding 引用了错误类型的资源",
                        status_code=422,
                        field=f"spec.bindings.{kind}",
                        details={"resourceId": binding.resource_id},
                    )
                config = deepcopy(binding.config)
                if kind == "mcp" and descriptor.contract.get("materialization") == "dsh-profile":
                    if binding.approval != "never":
                        raise StudioError(
                            "DSH_MCP_AUTONOMOUS_APPROVAL_REQUIRED",
                            "Harness 尚不支持 DSH 工具逐调用审批；必须显式允许自主调用",
                            status_code=422,
                            field="spec.bindings.mcpServers.approval",
                        )
                    unknown = sorted(set(config) - {"toolFilter", "toolNamePrefix"})
                    if unknown:
                        raise StudioError(
                            "DSH_MCP_BINDING_CONFIG_INVALID",
                            "DSH MCP 绑定只支持 toolFilter 与 toolNamePrefix",
                            status_code=422,
                            field="spec.bindings.mcpServers",
                            details={"unknownFields": unknown},
                        )
                    tool_filter = config.get("toolFilter")
                    if (
                        not isinstance(tool_filter, list)
                        or not tool_filter
                        or any(
                            not isinstance(item, str) or not item.strip()
                            for item in tool_filter
                        )
                        or len(tool_filter) != len(set(tool_filter))
                        or len(tool_filter) > 32
                        or sum(len(item.encode("utf-8")) for item in tool_filter) > 2048
                    ):
                        raise StudioError(
                            "DSH_MCP_TOOL_FILTER_REQUIRED",
                            "DSH MCP 绑定必须显式选择至少一个且不重复的工具",
                            status_code=422,
                            field="spec.bindings.mcpServers",
                        )
                    tool_filter = [item.strip() for item in tool_filter]
                    config["toolFilter"] = tool_filter
                    known_tools = {
                        str(item.get("name"))
                        for item in descriptor.contract.get("discoveredTools") or []
                        if isinstance(item, Mapping)
                    }
                    unknown_tools = sorted(set(tool_filter) - known_tools)
                    if unknown_tools:
                        raise StudioError(
                            "DSH_MCP_TOOL_FILTER_INVALID",
                            "DSH MCP toolFilter 包含当前 Profile 不存在的工具",
                            status_code=422,
                            field="spec.bindings.mcpServers",
                            details={"unknownTools": unknown_tools},
                        )
                    if (
                        DSH_PROFILE_TOOL_PERMISSION
                        not in draft.spec.security.allowed_permissions
                    ):
                        raise StudioError(
                            "DSH_MCP_HOST_PERMISSION_REQUIRED",
                            "DSH Profile 工具在宿主用户权限下运行，必须显式授权",
                            status_code=422,
                            field="spec.security.allowedPermissions",
                            details={
                                "missingPermissions": [DSH_PROFILE_TOOL_PERMISSION]
                            },
                        )
                    prefix = config.get("toolNamePrefix")
                    if prefix is not None and (
                        not isinstance(prefix, str)
                        or not prefix.strip()
                        or len(prefix.strip()) > 16
                        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,15}", prefix.strip())
                        is None
                    ):
                        raise StudioError(
                            "DSH_MCP_BINDING_CONFIG_INVALID",
                            "DSH MCP toolNamePrefix 必须是 1-16 个字母、数字、下划线或连字符",
                            status_code=422,
                            field="spec.bindings.mcpServers",
                        )
                    prefix = prefix.strip() if isinstance(prefix, str) else None
                    aliases = [dsh_harness_tool_alias(name, prefix) for name in tool_filter]
                    if len(aliases) != len(set(aliases)):
                        raise StudioError(
                            "DSH_MCP_TOOL_ALIAS_COLLISION",
                            "所选 DSH 工具映射为模型工具名后发生冲突",
                            status_code=422,
                            field="spec.bindings.mcpServers",
                        )
                    reserved_aliases = sorted(
                        set(aliases) & HARNESS_SANDBOX_TOOL_NAMES
                    )
                    if reserved_aliases:
                        raise StudioError(
                            "DSH_MCP_TOOL_ALIAS_RESERVED",
                            "DSH 工具名与 Harness 内置 sandbox 工具冲突，请设置 toolNamePrefix",
                            status_code=422,
                            field="spec.bindings.mcpServers",
                            details={"reservedAliases": reserved_aliases},
                        )
                    config["toolNamePrefix"] = prefix
                    plugin_id = DSH_PROFILE_MCP_PLUGIN_ID
                    plugin_version = DSH_PROFILE_MCP_PLUGIN_VERSION
                    config.update(
                        {
                            "profile": descriptor.contract.get("profile"),
                            "profileDigest": descriptor.contract.get("profileDigest"),
                            "descriptorDigest": descriptor.contract.get("descriptorDigest"),
                            "inventoryDigest": descriptor.contract.get("inventoryDigest"),
                        }
                    )
                else:
                    plugin_id = (
                        WORKSPACE_MCP_PLUGIN_ID
                        if kind == "mcp"
                        else WORKSPACE_SKILL_PLUGIN_ID
                    )
                    plugin_version = BUILTIN_PLUGIN_VERSION
                materializations[binding.resource_id] = ResourcePluginMaterialization(
                    kind=kind,  # type: ignore[arg-type]
                    plugin_ref=_plugin_ref(plugin_id, plugin_version),
                    config=config,
                )
        return materializations

__all__ = ["StudioPluginCompositionCompiler"]
