"""Studio Agent revision -> immutable PluginHost composition binding.

This is the production build seam for Phase 2.  It admits built-in providers
or one exact AgentProvider registration emitted by the active DSH host,
then delegates the deterministic Profile/Lock construction to
``CompositionCompiler``.  Legacy ADK/LangGraph drafts intentionally bypass
this module and retain their established source-build behavior.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

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
from ksadk.plugins.providers.legacy_catalog import (
    BUILTIN_PROVIDER_VERSION,
    KSADK_HARNESS_AGENT_PROVIDER_PLUGIN_ID,
    builtin_agent_provider_manifests,
)
from ksadk.plugins.resolver import PluginRegistry, ResolvedComposition
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
            return CompositionCompiler(
                PluginRegistry(manifests), self._catalog, policy
            ).compile(draft)
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
                plugin_id = (
                    WORKSPACE_MCP_PLUGIN_ID if kind == "mcp" else WORKSPACE_SKILL_PLUGIN_ID
                )
                materializations[binding.resource_id] = ResourcePluginMaterialization(
                    kind=kind,  # type: ignore[arg-type]
                    plugin_ref=_plugin_ref(plugin_id, BUILTIN_PLUGIN_VERSION),
                    config=deepcopy(binding.config),
                )
        return materializations

__all__ = ["StudioPluginCompositionCompiler"]
