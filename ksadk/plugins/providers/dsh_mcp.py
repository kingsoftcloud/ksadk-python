"""PluginHost adapter for the ephemeral DSH Profile MCP lease.

The immutable Bundle records only the DSH Profile identity and capability
digests.  The loopback URL and bearer credential are obtained from Studio's
profile-level lifecycle service after PluginHost activation and are never
serialized into the Agent spec, composition, or plugin lock.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any, cast

from ksadk.harness.config import McpToolSpec
from ksadk.plugins.bundle import ResolvedPluginBundle
from ksadk.plugins.contracts import CompositionProfile, PluginManifest
from ksadk.plugins.dsh_toolchain import DSH_PACKAGE_SPEC
from ksadk.plugins.host import PluginHostError
from ksadk.plugins.providers.dsh import DSH_HOST_USER_PERMISSION
from ksadk.plugins.providers.dsh_capabilities import (
    DshMcpConnectorLease,
    DshProfileCapabilityDescriptor,
    load_dsh_capability_bundle,
)

DSH_PROFILE_MCP_PLUGIN_ID = "io.ksadk.mcp.dsh-profile"
DSH_PROFILE_MCP_PLUGIN_VERSION = "1.0.0"
DSH_PROFILE_TOOL_PERMISSION = DSH_HOST_USER_PERMISSION
_PLUGIN_REF = f"plugin://{DSH_PROFILE_MCP_PLUGIN_ID}@{DSH_PROFILE_MCP_PLUGIN_VERSION}"
_MODEL_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

def dsh_harness_tool_alias(name: str, prefix: str | None = None) -> str:
    """Return a deterministic OpenAI-compatible alias for one DSH tool."""

    normalized_prefix = (prefix or "").strip()
    candidate = name
    changed = not bool(_MODEL_TOOL_NAME.fullmatch(candidate))
    if changed:
        stem = re.sub(r"[^A-Za-z0-9_-]", "_", candidate).strip("_") or "tool"
        candidate = f"{stem[:55]}_{hashlib.sha256(name.encode()).hexdigest()[:8]}"
    if normalized_prefix:
        candidate = f"{normalized_prefix}_{candidate}"
    if len(candidate) > 64:
        suffix = hashlib.sha256(name.encode()).hexdigest()[:8]
        candidate = f"{candidate[:55]}_{suffix}"
    if not _MODEL_TOOL_NAME.fullmatch(candidate):
        raise PluginHostError(
            "dsh_mcp_tool_alias_invalid",
            "DSH tool alias is not compatible with the Harness model protocol",
        )
    return candidate


def dsh_profile_mcp_manifest() -> PluginManifest:
    """Return the deterministic platform adapter manifest."""

    manifest_digest = load_dsh_capability_bundle().digest
    return cast(
        PluginManifest,
        PluginManifest.model_validate(
            {
                "metadata": {
                    "id": DSH_PROFILE_MCP_PLUGIN_ID,
                    "version": DSH_PROFILE_MCP_PLUGIN_VERSION,
                },
                "spec": {
                    "domain": "ksadk-platform",
                    "runtime": "python",
                    "entrypoint": "ksadk.plugins.providers.dsh_mcp:DshProfileMCPFactory",
                    "provides": [
                        {
                            "definition": "mcp.connector/v1",
                            "slot": "mcp.dsh-profile",
                            "mode": "multiple",
                        }
                    ],
                    "permissions": ["network:mcp", DSH_PROFILE_TOOL_PERMISSION],
                    "isolation": "sidecar",
                    "compatibility": {
                        "kernelApi": ">=1,<2",
                        "runtimeProtocols": ["agentkit.runtime/v1", "2025-06-18"],
                        "python": ">=3.10,<3.15",
                    },
                    "healthContract": "plugin.health/v1",
                    "provenance": {
                        "source": "runtime-native",
                        "digest": manifest_digest,
                        "license": "Apache-2.0",
                        "upstream": {
                            "ecosystem": "dsh",
                            "type": "runtime-native",
                            "requested": DSH_PACKAGE_SPEC,
                            "resolved": DSH_PACKAGE_SPEC,
                        },
                        "components": [
                            {
                                "id": "dsh-profile-mcp",
                                "kind": "mcp",
                                "digest": manifest_digest,
                            }
                        ],
                    },
                },
            }
        ),
    )


def _mapping(value: Any, *, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PluginHostError(code, "expected an object")
    return value


def _required(value: Mapping[str, Any], field: str, *, code: str) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item.strip():
        raise PluginHostError(code, f"{field} must be a non-empty string")
    return item.strip()


def _optional(value: Any, *, code: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PluginHostError(code, "optional string configuration is invalid")
    return value.strip() or None


def _string_list(value: Any, *, code: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise PluginHostError(code, "toolFilter must be an array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise PluginHostError(code, "toolFilter entries must be non-empty strings")
        result.append(item.strip())
    if len(result) != len(set(result)):
        raise PluginHostError(code, "toolFilter entries must be unique")
    return tuple(result)


def _profile_config(profile: CompositionProfile) -> Mapping[str, Any]:
    matches = [item for item in profile.capabilities if item.ref == _PLUGIN_REF]
    if len(matches) != 1:
        raise PluginHostError(
            "dsh_mcp_binding_missing",
            "DSH Profile MCP requires one exact composition capability",
        )
    return cast(Mapping[str, Any], matches[0].config)


def _resource_entry(config: Mapping[str, Any]) -> Mapping[str, Any]:
    resources = config.get("resources")
    if (
        not isinstance(resources, Sequence)
        or isinstance(resources, (str, bytes))
        or len(resources) != 1
    ):
        raise PluginHostError(
            "dsh_mcp_config_invalid",
            "one DSH Profile MCP resource must be selected",
        )
    entry = _mapping(resources[0], code="dsh_mcp_config_invalid")
    if entry.get("kind") != "mcp":
        raise PluginHostError("dsh_mcp_config_invalid", "resource kind must be mcp")
    for field in ("resourceId", "name", "version", "digest"):
        _required(entry, field, code="dsh_mcp_config_invalid")
    return entry


class DshProfileMCPRuntime:
    """Resolve a locked DSH descriptor into one process-scoped MCP spec."""

    def __init__(self, *, service: Any, profile: CompositionProfile) -> None:
        self._service = service
        self._entry = _resource_entry(_profile_config(profile))
        self._descriptor: DshProfileCapabilityDescriptor | None = None
        self._lease: DshMcpConnectorLease | None = None
        self._issued_scopes: dict[str, DshMcpConnectorLease] = {}
        self._revocation_tasks: dict[str, asyncio.Task[None]] = {}
        self._ready = False
        self._disposed = False

    async def start(self) -> None:
        if self._disposed:
            raise PluginHostError("dsh_mcp_disposed", "DSH MCP runtime is disposed")
        await self._refresh_lease()
        self._ready = True

    async def _refresh_lease(self) -> None:
        try:
            snapshot = await self._service.runtime_snapshot()
            descriptor = snapshot.descriptor
            lease = snapshot.lease
        except PluginHostError:
            raise
        except Exception as error:  # noqa: BLE001 - lifecycle service boundary
            raise PluginHostError(
                "dsh_mcp_lease_unavailable", "DSH Profile MCP lease is unavailable"
            ) from error
        self._validate_descriptor(descriptor, lease)
        self._descriptor = descriptor
        self._lease = lease

    async def health(self) -> bool:
        return self._ready and not self._disposed and self._lease is not None

    async def drain(self) -> None:
        self._ready = False

    async def dispose(self) -> None:
        self._ready = False
        self._disposed = True
        scopes = tuple(self._issued_scopes)
        first_error: BaseException | None = None
        for token in scopes:
            try:
                await self._revoke_tracked_scope(token)
            except BaseException as error:  # revocation must continue
                if first_error is None:
                    first_error = error
        self._descriptor = None
        self._lease = None
        if first_error is not None:
            raise first_error

    async def harness_mcp_specs(
        self, bundle: ResolvedPluginBundle
    ) -> tuple[McpToolSpec, ...]:
        if not self._ready or self._disposed:
            raise PluginHostError("dsh_mcp_unavailable", "DSH Profile MCP is not ready")
        # A supervised DSH sidecar may have restarted since this PluginHost
        # graph was staged.  Resolve and fence the lease at every new Agent
        # activation so stale ports or credentials never escape into a client.
        await self._refresh_lease()
        if self._descriptor is None or self._lease is None:  # pragma: no cover - invariant
            raise PluginHostError("dsh_mcp_unavailable", "DSH Profile MCP is not ready")
        resolved = self._resolved_resource(bundle)
        self._validate_resolved_resource(resolved)
        materializer = _mapping(
            self._entry.get("materializer"), code="dsh_mcp_config_invalid"
        )
        tool_filter = _string_list(
            materializer.get("toolFilter"), code="dsh_mcp_tool_filter_invalid"
        )
        known_tools = {tool.name for tool in self._descriptor.tools}
        unknown = sorted(set(tool_filter) - known_tools)
        if unknown:
            raise PluginHostError(
                "dsh_mcp_tool_filter_invalid",
                f"DSH toolFilter contains unknown tools: {', '.join(unknown)}",
            )
        prefix = _optional(
            materializer.get("toolNamePrefix"),
            code="dsh_mcp_config_invalid",
        )
        aliases = {
            dsh_harness_tool_alias(name, prefix): name for name in tool_filter
        }
        if len(aliases) != len(tool_filter):
            raise PluginHostError(
                "dsh_mcp_tool_alias_collision",
                "selected DSH tools produce duplicate Harness aliases",
            )
        resource_name = _required(resolved, "name", code="dsh_mcp_resource_invalid")
        token = self._lease.bearer_token_for_runtime(aliases)
        self._issued_scopes[token] = self._lease
        return (
            McpToolSpec(
                name=resource_name,
                url=self._lease.endpoint,
                api_key=token,
                tool_filter=tuple(aliases),
                tool_name_prefix=None,
            ),
        )

    async def release_harness_mcp_specs(
        self,
        specs: Sequence[McpToolSpec],
    ) -> None:
        """Revoke every activation-scoped credential projected in ``specs``."""

        first_error: BaseException | None = None
        for spec in specs:
            token = spec.api_key
            if token is None:
                continue
            if token not in self._issued_scopes:
                continue
            try:
                await self._revoke_tracked_scope(token)
            except BaseException as error:  # revocation must continue
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    async def _revoke_tracked_scope(self, token: str) -> None:
        lease = self._issued_scopes.get(token)
        if lease is None:
            return
        task = self._revocation_tasks.get(token)
        if task is None:
            task = asyncio.create_task(self._revoke_scope(lease, token))
            self._revocation_tasks[token] = task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # The owned task keeps the security action alive. The token stays
            # tracked until revocation succeeds or a later dispose retries it.
            raise
        finally:
            if task.done():
                self._revocation_tasks.pop(token, None)
        task.result()
        self._issued_scopes.pop(token, None)

    async def _revoke_scope(self, lease: DshMcpConnectorLease, token: str) -> None:
        revoke = getattr(self._service, "revoke_runtime_token", None)
        if not callable(revoke):
            raise PluginHostError(
                "dsh_mcp_revocation_unavailable",
                "DSH capability lifecycle service cannot revoke runtime credentials",
            )
        await revoke(lease, token)

    def _validate_descriptor(
        self,
        descriptor: DshProfileCapabilityDescriptor,
        lease: DshMcpConnectorLease,
    ) -> None:
        materializer = _mapping(
            self._entry.get("materializer"), code="dsh_mcp_config_invalid"
        )
        expected = {
            "profile": descriptor.profile,
            "profileDigest": descriptor.profile_digest,
            "descriptorDigest": descriptor.descriptor_digest,
            "inventoryDigest": descriptor.inventory_digest,
        }
        if any(materializer.get(key) != value for key, value in expected.items()):
            raise PluginHostError(
                "dsh_mcp_descriptor_mismatch",
                "DSH Profile changed after this Agent Build was compiled",
            )
        if (
            _required(self._entry, "version", code="dsh_mcp_config_invalid")
            != descriptor.dsh_version
        ):
            raise PluginHostError(
                "dsh_mcp_descriptor_mismatch", "DSH runtime version changed after build"
            )
        if (
            lease.profile != descriptor.profile
            or lease.profile_digest != descriptor.profile_digest
            or lease.descriptor_digest != descriptor.descriptor_digest
        ):
            raise PluginHostError(
                "dsh_mcp_lease_mismatch", "DSH MCP lease does not match the locked descriptor"
            )

    def _resolved_resource(self, bundle: ResolvedPluginBundle) -> Mapping[str, Any]:
        capabilities = _mapping(
            bundle.resolved_agent_spec.get("capabilities"),
            code="dsh_mcp_bundle_invalid",
        )
        raw = capabilities.get("mcpServers")
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise PluginHostError("dsh_mcp_bundle_invalid", "mcpServers is missing")
        matches = [
            _mapping(item, code="dsh_mcp_bundle_invalid")
            for item in raw
            if isinstance(item, Mapping)
            and item.get("name") == self._entry.get("name")
            and item.get("version") == self._entry.get("version")
            and item.get("digest") == self._entry.get("digest")
        ]
        if len(matches) != 1:
            raise PluginHostError(
                "dsh_mcp_resource_mismatch",
                "locked DSH MCP resource is absent from the resolved Agent spec",
            )
        return matches[0]

    def _validate_resolved_resource(self, resolved: Mapping[str, Any]) -> None:
        assert self._descriptor is not None
        expected = {
            "transport": "http",
            "materialization": "dsh-profile",
            "profile": self._descriptor.profile,
            "profileDigest": self._descriptor.profile_digest,
            "descriptorDigest": self._descriptor.descriptor_digest,
            "inventoryDigest": self._descriptor.inventory_digest,
        }
        if any(resolved.get(key) != value for key, value in expected.items()):
            raise PluginHostError(
                "dsh_mcp_resource_mismatch", "resolved DSH MCP metadata changed after build"
            )
        arguments = resolved.get("args")
        environment = resolved.get("envRefs")
        if (
            resolved.get("endpointUrl") is not None
            or resolved.get("command") is not None
            or arguments not in (None, (), [])
            or (environment is not None and (not isinstance(environment, Mapping) or environment))
        ):
            raise PluginHostError(
                "dsh_mcp_secret_persisted",
                "resolved DSH MCP must not contain runtime connection material",
            )


class DshProfileMCPFactory:
    """Stage the platform adapter against Studio's shared Profile service."""

    def __init__(self, service: Any) -> None:
        self._service = service

    async def stage(
        self,
        manifest: PluginManifest,
        *,
        profile: CompositionProfile,
        services: Mapping[str, Any],
    ) -> DshProfileMCPRuntime:
        del services
        service = self._service
        if (
            manifest.metadata.id != DSH_PROFILE_MCP_PLUGIN_ID
            or manifest.metadata.version != DSH_PROFILE_MCP_PLUGIN_VERSION
        ):
            raise PluginHostError("dsh_mcp_manifest_mismatch", "DSH MCP manifest mismatch")
        if service is None:
            raise PluginHostError(
                "dsh_mcp_service_unavailable", "DSH capability lifecycle service is unavailable"
            )
        return DshProfileMCPRuntime(service=service, profile=profile)


__all__ = [
    "DSH_PROFILE_MCP_PLUGIN_ID",
    "DSH_PROFILE_MCP_PLUGIN_VERSION",
    "DSH_PROFILE_TOOL_PERMISSION",
    "DshProfileMCPFactory",
    "DshProfileMCPRuntime",
    "dsh_profile_mcp_manifest",
    "dsh_harness_tool_alias",
]
