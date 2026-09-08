"""Official platform-resource MCP capability for immutable Agent Bundles.

The plugin re-authorizes every activation, sends verified credentials only to
the private resource worker pipe, and projects the resulting scoped Core lease
through the provider-neutral MCP capability ABI.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from ksadk.harness.config import McpToolSpec
from ksadk.plugins.bundle import ResolvedPluginBundle
from ksadk.plugins.contracts import CompositionProfile, PluginManifest
from ksadk.plugins.host import PluginHostError
from ksadk.resource_runtime.build_artifacts import (
    ResourceBuildReference,
    restore_resource_build,
)
from ksadk.resource_runtime.contracts import InvocationIdentity
from ksadk.resource_runtime.leases import ResourceScope
from ksadk.resource_runtime.supervisor import ActiveResources
from ksadk.resource_runtime.worker import KnowledgeWorkerBinding, WorkerInitialization
from ksadk.studio.errors import StudioError
from ksadk.studio.resource_authority import AdmittedResourceAccess

PLATFORM_RESOURCE_MCP_PLUGIN_ID = "io.ksadk.mcp.platform-resources"
PLATFORM_RESOURCE_MCP_PLUGIN_VERSION = "1.0.0"
PLATFORM_RESOURCE_MCP_PERMISSION = "platform-resource:read"
PLATFORM_RESOURCE_MCP_REF = (
    f"plugin://{PLATFORM_RESOURCE_MCP_PLUGIN_ID}@{PLATFORM_RESOURCE_MCP_PLUGIN_VERSION}"
)
_ARTIFACT_PATH = "platform-resources"
_CONNECTOR_NAME = "platform-resources"
_KNOWLEDGE_OPERATION = "search_knowledge_base"


def _manifest_digest() -> str:
    payload = (
        f"{PLATFORM_RESOURCE_MCP_REF}:activation-mcp/v1:resource-worker/v1"
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def platform_resource_mcp_manifest() -> PluginManifest:
    return cast(
        PluginManifest,
        PluginManifest.model_validate(
            {
                "metadata": {
                    "id": PLATFORM_RESOURCE_MCP_PLUGIN_ID,
                    "version": PLATFORM_RESOURCE_MCP_PLUGIN_VERSION,
                },
                "spec": {
                    "domain": "ksadk-platform",
                    "runtime": "python",
                    "entrypoint": (
                        "ksadk.plugins.providers.platform_resources:"
                        "PlatformResourceMCPFactory"
                    ),
                    "provides": [
                        {
                            "definition": "mcp.connector/v1",
                            "slot": "mcp.platform-resources",
                            "mode": "multiple",
                        }
                    ],
                    "permissions": [
                        "network:mcp",
                        PLATFORM_RESOURCE_MCP_PERMISSION,
                        "process:resource-worker",
                    ],
                    "isolation": "process",
                    "compatibility": {
                        "kernelApi": ">=1,<2",
                        "runtimeProtocols": ["agentkit.runtime/v1", "2025-06-18"],
                        "python": ">=3.10,<3.15",
                    },
                    "healthContract": "plugin.health/v1",
                    "provenance": {
                        "source": "builtin",
                        "digest": _manifest_digest(),
                        "license": "Apache-2.0",
                    },
                },
            }
        ),
    )


def platform_resource_capability_selected(profile: CompositionProfile) -> bool:
    return any(item.ref == PLATFORM_RESOURCE_MCP_REF for item in profile.capabilities)


def _profile_config(profile: CompositionProfile) -> Mapping[str, Any]:
    matches = [item for item in profile.capabilities if item.ref == PLATFORM_RESOURCE_MCP_REF]
    if len(matches) != 1 or matches[0].config != {"artifactPath": _ARTIFACT_PATH}:
        raise PluginHostError(
            "platform_resource_binding_invalid",
            "platform resource capability requires its fixed Bundle artifact path",
        )
    return matches[0].config


def _runtime_identity(
    *,
    access: AdmittedResourceAccess,
    actor_ref: str,
    agent_id: str,
    activation_key: str,
) -> InvocationIdentity:
    session_digest = hashlib.sha256(activation_key.encode()).hexdigest()[:40]
    return InvocationIdentity(
        tenant_ref=access.authority.tenant_ref,
        resource_principal_ref=access.authority.resource_principal_ref,
        actor_ref=actor_ref,
        memory_subject_ref=actor_ref,
        agent_id=agent_id,
        session_ref=f"session-{session_digest}",
    )


def _validate_access(frozen: Any, access: AdmittedResourceAccess) -> None:
    grant = access.authority
    if (
        grant.connection_ref != frozen.connection.connection_ref
        or grant.connection_revision != frozen.connection_revision
        or grant.tenant_ref != frozen.connection.tenant_ref
        or grant.resource_principal_ref != frozen.connection.principal_ref
        or grant.resource != frozen.config.binding.resource
        or grant.data_endpoint != frozen.connection.endpoint
        or grant.expires_at <= datetime.now(timezone.utc)
        or tuple(grant.allowed_operations) != (_KNOWLEDGE_OPERATION,)
    ):
        raise PluginHostError(
            "platform_resource_authority_mismatch",
            "runtime resource authority does not match the immutable Build",
        )


@dataclass(frozen=True)
class _OwnedActivation:
    resources: ActiveResources
    spec: McpToolSpec


class PlatformResourceMCPRuntime:
    """Activation-scoped resource worker and DSH Core lease owner."""

    def __init__(
        self,
        *,
        profile: CompositionProfile,
        authority: Any,
        connections: Any,
        dsh_service: Any,
        actor_ref: str,
        state_root: Path,
    ) -> None:
        _profile_config(profile)
        if not callable(getattr(authority, "admit_runtime", None)):
            raise PluginHostError(
                "platform_resource_authority_unavailable",
                "platform resource runtime requires credential-bound authority admission",
            )
        if not actor_ref or any(character.isspace() for character in actor_ref):
            raise PluginHostError(
                "platform_resource_identity_invalid",
                "platform resource runtime requires a trusted actor identity",
            )
        self._authority = authority
        self._connections = connections
        self._dsh = dsh_service
        self._actor_ref = actor_ref
        self._state_root = state_root.resolve()
        self._lock = asyncio.Lock()
        self._owned: dict[str, _OwnedActivation] = {}
        self._cleanup_tasks: dict[str, asyncio.Task[None]] = {}
        self._ready = False
        self._disposed = False

    async def start(self) -> None:
        if self._disposed:
            raise PluginHostError(
                "platform_resource_disposed",
                "platform resource capability is disposed",
            )
        self._state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._ready = True

    async def health(self) -> bool:
        return self._ready and not self._disposed

    async def drain(self) -> None:
        self._ready = False

    async def dispose(self) -> None:
        self._ready = False
        first_error: BaseException | None = None
        async with self._lock:
            keys = tuple(self._owned)
        for activation_key in keys:
            try:
                await self._release(activation_key)
            except BaseException as error:
                if first_error is None:
                    first_error = error
        self._disposed = True
        if first_error is not None:
            raise first_error

    async def activation_mcp_specs(
        self,
        bundle: ResolvedPluginBundle,
        *,
        activation_key: str,
    ) -> tuple[McpToolSpec, ...]:
        if not self._ready or self._disposed:
            raise PluginHostError(
                "platform_resource_unavailable",
                "platform resource capability is not ready",
            )
        if not activation_key:
            raise PluginHostError(
                "platform_resource_activation_key_missing",
                "platform resources require a host-owned activation key",
            )
        async with self._lock:
            if activation_key in self._owned:
                raise PluginHostError(
                    "platform_resource_activation_duplicate",
                    "platform resources are already active for this activation",
                )

        manifest = bundle.manifest
        if not manifest.resource_build_digest or not manifest.resource_snapshot_digest:
            raise PluginHostError(
                "platform_resource_build_missing",
                "Bundle has no immutable platform resource artifact",
            )
        reference = ResourceBuildReference(
            digest=manifest.resource_build_digest,
            snapshot_digest=manifest.resource_snapshot_digest,
        )
        try:
            with tempfile.TemporaryDirectory(
                prefix="resource-restore-",
                dir=self._state_root,
            ) as cache:
                restored, packages = await asyncio.to_thread(
                    restore_resource_build,
                    bundle.root / _ARTIFACT_PATH,
                    reference,
                    cache_directory=Path(cache),
                )
            if packages:
                raise PluginHostError(
                    "platform_resource_runtime_unsupported",
                    "this runtime slice supports knowledge resources only",
                )
            snapshot = restored.snapshot
            if snapshot.dsh_profile is None:
                raise PluginHostError(
                    "platform_resource_profile_missing",
                    "resource Build has no locked DSH Profile",
                )

            accesses: list[AdmittedResourceAccess] = []
            for frozen in snapshot.bindings:
                if frozen.config.binding.resource.kind != "knowledge-base":
                    raise PluginHostError(
                        "platform_resource_runtime_unsupported",
                        "this runtime slice supports knowledge resources only",
                    )
                current = await asyncio.to_thread(
                    self._connections.get,
                    frozen.connection.connection_ref,
                )
                if (
                    current.target != frozen.connection
                    or current.revision != frozen.connection_revision
                ):
                    raise PluginHostError(
                        "platform_resource_connection_changed",
                        "resource connection changed after this Build",
                    )
                access = await asyncio.to_thread(
                    self._authority.admit_runtime,
                    frozen.config,
                    expected_connection_revision=frozen.connection_revision,
                )
                if not isinstance(access, AdmittedResourceAccess):
                    raise PluginHostError(
                        "platform_resource_authority_invalid",
                        "resource authority returned an invalid runtime admission",
                    )
                _validate_access(frozen, access)
                if await asyncio.to_thread(
                    self._connections.get,
                    frozen.connection.connection_ref,
                ) != current:
                    raise PluginHostError(
                        "platform_resource_connection_changed",
                        "resource connection changed during activation",
                    )
                accesses.append(access)

            if not accesses:
                raise PluginHostError(
                    "platform_resource_build_empty",
                    "resource Build contains no runtime bindings",
                )
            identity = _runtime_identity(
                access=accesses[0],
                actor_ref=self._actor_ref,
                agent_id=manifest.agent_id,
                activation_key=activation_key,
            )
            if any(
                access.authority.tenant_ref != identity.tenant_ref
                or access.authority.resource_principal_ref
                != identity.resource_principal_ref
                for access in accesses
            ):
                raise PluginHostError(
                    "platform_resource_identity_mixed",
                    "one activation cannot mix resource principals",
                )

            descriptor, generation_id = await self._dsh.prepare_resource_generation(
                snapshot.dsh_profile
            )
            activation_id = "resource-" + secrets.token_hex(24)
            scopes = tuple(
                ResourceScope(
                    profile_digest=descriptor.profile_digest,
                    generation_id=generation_id,
                    activation_id=activation_id,
                    build_digest=manifest.bundle_digest,
                    binding_snapshot_digest=snapshot.digest,
                    identity=identity,
                    binding_id=frozen.config.binding.id,
                    allowed_operations=access.authority.allowed_operations,
                )
                for frozen, access in zip(snapshot.bindings, accesses, strict=True)
            )
            bindings = []
            for frozen, access in zip(snapshot.bindings, accesses, strict=True):
                credentials = access.credentials
                if credentials.access_key is None or credentials.secret_key is None:
                    raise PluginHostError(
                        "platform_resource_credentials_invalid",
                        "knowledge resource admission did not return signed credentials",
                    )
                bindings.append(
                    KnowledgeWorkerBinding(
                        config=frozen.config,
                        endpoint=frozen.connection.endpoint,
                        access_key=credentials.access_key,
                        secret_key=credentials.secret_key,
                        session_token=credentials.session_token or "",
                    )
                )
            active = await self._dsh.activate_resources(
                WorkerInitialization(
                    resource_snapshot=snapshot,
                    bindings=tuple(bindings),
                    scopes=scopes,
                ),
                expected=snapshot.dsh_profile,
            )
            try:
                connector = await self._dsh.resource_connector_lease(active)
                token = connector.resource_bearer_token(
                    {_KNOWLEDGE_OPERATION: _KNOWLEDGE_OPERATION},
                    active.leases,
                )
                spec = McpToolSpec(
                    name=_CONNECTOR_NAME,
                    url=connector.endpoint,
                    api_key=token,
                    tool_filter=(_KNOWLEDGE_OPERATION,),
                )
            except BaseException:
                await self._dsh.deactivate_resources(active.activation_id)
                raise
        except (PluginHostError, asyncio.CancelledError):
            raise
        except StudioError as error:
            raise PluginHostError(error.code.lower(), str(error)) from error
        except Exception as error:
            raise PluginHostError(
                "platform_resource_activation_failed",
                "platform resource activation failed",
            ) from error

        async with self._lock:
            if self._disposed or not self._ready:
                await self._dsh.deactivate_resources(active.activation_id)
                raise PluginHostError(
                    "platform_resource_unavailable",
                    "platform resource capability stopped during activation",
                )
            self._owned[activation_key] = _OwnedActivation(active, spec)
        return (spec,)

    async def release_activation_mcp_specs(
        self,
        activation_key: str,
        specs: Sequence[McpToolSpec],
    ) -> None:
        async with self._lock:
            owned = self._owned.get(activation_key)
        if owned is None:
            return
        if tuple(specs) != (owned.spec,):
            raise PluginHostError(
                "platform_resource_release_mismatch",
                "platform resource cleanup does not match its activation lease",
            )
        await self._release(activation_key)

    async def _release(self, activation_key: str) -> None:
        async with self._lock:
            owned = self._owned.get(activation_key)
            if owned is None:
                return
            task = self._cleanup_tasks.get(activation_key)
            if task is None:
                task = asyncio.create_task(
                    self._dsh.deactivate_resources(owned.resources.activation_id)
                )
                self._cleanup_tasks[activation_key] = task
        try:
            await asyncio.shield(task)
        finally:
            if task.done():
                async with self._lock:
                    self._cleanup_tasks.pop(activation_key, None)
                    if not task.cancelled() and task.exception() is None:
                        self._owned.pop(activation_key, None)
        task.result()


class PlatformResourceMCPFactory:
    def __init__(self) -> None:
        self.runtime: PlatformResourceMCPRuntime | None = None

    async def stage(
        self,
        manifest: PluginManifest,
        *,
        profile: CompositionProfile,
        services: Mapping[str, Any],
    ) -> PlatformResourceMCPRuntime:
        if manifest.metadata.id != PLATFORM_RESOURCE_MCP_PLUGIN_ID:
            raise PluginHostError(
                "platform_resource_manifest_invalid",
                "platform resource factory received another plugin manifest",
            )
        state_root = services.get("runtime_state_root")
        if state_root is None:
            raise PluginHostError(
                "platform_resource_state_unavailable",
                "platform resource runtime state root is unavailable",
            )
        self.runtime = PlatformResourceMCPRuntime(
            profile=profile,
            authority=services.get("resource_authority"),
            connections=services.get("resource_connections"),
            dsh_service=services.get("dsh_capability_service"),
            actor_ref=str(services.get("resource_actor_ref") or ""),
            state_root=Path(str(state_root)) / "platform-resources",
        )
        return self.runtime


__all__ = [
    "PLATFORM_RESOURCE_MCP_PERMISSION",
    "PLATFORM_RESOURCE_MCP_PLUGIN_ID",
    "PLATFORM_RESOURCE_MCP_PLUGIN_VERSION",
    "PLATFORM_RESOURCE_MCP_REF",
    "PlatformResourceMCPFactory",
    "PlatformResourceMCPRuntime",
    "platform_resource_capability_selected",
    "platform_resource_mcp_manifest",
]
