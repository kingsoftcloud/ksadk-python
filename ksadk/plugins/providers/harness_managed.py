"""Managed Harness assembly for the DSH Harness Provider.

The DSH layer owns discovery and lifecycle.  This module converts its locked
capability contributions into the immutable ``HarnessSpec`` and runtime
objects consumed by the LangGraph-based Managed Agent Loop.
"""

from __future__ import annotations

import hashlib
import inspect
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
    ExecutionStrategySpec,
    HarnessSpec,
    ModelBinding,
    PromptSpec,
    SubAgentBinding,
)
from ksadk.harness.tools import HarnessTool, load_mcp_tools


async def build_managed_provider_adapter(
    config: HarnessConfig,
    *,
    agent_name: str,
    workspace_root: Path,
    reasoner: HarnessReasoner,
    skills: Sequence[Any] = (),
    state_dir: str | Path | None = None,
    checkpoint_dsn: str | None = None,
    tool_contracts: dict[str, Any] | None = None,
    bundle_root: Path | None = None,
    execution_policy_resolver: Any = None,
) -> ManagedHarnessRuntimeAdapter:
    """Assemble the DSH contributions behind the canonical Harness adapter.

    ``state_dir``/``checkpoint_dsn`` 触发持久 Checkpoint 装配（SQLite/Postgres，
    由共享 ``assemble_checkpoint_stack`` 分档）；两者皆缺省时保持内存回退。
    """

    model_name = _resource_id(config.model, fallback="provider-model")
    agent_id = _resource_id(agent_name, fallback="provider-agent")
    skill_source = _ProviderSkillSource(skills)
    skill_runtime = SkillRuntime(skill_source) if skill_source.refs else None
    mcp_runtime, transports, mcp_bindings = _mcp_runtime(config.mcp_tools)
    from ksadk.plugins.providers.harness_tools import assemble_python_tools

    tool_workspace = (
        Path(state_dir) / "tool-workspaces" / hashlib.sha256(agent_name.encode()).hexdigest()
        if state_dir is not None
        else workspace_root
    )
    if bundle_root is not None and tool_workspace.resolve().is_relative_to(bundle_root.resolve()):
        if any(
            item.get("enabled", True) and item.get("executor", "builtin") == "builtin"
            for item in (tool_contracts or {}).get("capabilities", {}).get("tools", ())
        ):
            raise ValueError("内置 Tool 需要 Bundle 之外的可写 state_dir，不能修改不可变运行包")
    tools, approvals = assemble_python_tools(
        bundle_root or workspace_root,
        tool_contracts or {},
        workspace_root=tool_workspace,
        mcp_server_names=frozenset(item.name for item in config.mcp_tools),
    )
    spec = HarnessSpec(
        agent_revision_ref=f"agent-revision://{agent_id}@1",
        model=ModelBinding(profile_ref=f"model-profile://{model_name}@1"),
        prompt=PromptSpec(instructions=config.prompt),
        sub_agents=tuple(
            SubAgentBinding.model_validate(item)
            for item in (tool_contracts or {}).get(
                "subAgents", (tool_contracts or {}).get("sub_agents", ())
            )
        ),
        execution_strategy=ExecutionStrategySpec(
            config=dict((tool_contracts or {}).get("execution", {}).get("harnessConfig", {}))
        ),
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
    if state_dir is None and not checkpoint_dsn:
        checkpointer: Any = memory_checkpointer()
        stack = None
    else:
        from ksadk.harness.runtime_server import assemble_checkpoint_stack

        stack = await assemble_checkpoint_stack(state_dir=state_dir, dsn=checkpoint_dsn)
        checkpointer = stack.checkpointer
    engine = ManagedLangGraphEngine(
        reasoner=_BoundModelReasoner(reasoner, spec.model.profile_ref, config.model),
        checkpointer=checkpointer,
        context_engine=HarnessContextEngine(),
        skill_runtime=skill_runtime,
        mcp_runtime=mcp_runtime,
        tools=tools,
        approval_required=approvals,
        max_reasoning_turns=int((tool_contracts or {}).get("execution", {}).get("maxSteps", 8)),
    )
    adapter = _PluginManagedHarnessRuntimeAdapter(
        spec,
        reasoner=reasoner,
        workspace_root=workspace_root,
        engine=engine,
        durable=bool(stack and stack.durable),
        shared_across_pods=bool(checkpoint_dsn),
        transports=transports,
        execution_policy_resolver=execution_policy_resolver,
    )
    adapter._checkpoint_stack = stack  # noqa: SLF001 - 生命周期由激活层托管
    adapter._run_store = stack.run_store if stack is not None else None  # noqa: SLF001
    return adapter


class _BoundModelReasoner:
    """Resolve only this provider's locked model reference, never arbitrary aliases."""

    def __init__(self, delegate: HarnessReasoner, profile_ref: str, model: str) -> None:
        self._delegate = delegate
        self._profile_ref = profile_ref
        self._model = model

    async def complete(self, *, model, prompt, messages, tools, max_output_tokens=None):
        if model != self._profile_ref:
            raise ValueError(f"Unbound model profile: {model}")
        kwargs = dict(model=self._model, prompt=prompt, messages=messages, tools=tools)
        parameters = inspect.signature(self._delegate.complete).parameters
        if "max_output_tokens" in parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()
        ):
            kwargs["max_output_tokens"] = max_output_tokens
        return await self._delegate.complete(**kwargs)


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
        contribution = self._require(skill_id)
        root = getattr(contribution, "resource_root", None)
        if root is None:
            raise FileNotFoundError(
                f"DSH inline Skill {skill_id} has no packaged resource {resource_ref!r}"
            )
        root = Path(root).resolve()
        candidate = (root / resource_ref).resolve()
        if Path(resource_ref).is_absolute() or not candidate.is_relative_to(root):
            raise ValueError("Skill resource path escapes locked Bundle directory")
        if not candidate.is_file():
            raise FileNotFoundError(resource_ref)
        return candidate.read_bytes()

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
