"""Compile an Agent revision into one resolvable plugin composition.

This module is the build-time boundary between Studio's editable bindings and
the immutable plugin graph.  It deliberately does not install or execute
plugins.  Every selected capability must already have an exact ``plugin://``
materialization whose manifest provides the expected Definition; the existing
``PluginRegistry`` then produces the authoritative lock.

In particular, ``mcp://`` and ``skill://`` are catalog identities, not active
PluginHost owners.  Accepting them in a profile without a materializer would
make a Studio binding look enabled while the resolver silently omitted it.
This compiler therefore rejects that state before a Bundle is built.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import ValidationError

from ksadk.plugins.contracts import (
    CompositionCapability,
    CompositionProfile,
    PluginReference,
)
from ksadk.plugins.resolver import (
    PluginRegistry,
    PluginResolutionError,
    ResolvedComposition,
)
from ksadk.studio.contracts import AgentDraft, CapabilityBinding, ResourceDescriptor
from ksadk.studio.errors import StudioError


class CompositionCompileError(ValueError):
    """Stable, typed failure while normalizing one Agent revision."""

    def __init__(self, code: str, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


class StudioResourceCatalog(Protocol):
    """Narrow catalog seam implemented by ``LocalResourceCatalog``."""

    def get(self, resource: str) -> ResourceDescriptor: ...


@dataclass(frozen=True)
class PluginCapabilitySelection:
    """One exact platform capability selected by build/deployment policy."""

    ref: str
    definition: str
    slot: str | None = None
    required: bool = True
    config: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimePluginSelection:
    """One coarse-grained AgentProvider selected by trusted product policy.

    Execution semantics remain inside the provider.  ``supported_strategies``
    is admission metadata, not another executable plugin slot.
    """

    provider_ref: str
    provider_config: Mapping[str, object] = field(default_factory=dict)
    supported_strategies: frozenset[str] = frozenset({"direct"})


@dataclass(frozen=True)
class ResourcePluginMaterialization:
    """Proof that one Studio MCP/Skill resource has an executable plugin owner."""

    kind: Literal["mcp", "skill"]
    plugin_ref: str
    config: Mapping[str, object] = field(default_factory=dict)
    required: bool = True


@dataclass(frozen=True)
class CompositionPolicy:
    """Trusted build policy used to normalize an Agent revision.

    The policy is supplied by the product/runtime distribution, not generated
    by an LLM and not stored as a second editable Agent specification.
    """

    runtimes: Mapping[str, RuntimePluginSelection]
    session_store: PluginCapabilitySelection
    providers: Mapping[str, RuntimePluginSelection] = field(default_factory=dict)
    resource_materializations: Mapping[str, ResourcePluginMaterialization] = field(
        default_factory=dict
    )
    memory_providers: Mapping[str, PluginCapabilitySelection] = field(default_factory=dict)
    context_contributors: Mapping[str, PluginCapabilitySelection] = field(
        default_factory=dict
    )
    renderers: tuple[PluginCapabilitySelection, ...] = ()
    default_runtime: str = "codex"


class CompositionCompiler:
    """Normalize Studio bindings and resolve the resulting immutable graph."""

    def __init__(
        self,
        registry: PluginRegistry,
        catalog: StudioResourceCatalog,
        policy: CompositionPolicy,
    ) -> None:
        self._registry = registry
        self._catalog = catalog
        self._policy = policy

    def compile(self, revision: AgentDraft) -> ResolvedComposition:
        runtime_type = (
            revision.spec.runtime.type
            if revision.spec.runtime is not None
            else self._policy.default_runtime
        )
        provider_ref = (
            revision.spec.runtime.provider_ref
            if revision.spec.runtime is not None
            and revision.spec.runtime.type == "plugin"
            else None
        )
        runtime = (
            self._policy.providers.get(provider_ref)
            if provider_ref is not None
            else self._policy.runtimes.get(runtime_type)
        )
        if runtime is None:
            raise CompositionCompileError(
                "agent_provider_unavailable",
                (
                    f"provider {provider_ref!r} is not installed and enabled"
                    if provider_ref is not None
                    else f"runtime {runtime_type!r} has no materialized AgentProvider"
                ),
                field=(
                    "spec.runtime.providerRef"
                    if provider_ref is not None
                    else "spec.runtime.type"
                ),
            )
        if provider_ref is not None and runtime.provider_ref != provider_ref:
            raise CompositionCompileError(
                "agent_provider_reference_mismatch",
                "installed AgentProvider selection does not match runtime providerRef",
                field="spec.runtime.providerRef",
            )

        provider_config = deepcopy(dict(runtime.provider_config))
        if revision.spec.runtime is not None:
            for key, value in revision.spec.runtime.provider_config.items():
                if key in provider_config and provider_config[key] != value:
                    raise CompositionCompileError(
                        "agent_provider_config_conflict",
                        f"AgentProvider config {key!r} conflicts with installation policy",
                        field=f"spec.runtime.providerConfig.{key}",
                    )
                provider_config[key] = deepcopy(value)
        provider_config["runtimeType"] = runtime_type
        if revision.spec.runtime is not None and revision.spec.runtime.version:
            provider_config["runtimeVersion"] = revision.spec.runtime.version

        capabilities: list[CompositionCapability] = []
        self._validate_plugin_owner(
            runtime.provider_ref,
            definition="agent.provider/v1",
            slot="agent.execution",
            field="spec.runtime.type",
        )
        self._validate_provider_strategy(revision, runtime)
        self._append_selection(
            capabilities,
            self._policy.session_store,
            field="policy.sessionStore",
        )

        if revision.spec.memory.enabled:
            memory = self._policy.memory_providers.get(revision.spec.memory.provider_ref)
            if memory is None:
                raise CompositionCompileError(
                    "memory_provider_unmaterialized",
                    "enabled memory provider has no materialized plugin owner",
                    field="spec.memory.providerRef",
                )
            self._append_selection(
                capabilities,
                memory,
                field="spec.memory.providerRef",
                extra_config={
                    "providerRef": revision.spec.memory.provider_ref,
                    "scopes": sorted(revision.spec.memory.scopes),
                },
            )

        if revision.spec.context.rollout.context_engine != "off":
            contributor_flags = revision.spec.context.contributors.model_dump(
                mode="python"
            )
            for contributor, selection in sorted(self._policy.context_contributors.items()):
                # ``None`` means policy-owned default.  Supplying the selection
                # in this trusted policy makes it active unless the revision
                # explicitly disables it.
                if contributor_flags.get(contributor) is False:
                    continue
                self._append_selection(
                    capabilities,
                    selection,
                    field=f"spec.context.contributors.{contributor}",
                    extra_config={"contributor": contributor},
                )

        for selection in self._policy.renderers:
            self._append_selection(
                capabilities,
                selection,
                field="policy.renderers",
            )

        resource_entries: dict[str, list[dict[str, object]]] = {}
        resource_required: dict[str, bool] = {}
        resource_definitions: dict[str, set[str]] = {}
        resource_groups: tuple[
            tuple[Literal["mcp", "skill"], list[CapabilityBinding]], ...
        ] = (
            ("mcp", revision.spec.bindings.mcp_servers),
            ("skill", revision.spec.bindings.skills),
        )
        for kind, bindings in resource_groups:
            for binding in bindings:
                if not binding.enabled:
                    continue
                materialization = self._materialization(binding, expected_kind=kind)
                definition = (
                    "mcp.connector/v1" if kind == "mcp" else "skill.source/v1"
                )
                self._validate_plugin_owner(
                    materialization.plugin_ref,
                    definition=definition,
                    field=f"spec.bindings.{kind}",
                )
                descriptor = self._catalog_resource(
                    binding.resource_id,
                    field=f"spec.bindings.{kind}",
                )
                if descriptor.kind != kind:
                    raise CompositionCompileError(
                        "resource_kind_mismatch",
                        f"resource {binding.resource_id!r} is not a {kind} resource",
                        field=f"spec.bindings.{kind}",
                    )
                if descriptor.status != "ready":
                    raise CompositionCompileError(
                        "resource_not_ready",
                        f"resource {binding.resource_id!r} is {descriptor.status!r}",
                        field=f"spec.bindings.{kind}",
                    )
                entry: dict[str, object] = {
                    "resourceId": descriptor.resource_id,
                    "kind": kind,
                    "name": descriptor.name,
                    "version": descriptor.version,
                    "digest": descriptor.digest,
                    "binding": deepcopy(binding.config),
                    "materializer": deepcopy(dict(materialization.config)),
                }
                resource_entries.setdefault(materialization.plugin_ref, []).append(entry)
                resource_required[materialization.plugin_ref] = (
                    resource_required.get(materialization.plugin_ref, False)
                    or materialization.required
                )
                resource_definitions.setdefault(materialization.plugin_ref, set()).add(
                    definition
                )

        for plugin_ref, resources in sorted(resource_entries.items()):
            definitions = resource_definitions[plugin_ref]
            if len(definitions) > 1:
                raise CompositionCompileError(
                    "resource_materializer_ambiguous",
                    f"plugin {plugin_ref!r} cannot materialize MCP and Skill resources "
                    "in one capability entry",
                    field="spec.bindings",
                )
            resources.sort(key=lambda item: (str(item["kind"]), str(item["resourceId"])))
            capabilities.append(
                self._capability(
                    plugin_ref,
                    required=resource_required[plugin_ref],
                    config={"resources": resources},
                    field="spec.bindings",
                )
            )

        capabilities = self._merge_capabilities(capabilities)
        policies = {
            "network": f"policy://{revision.spec.security.network.mode}@1",
            "tool": f"policy://{revision.spec.bindings.policy_template}@1",
        }
        try:
            profile = CompositionProfile(
                agent_provider=PluginReference(
                    ref=runtime.provider_ref,
                    config=provider_config,
                ),
                capabilities=capabilities,
                policies=policies,
                ui_contributions=sorted(selection.ref for selection in self._policy.renderers),
            )
        except ValidationError as exc:
            raise CompositionCompileError(
                "composition_profile_invalid",
                f"normalized composition profile is invalid: {exc}",
            ) from exc

        dangling = [
            item.ref
            for item in profile.capabilities
            if item.ref.startswith(("mcp://", "skill://"))
        ]
        if dangling:
            raise CompositionCompileError(
                "resource_capability_unmaterialized",
                f"catalog references are not active plugin owners: {', '.join(dangling)}",
            )
        try:
            return self._registry.resolve(profile)
        except PluginResolutionError as exc:
            raise CompositionCompileError(exc.code, str(exc)) from exc

    def _validate_provider_strategy(
        self,
        revision: AgentDraft,
        runtime: RuntimePluginSelection,
    ) -> None:
        strategy_name = revision.spec.execution.strategy
        if strategy_name not in runtime.supported_strategies:
            raise CompositionCompileError(
                "execution_strategy_unavailable",
                f"AgentProvider does not support execution strategy {strategy_name!r}",
                field="spec.execution.strategy",
            )

    def _materialization(
        self,
        binding: CapabilityBinding,
        *,
        expected_kind: Literal["mcp", "skill"],
    ) -> ResourcePluginMaterialization:
        materialization = self._policy.resource_materializations.get(binding.resource_id)
        if materialization is None:
            raise CompositionCompileError(
                "resource_capability_unmaterialized",
                f"resource {binding.resource_id!r} has no active plugin materialization",
                field=f"spec.bindings.{expected_kind}",
            )
        if materialization.kind != expected_kind:
            raise CompositionCompileError(
                "resource_materializer_kind_mismatch",
                f"resource {binding.resource_id!r} is mapped as {materialization.kind!r}",
                field=f"spec.bindings.{expected_kind}",
            )
        if materialization.plugin_ref.startswith(("mcp://", "skill://")):
            raise CompositionCompileError(
                "resource_capability_unmaterialized",
                f"resource {binding.resource_id!r} still points at catalog identity "
                f"{materialization.plugin_ref!r}",
                field=f"spec.bindings.{expected_kind}",
            )
        return materialization

    def _catalog_resource(self, resource_id: str, *, field: str) -> ResourceDescriptor:
        try:
            return self._catalog.get(resource_id)
        except (KeyError, StudioError) as exc:
            raise CompositionCompileError(
                "resource_not_found",
                f"materialized resource {resource_id!r} is absent from the revision catalog",
                field=field,
            ) from exc

    def _append_selection(
        self,
        capabilities: list[CompositionCapability],
        selection: PluginCapabilitySelection,
        *,
        field: str,
        extra_config: Mapping[str, object] | None = None,
    ) -> None:
        self._validate_plugin_owner(
            selection.ref,
            definition=selection.definition,
            slot=selection.slot,
            field=field,
        )
        config = deepcopy(dict(selection.config))
        if extra_config:
            overlap = set(config).intersection(extra_config)
            if overlap:
                raise CompositionCompileError(
                    "composition_config_conflict",
                    f"compiler-owned config keys cannot be overridden: {sorted(overlap)}",
                    field=field,
                )
            config.update(deepcopy(dict(extra_config)))
        capabilities.append(
            self._capability(
                selection.ref,
                required=selection.required,
                config=config,
                field=field,
            )
        )

    @staticmethod
    def _merge_capabilities(
        capabilities: list[CompositionCapability],
    ) -> list[CompositionCapability]:
        """Collapse multiple Definition selections owned by one plugin.

        A single plugin can legitimately provide Store, Context, and renderer
        Definitions.  ``CompositionProfile`` pins that plugin once, so the
        compiler merges disjoint (or identical) config keys before validation
        rather than rejecting a valid multi-capability owner as a duplicate.
        Conflicting values remain a typed build error instead of last-write
        wins behavior.
        """

        merged: dict[str, tuple[bool, dict[str, object]]] = {}
        for capability in capabilities:
            required, config = merged.get(capability.ref, (False, {}))
            for key, value in capability.config.items():
                if key in config and config[key] != value:
                    raise CompositionCompileError(
                        "composition_config_conflict",
                        f"plugin {capability.ref!r} received conflicting config for {key!r}",
                    )
                config[key] = deepcopy(value)
            merged[capability.ref] = (required or capability.required, config)
        return [
            CompositionCapability(ref=ref, required=required, config=config)
            for ref, (required, config) in sorted(merged.items())
        ]

    @staticmethod
    def _capability(
        ref: str,
        *,
        required: bool,
        config: Mapping[str, object],
        field: str,
    ) -> CompositionCapability:
        if ref.startswith(("mcp://", "skill://")):
            raise CompositionCompileError(
                "resource_capability_unmaterialized",
                f"catalog identity {ref!r} has no active plugin owner",
                field=field,
            )
        try:
            return CompositionCapability(
                ref=ref,
                required=required,
                config=deepcopy(dict(config)),
            )
        except ValidationError as exc:
            raise CompositionCompileError(
                "plugin_reference_invalid",
                f"invalid materialized plugin reference {ref!r}: {exc}",
                field=field,
            ) from exc

    def _validate_plugin_owner(
        self,
        ref: str,
        *,
        definition: str,
        slot: str | None = None,
        field: str,
    ) -> None:
        if ref.startswith(("mcp://", "skill://")):
            raise CompositionCompileError(
                "resource_capability_unmaterialized",
                f"catalog identity {ref!r} has no active plugin owner",
                field=field,
            )
        try:
            parsed = PluginReference(ref=ref)
        except ValidationError as exc:
            raise CompositionCompileError(
                "plugin_reference_invalid",
                f"invalid plugin reference {ref!r}",
                field=field,
            ) from exc
        plugin_id, version = parsed.ref.removeprefix("plugin://").rsplit("@", 1)
        try:
            manifest = self._registry.manifest_for(plugin_id, version)
        except PluginResolutionError as exc:
            raise CompositionCompileError(exc.code, str(exc), field=field) from exc
        if any(
            offer.definition == definition and (slot is None or offer.slot == slot)
            for offer in manifest.spec.provides
        ):
            return
        slot_suffix = f" at slot {slot!r}" if slot is not None else ""
        raise CompositionCompileError(
            "plugin_capability_mismatch",
            f"plugin {ref!r} does not provide {definition!r}{slot_suffix}",
            field=field,
        )


__all__ = [
    "CompositionCompileError",
    "CompositionCompiler",
    "CompositionPolicy",
    "PluginCapabilitySelection",
    "ResourcePluginMaterialization",
    "RuntimePluginSelection",
]
