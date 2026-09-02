"""Managed Harness assembly for the DSH Harness Provider.

The DSH layer owns discovery and lifecycle.  This module converts its locked
capability contributions into the immutable ``HarnessSpec`` and runtime
objects consumed by the LangGraph-based Managed Agent Loop.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ksadk.harness.capabilities import (
    CapabilityDescriptor,
    CapabilityKind,
    LoadPolicy,
    RiskLevel,
)
from ksadk.harness.config import HarnessConfig, McpToolSpec
from ksadk.harness.context_engine import HarnessContextEngine
from ksadk.harness.engine.langgraph import ManagedLangGraphEngine, memory_checkpointer
from ksadk.harness.managed_runtime import ManagedHarnessRuntimeAdapter
from ksadk.harness.mcp_runtime import McpCapabilityRuntime, McpServerBinding
from ksadk.harness.reasoner import HarnessReasoner
from ksadk.harness.skill_runtime import SkillManifest, SkillRuntime
from ksadk.harness.spec import (
    CapabilityBinding,
    CapabilityBindings,
    HarnessSpec,
    ModelBinding,
    PromptSpec,
)
from ksadk.harness.tools import HarnessTool, load_mcp_tools


def build_managed_provider_adapter(
    config: HarnessConfig,
    *,
    agent_name: str,
    workspace_root: Path,
    reasoner: HarnessReasoner,
    skills: Sequence[Any] = (),
) -> ManagedHarnessRuntimeAdapter:
    """Assemble the DSH contributions behind the canonical Harness adapter."""

    model_name = _resource_id(config.model, fallback="provider-model")
    agent_id = _resource_id(agent_name, fallback="provider-agent")
    skill_source = _ProviderSkillSource(skills)
    skill_runtime = SkillRuntime(skill_source) if skill_source.refs else None
    mcp_runtime, transports, mcp_bindings = _mcp_runtime(config.mcp_tools)
    spec = HarnessSpec(
        agent_revision_ref=f"agent-revision://{agent_id}@1",
        model=ModelBinding(profile_ref=f"model-profile://{model_name}@1"),
        prompt=PromptSpec(instructions=config.prompt),
        capabilities=CapabilityBindings(
            mcp_bindings=mcp_bindings,
            skill_bindings=tuple(
                CapabilityBinding(
                    capability_ref=skill_ref,
                    required=True,
                    load_policy="on_demand",
                )
                for skill_ref in skill_source.refs
            ),
        ),
    )
    engine = ManagedLangGraphEngine(
        reasoner=reasoner,
        checkpointer=memory_checkpointer(),
        context_engine=HarnessContextEngine(),
        skill_runtime=skill_runtime,
        mcp_runtime=mcp_runtime,
    )
    return _PluginManagedHarnessRuntimeAdapter(
        spec,
        reasoner=reasoner,
        workspace_root=workspace_root,
        engine=engine,
        transports=transports,
    )


class _PluginManagedHarnessRuntimeAdapter(ManagedHarnessRuntimeAdapter):
    """Managed adapter that also owns DSH-created MCP transport resources."""

    def __init__(self, *args: Any, transports: Sequence[Any], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._provider_transports = tuple(transports)

    async def close_all(self) -> None:
        for handle in list(self._external_handles.values()):
            await self._engine.close(handle)
        self._external_handles.clear()
        for transport in self._provider_transports:
            await transport.close()


class _ProviderSkillSource:
    """Expose locked DSH Skill contributions through L0/L1/L2 disclosure."""

    def __init__(self, contributions: Sequence[Any]) -> None:
        self._skills: dict[str, Any] = {}
        for contribution in contributions:
            name = str(getattr(contribution, "name", "")).strip()
            instructions = str(getattr(contribution, "instructions", "")).strip()
            if not name or not instructions:
                continue
            ref = f"skill://{_resource_id(name, fallback='provider-skill')}@1.0.0"
            if ref in self._skills:
                raise ValueError(f"duplicate DSH Skill contribution: {ref}")
            self._skills[ref] = contribution

    @property
    def refs(self) -> tuple[str, ...]:
        return tuple(self._skills)

    def manifest(self, skill_id: str) -> SkillManifest:
        contribution = self._require(skill_id)
        instructions = str(contribution.instructions).strip()
        summary = next((line.strip() for line in instructions.splitlines() if line.strip()), "")
        return SkillManifest(name=str(contribution.name), summary=summary[:512])

    def full_text(self, skill_id: str) -> str:
        return str(self._require(skill_id).instructions)

    def resource(self, skill_id: str, resource_ref: str) -> bytes:
        self._require(skill_id)
        raise FileNotFoundError(
            f"DSH inline Skill {skill_id} has no packaged resource {resource_ref!r}"
        )

    def _require(self, skill_id: str) -> Any:
        try:
            return self._skills[skill_id]
        except KeyError:
            raise KeyError(f"DSH Skill {skill_id!r} is not bound") from None


class _McpToolSpecTransport:
    """Adapt the existing DSH MCP contribution to Managed MCP semantics."""

    def __init__(self, spec: McpToolSpec) -> None:
        self._spec = spec
        self._toolset: Any | None = None
        self._tools: dict[str, HarnessTool] = {}

    async def list_tools(self) -> list[dict[str, Any]]:
        await self._ensure_loaded()
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.parameters,
            }
            for tool in self._tools.values()
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        await self._ensure_loaded()
        try:
            tool = self._tools[name]
        except KeyError:
            raise RuntimeError(f"MCP tool {name!r} is not exposed by {self._spec.name!r}") from None
        return await tool.call(arguments)

    async def close(self) -> None:
        if self._toolset is not None:
            await self._toolset.close()
        self._toolset = None
        self._tools.clear()

    async def _ensure_loaded(self) -> None:
        if self._toolset is not None:
            return
        toolset, tools = await load_mcp_tools(self._spec)
        self._toolset = toolset
        self._tools = {tool.name: tool for tool in tools}


def _mcp_runtime(
    specs: Sequence[McpToolSpec],
) -> tuple[
    McpCapabilityRuntime | None,
    tuple[_McpToolSpecTransport, ...],
    tuple[CapabilityBinding, ...],
]:
    if not specs:
        return None, (), ()
    runtime = McpCapabilityRuntime()
    transports: list[_McpToolSpecTransport] = []
    bindings: list[CapabilityBinding] = []
    seen: set[str] = set()
    for spec in specs:
        ref = f"mcp://{_resource_id(spec.name, fallback='provider-mcp')}@1.0.0"
        if ref in seen:
            raise ValueError(f"duplicate DSH MCP contribution: {ref}")
        seen.add(ref)
        transport = _McpToolSpecTransport(spec)
        runtime.bind(
            McpServerBinding(
                descriptor=CapabilityDescriptor(
                    id=ref,
                    kind=CapabilityKind.MCP,
                    name=spec.name,
                    description=f"DSH-provided MCP server {spec.name}",
                    version="1.0.0",
                    risk_level=RiskLevel.MEDIUM,
                    load_policy=LoadPolicy.ON_DEMAND,
                ),
                transport=transport,
                required=True,
                tool_prefix=spec.tool_name_prefix or "",
            )
        )
        transports.append(transport)
        bindings.append(
            CapabilityBinding(
                capability_ref=ref,
                required=True,
                load_policy="on_demand",
            )
        )
    return runtime, tuple(transports), tuple(bindings)


def _resource_id(value: str, *, fallback: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return normalized or fallback


__all__ = ["build_managed_provider_adapter"]
