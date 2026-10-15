"""DSH Composition/PluginHost -> Native admission, without plugin discovery.

Call this inside a host-owned AgentProvider.prepare/activation assembly. The
host supplies a generation-specific probe and already bound ToolPorts; this
module never creates leases, imports entrypoints, starts or disposes plugins.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ksadk.harness.native.adapters import ToolAuthorizer, _snapshot
from ksadk.harness.native.capabilities import (
    ActivationPin,
    AdmittedCapabilities,
    CapabilityContribution,
    CapabilityInventory,
    CapabilityPin,
    CapabilityProbe,
    CapabilityStatus,
    CapabilityUnavailable,
)
from ksadk.harness.native.ports import ToolDefinition, ToolPort
from ksadk.harness.native.state import RunScope
from ksadk.plugins.contracts import CompositionProfile, plugin_lock_digest
from ksadk.plugins.host import PluginCapabilityBinding, PluginExecutionContext
from ksadk.plugins.providers.dsh_capabilities import DshProfileCapabilityDescriptor
from ksadk.plugins.providers.dsh_mcp import (
    DSH_PROFILE_MCP_PLUGIN_ID,
    DSH_PROFILE_MCP_PLUGIN_VERSION,
    dsh_harness_tool_alias,
)
from ksadk.plugins.resolver import PluginRegistry, ResolvedComposition, composition_profile_digest


@dataclass(frozen=True)
class DshNativeContribution:
    """A host-written bridge for exactly one already active capability.

    The ToolPort must preserve the binding runtime's isolation and scoped lease.
    Its model definitions are host-reviewed and copied at admission. Instructions
    here have been admitted as trusted context, never obtained from tool output.
    A missing mapper is unavailable, rather than an in-process fallback.
    """

    binding: PluginCapabilityBinding
    tools: tuple[ToolDefinition, ...]
    tool_port: ToolPort
    trusted_instructions: tuple[str, ...] = ()
    dsh_descriptor: DshProfileCapabilityDescriptor | None = None


def project_dsh_activation(
    *,
    composition: ResolvedComposition,
    context: PluginExecutionContext,
    scope: RunScope,
    run_id: str,
    generation: str,
    tenant_permissions: Sequence[str],
    agent_permissions: Sequence[str],
) -> CapabilityInventory:
    """Revalidate existing resolved contracts and compute the exact grant view.

    Generation/scope are authenticated host facts. When PluginHost supplies
    its generation, an adapter cannot substitute another one. Neither value
    may be inferred from a profile hash or filled from model arguments.
    Profile/lock hashes use their existing DSH
    canonicalizers, including exact source snapshots/components.
    """
    if context.activation_generation and generation != context.activation_generation:
        raise CapabilityUnavailable("activation_generation_mismatch")
    try:
        profile = _snapshot(composition.profile)
        lock = _snapshot(composition.plugin_lock)
        manifests = tuple(_snapshot(manifest) for manifest in composition.manifests)
        resolved = PluginRegistry(manifests).resolve(profile)
        if (
            composition_profile_digest(profile) != composition.profile_digest
            or plugin_lock_digest(lock) != composition.plugin_lock_digest
            or resolved.plugin_lock != lock
            or context.profile_digest != composition.profile_digest
            or context.plugin_lock_digest != composition.plugin_lock_digest
        ):
            raise CapabilityUnavailable("composition_mismatch")
        activation = ActivationPin(
            scope=_snapshot(scope),
            run_id=run_id,
            generation=generation,
            profile_digest=composition.profile_digest,
            plugin_lock_digest=composition.plugin_lock_digest,
            activation_key=context.activation_key,
        )
    except CapabilityUnavailable:
        raise
    except Exception:
        raise CapabilityUnavailable("composition_invalid") from None
    manifest_by_id = {item.metadata.id: item for item in manifests}
    pins = tuple(
        CapabilityPin(
            plugin_id=entry.id,
            plugin_version=entry.version,
            plugin_digest=entry.digest,
            definition=cap.definition,
            slot=cap.slot,
            required_permissions=tuple(sorted(manifest_by_id[entry.id].spec.permissions)),
        )
        for entry in lock.plugins
        for cap in entry.provides
    )
    expected = {(pin.plugin_id, pin.plugin_version, pin.definition, pin.slot) for pin in pins}
    actual = [
        (binding.plugin_id, binding.plugin_version, binding.definition, binding.slot)
        for binding in context.bindings
    ]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise CapabilityUnavailable("capability_context_mismatch")
    execution = tuple(pin for pin in pins if pin.definition == "agent.provider/v1")
    if len(execution) != 1 or execution[0].slot != "agent.execution":
        raise CapabilityUnavailable("agent_execution_slot_invalid")
    granted = frozenset(tenant_permissions) & frozenset(agent_permissions)
    required = {permission for pin in pins for permission in pin.required_permissions}
    if not required.issubset(granted):
        raise CapabilityUnavailable("capability_permission_unavailable")
    return CapabilityInventory(
        activation=activation,
        capabilities=pins,
        granted_permissions=tuple(sorted(granted)),
    )


async def admit_dsh_capabilities(
    *,
    composition: ResolvedComposition,
    context: PluginExecutionContext,
    scope: RunScope,
    run_id: str,
    generation: str,
    tenant_permissions: Sequence[str],
    agent_permissions: Sequence[str],
    contributions: Sequence[DshNativeContribution],
    probe: CapabilityProbe | None,
    authorize: ToolAuthorizer | None = None,
    max_result_bytes: int = 65536,
) -> AdmittedCapabilities:
    """Admit once, and compose fresh host fencing into every tool dispatch."""
    inventory = project_dsh_activation(
        composition=composition,
        context=context,
        scope=scope,
        run_id=run_id,
        generation=generation,
        tenant_permissions=tenant_permissions,
        agent_permissions=agent_permissions,
    )
    if probe is None:
        raise CapabilityUnavailable("capability_status_unavailable")
    pins = {pin.identity: pin for pin in inventory.capabilities}
    active = {(b.plugin_id, b.definition, b.slot): b for b in context.bindings}
    seen: set[tuple[str, str, str]] = set()
    native: list[CapabilityContribution] = []
    for contribution in contributions:
        binding = contribution.binding
        key = (binding.plugin_id, binding.definition, binding.slot)
        bound = active.get(key)
        if (
            bound is None
            or bound.runtime is not binding.runtime
            or bound.plugin_version != binding.plugin_version
            or key in seen
            or binding.definition == "agent.provider/v1"
        ):
            raise CapabilityUnavailable("capability_contribution_mismatch")
        if not callable(getattr(contribution.tool_port, "execute", None)):
            raise CapabilityUnavailable("capability_adapter_unavailable")
        if binding.plugin_id == DSH_PROFILE_MCP_PLUGIN_ID:
            if contribution.dsh_descriptor is None:
                raise CapabilityUnavailable("dsh_descriptor_unavailable")
            if contribution.tools != project_dsh_tools(
                contribution.dsh_descriptor, composition.profile
            ):
                raise CapabilityUnavailable("dsh_tool_projection_mismatch")
        seen.add(key)
        native.append(
            CapabilityContribution(
                pin=pins[key],
                tools=contribution.tools,
                tool_port=contribution.tool_port,
                trusted_instructions=contribution.trusted_instructions,
            )
        )
    # All locked capabilities need an explicit host projection. Lifecycle-only
    # capabilities may have an empty view; no unknown capability is auto-enabled.
    if seen != {
        pin.identity for pin in inventory.capabilities if pin.definition != "agent.provider/v1"
    }:
        raise CapabilityUnavailable("capability_adapter_unavailable")

    async def host_probe(activation: ActivationPin) -> CapabilityStatus:
        status = await probe(activation)
        if not isinstance(status, CapabilityStatus):
            raise CapabilityUnavailable("capability_status_unavailable")
        # Revocation/activation fencing is host authority. Health supplements
        # that authority and does not substitute for checking generation.
        if status.available and not status.revoked:
            for binding in context.bindings:
                if await binding.runtime.health() is not True:
                    return status.model_copy(update={"available": False})
        return status

    admitted = AdmittedCapabilities(
        inventory=inventory,
        contributions=native,
        probe=host_probe,
        authorize=authorize,
        max_result_bytes=max_result_bytes,
    )
    await admitted.check_available()
    return admitted


def project_dsh_tools(
    descriptor: DshProfileCapabilityDescriptor,
    profile: CompositionProfile,
) -> tuple[ToolDefinition, ...]:
    """Project the actual DSH descriptor using its locked filter and alias ABI.

    No URL, bearer token, runtime object, remote fetching or filesystem access is
    involved. The host binds these definitions to its existing MCP ToolPort.
    Empty filters intentionally expose no tools, matching DshProfileMCPRuntime.
    """
    try:
        descriptor = _snapshot(descriptor)
        profile = _snapshot(profile)
        refs = [
            cap
            for cap in profile.capabilities
            if cap.ref == f"plugin://{DSH_PROFILE_MCP_PLUGIN_ID}@{DSH_PROFILE_MCP_PLUGIN_VERSION}"
        ]
        if len(refs) != 1:
            raise CapabilityUnavailable("dsh_binding_unavailable")
        resources = refs[0].config.get("resources")
        if not isinstance(resources, list) or len(resources) != 1:
            raise CapabilityUnavailable("dsh_binding_invalid")
        resource = resources[0]
        if not isinstance(resource, dict) or resource.get("kind") != "mcp":
            raise CapabilityUnavailable("dsh_binding_invalid")
        materializer = resource.get("materializer")
        if not isinstance(materializer, dict):
            raise CapabilityUnavailable("dsh_binding_invalid")
        expected = dict(
            profile=descriptor.profile,
            profileDigest=descriptor.profile_digest,
            descriptorDigest=descriptor.descriptor_digest,
            inventoryDigest=descriptor.inventory_digest,
        )
        if resource.get("version") != descriptor.dsh_version or any(
            materializer.get(key) != value for key, value in expected.items()
        ):
            raise CapabilityUnavailable("dsh_descriptor_mismatch")
        names = materializer.get("toolFilter")
        tools = {tool.name: tool for tool in descriptor.tools}
        if (
            not isinstance(names, list)
            or any(type(name) is not str for name in names)
            or len(names) != len(set(names))
            or not set(names).issubset(tools)
        ):
            raise CapabilityUnavailable("dsh_tool_filter_invalid")
        prefix = materializer.get("toolNamePrefix")
        if prefix is not None and type(prefix) is not str:
            raise CapabilityUnavailable("dsh_tool_alias_invalid")
        projected = tuple(
            ToolDefinition(
                name=dsh_harness_tool_alias(name, prefix),
                progress_observation=True,
                description=tools[name].description,
                parameters=tools[name].input_schema,
            )
            for name in names
        )
        if len({tool.name for tool in projected}) != len(projected):
            raise CapabilityUnavailable("dsh_tool_alias_invalid")
        return projected
    except CapabilityUnavailable:
        raise
    except Exception:
        raise CapabilityUnavailable("dsh_binding_invalid") from None
