"""Provider-neutral projection of PluginHost MCP connector capabilities."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
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


@dataclass
class MCPProjectionLease:
    """Projected connector specs and their activation-owned cleanup."""

    specs: tuple[McpToolSpec, ...]
    owners: tuple[str, ...]
    _cleanup: AsyncExitStack
    _close_task: asyncio.Task[None] | None = None

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
    return MCPProjectionLease(tuple(specs), tuple(owners), cleanup)


__all__ = [
    "ActivationMCPSource",
    "MCPProjectionLease",
    "ReleasableStaticMCPSource",
    "StaticMCPSource",
    "project_mcp_capabilities",
]
