"""Provider-neutral projection of PluginHost MCP connector capabilities."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ksadk.harness.config import McpToolSpec
from ksadk.plugins.bundle import ResolvedPluginBundle
from ksadk.plugins.host import PluginExecutionContext, PluginHostError


@runtime_checkable
class ActivationMCPSource(Protocol):
    """MCP source whose lease is bound to one PluginHost activation."""

    def activation_mcp_specs(
        self,
        bundle: ResolvedPluginBundle,
        *,
        activation_key: str,
    ) -> Sequence[McpToolSpec] | Awaitable[Sequence[McpToolSpec]]: ...

    async def release_activation_mcp_specs(
        self,
        activation_key: str,
        specs: Sequence[McpToolSpec],
    ) -> None: ...


@runtime_checkable
class StaticMCPSource(Protocol):
    """Compatibility seam implemented by existing MCP capability plugins."""

    def harness_mcp_specs(
        self,
        bundle: ResolvedPluginBundle,
    ) -> Sequence[McpToolSpec] | Awaitable[Sequence[McpToolSpec]]: ...


@runtime_checkable
class ReleasableStaticMCPSource(Protocol):
    async def release_harness_mcp_specs(self, specs: Sequence[McpToolSpec]) -> None: ...


@dataclass(frozen=True)
class MCPResourcePin:
    name: str
    generation: str
    descriptor_digest: str


@dataclass(frozen=True)
class MCPRuntimeCredential:
    """Ephemeral host callbacks; never part of a spec, lock or Run snapshot."""

    name: str
    token: Callable[[], Awaitable[str]] = field(repr=False)
    available: Callable[[], Awaitable[bool]] = field(repr=False)


@dataclass
class MCPProjectionLease:
    """Projected connector specs and their activation-owned cleanup."""

    specs: tuple[McpToolSpec, ...]
    owners: tuple[str, ...]
    _cleanup: AsyncExitStack
    _checks: tuple[Callable[[], Awaitable[bool]], ...] = ()
    _activation_available: Callable[[], bool] | None = None
    resource_pins: tuple[MCPResourcePin, ...] = ()
    _close_task: asyncio.Task[None] | None = None
    runtime_credentials: tuple[MCPRuntimeCredential, ...] = ()
    _runtime_checks: tuple[Callable[[], Awaitable[bool]], ...] = ()

    async def available(self) -> bool:
        """Check original owners; never replace specs with a newer lease."""
        return await self._available(self._checks)

    async def runtime_available(self) -> bool:
        """Admit only clients explicitly consuming owned runtime credentials."""
        return await self._available(self._runtime_checks or self._checks)

    async def _available(self, checks: tuple[Callable[[], Awaitable[bool]], ...]) -> bool:
        if self._close_task is not None:
            return False
        try:
            if self._activation_available is not None and not self._activation_available():
                return False
            for check in checks:
                if await check() is not True:
                    return False
            return self._close_task is None and (
                self._activation_available is None or self._activation_available() is True
            )
        except Exception:
            return False

    async def aclose(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._cleanup.aclose())
        await _finish_owned_task(self._close_task)


async def _finish_owned_task(task: asyncio.Task[None]) -> None:
    """Finish a security cleanup before propagating caller cancellation."""

    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    task.result()
    if interrupted:
        raise asyncio.CancelledError


async def project_mcp_capabilities(
    capabilities: PluginExecutionContext,
    bundle: ResolvedPluginBundle,
    *,
    activation_only: bool = False,
) -> MCPProjectionLease:
    """Activate every MCP capability and retain cleanup for the provider."""

    cleanup = AsyncExitStack()
    specs: list[McpToolSpec] = []
    owners: list[str] = []
    checks: list[Callable[[], Awaitable[bool]]] = []
    runtime_checks: list[Callable[[], Awaitable[bool]]] = []
    runtime_credentials: list[MCPRuntimeCredential] = []
    resource_pins: list[MCPResourcePin] = []
    names: set[str] = set()
    try:
        for binding in capabilities.all("mcp.connector/v1"):
            runtime = binding.runtime
            if isinstance(runtime, ActivationMCPSource):
                projected = runtime.activation_mcp_specs(
                    bundle,
                    activation_key=capabilities.activation_key,
                )
                if isinstance(projected, Awaitable):
                    projected = await projected
                projected_specs = tuple(projected)
                cleanup.push_async_callback(
                    runtime.release_activation_mcp_specs,
                    capabilities.activation_key,
                    projected_specs,
                )
            elif isinstance(runtime, StaticMCPSource):
                if activation_only:
                    continue
                projected = runtime.harness_mcp_specs(bundle)
                if isinstance(projected, Awaitable):
                    projected = await projected
                projected_specs = tuple(projected)
                if isinstance(runtime, ReleasableStaticMCPSource):
                    cleanup.push_async_callback(
                        runtime.release_harness_mcp_specs,
                        projected_specs,
                    )
            else:
                raise PluginHostError(
                    "mcp_capability_incompatible",
                    f"plugin {binding.plugin_id} cannot project MCP connector config",
                )
            lease_check = getattr(runtime, "activation_mcp_specs_available", None)
            if callable(lease_check):

                async def check(source=runtime, selected=projected_specs):
                    return await source.activation_mcp_specs_available(selected)

                checks.append(check)
            else:
                checks.append(runtime.health)
            credentials = getattr(runtime, "activation_mcp_runtime_credentials", None)
            selected_credentials = (
                tuple(credentials(projected_specs)) if callable(credentials) else ()
            )
            if any(
                not isinstance(item, MCPRuntimeCredential)
                or item.name not in {spec.name for spec in projected_specs}
                for item in selected_credentials
            ) or len({item.name for item in selected_credentials}) != len(selected_credentials):
                raise PluginHostError("mcp_credential_invalid", "invalid MCP runtime credential")
            if selected_credentials and {item.name for item in selected_credentials} != {
                spec.name for spec in projected_specs
            }:
                raise PluginHostError(
                    "mcp_credential_invalid", "partial MCP runtime credential ownership"
                )
            runtime_credentials.extend(selected_credentials)
            runtime_checks.extend(item.available for item in selected_credentials)
            # A connector with no rotating credential keeps its original probe.
            if not selected_credentials:
                runtime_checks.append(checks[-1])
            resource_refs = getattr(runtime, "activation_mcp_resource_pins", None)
            if callable(resource_refs):
                selected_pins = tuple(resource_refs(projected_specs))
                if any(
                    not isinstance(pin, MCPResourcePin)
                    or pin.name not in {spec.name for spec in projected_specs}
                    for pin in selected_pins
                ):
                    raise PluginHostError("mcp_resource_pin_invalid", "invalid MCP resource pin")
                resource_pins.extend(selected_pins)
            for spec in projected_specs:
                if not isinstance(spec, McpToolSpec) or not spec.name.strip():
                    raise PluginHostError(
                        "mcp_capability_invalid",
                        f"plugin {binding.plugin_id} returned an invalid MCP connector",
                    )
                if spec.name in names:
                    raise PluginHostError(
                        "mcp_capability_ambiguous",
                        f"more than one MCP connector is named {spec.name!r}",
                    )
                names.add(spec.name)
                specs.append(spec)
            owners.append(binding.plugin_id)
    except BaseException:
        close = asyncio.create_task(cleanup.aclose())
        await _finish_owned_task(close)
        raise
    return MCPProjectionLease(
        tuple(specs),
        tuple(owners),
        cleanup,
        tuple(checks),
        capabilities.activation_available,
        tuple(resource_pins),
        runtime_credentials=tuple(runtime_credentials),
        _runtime_checks=tuple(runtime_checks),
    )


__all__ = [
    "ActivationMCPSource",
    "MCPProjectionLease",
    "ReleasableStaticMCPSource",
    "StaticMCPSource",
    "project_mcp_capabilities",
]
