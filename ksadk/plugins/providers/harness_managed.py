"""Managed Harness assembly for the DSH Harness Provider.

The DSH layer owns discovery and lifecycle.  This module converts its locked
capability contributions into the immutable ``HarnessSpec`` and runtime
objects consumed by the LangGraph-based Managed Agent Loop.
"""

from __future__ import annotations

import hashlib
import inspect
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ksadk.harness.artifact_store import ArtifactStore
from ksadk.harness.capabilities import (
    CapabilityDescriptor,
    CapabilityKind,
    LoadPolicy,
    RiskLevel,
)
from ksadk.harness.config import HarnessConfig, McpToolSpec
from ksadk.harness.context_engine import HarnessContextEngine
from ksadk.harness.engine.langgraph import ManagedLangGraphEngine, memory_checkpointer
from ksadk.harness.execution_policy import ExecutionPolicyResolver
from ksadk.harness.managed_runtime import ManagedHarnessRuntimeAdapter
from ksadk.harness.mcp_runtime import McpCapabilityRuntime, McpServerBinding
from ksadk.harness.reasoner import HarnessReasoner
from ksadk.harness.skill_runtime import SkillManifest, SkillRuntime
from ksadk.harness.spec import (
    CapabilityBinding,
    CapabilityBindings,
    ExecutionStrategyKind,
    ExecutionStrategySpec,
    HarnessSpec,
    ModelBinding,
    PromptSpec,
    SubAgentBinding,
)
from ksadk.harness.tools import HarnessTool, load_mcp_tools
from ksadk.plugins.providers.harness_delegation import AdaptiveDelegationRuntime
from ksadk.plugins.subagent_providers.codex import (
    DEFAULT_CODEX_CHILD_PROVIDER_REF,
    CodexOneShotSubagentProvider,
)
from ksadk.plugins.subagents import SubagentProviderRouter

_AUTONOMOUS_CAPABILITY_INSTRUCTIONS = """
根据用户目标自主选择已提供的 Tool、Skill、MCP 与子智能体；不要要求用户指定内部能力名称。
当任务包含两个或更多彼此独立、适合并行处理的工作流时，主动委派子任务。
为每个子任务提供不超过 16 字的职责标签。
编码、仓库修改、调试和测试类子任务标记为 coding，其余调研与分析类子任务标记为 general。
简单任务直接完成，不要为展示能力而委派。
""".strip()


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
    execution_policy_resolver: ExecutionPolicyResolver | None = None,
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
        if state_dir is not None else workspace_root
    )
    if bundle_root is not None and tool_workspace.resolve().is_relative_to(bundle_root.resolve()):
        if any(
            item.get("enabled", True) and item.get("executor", "builtin") == "builtin"
            for item in (tool_contracts or {}).get("capabilities", {}).get("tools", ())
        ):
            raise ValueError("内置 Tool 需要 Bundle 之外的可写 state_dir，不能修改不可变运行包")
    tools, approvals = assemble_python_tools(
        bundle_root or workspace_root, tool_contracts or {}, workspace_root=tool_workspace,
        mcp_server_names=frozenset(item.name for item in config.mcp_tools),
    )
    execution = dict((tool_contracts or {}).get("execution") or {})
    strategy = (
        ExecutionStrategyKind.PLAN_EXECUTE
        if execution.get("strategy") == "plan-act-observe"
        else ExecutionStrategyKind.SINGLE_AGENT
    )
    spec = HarnessSpec(
        agent_revision_ref=f"agent-revision://{agent_id}@1",
        model=ModelBinding(profile_ref=f"model-profile://{model_name}@1"),
        # Provider-level orchestration policy is part of the immutable spec so
        # ContextManifest and Prompt Hash describe the exact instructions seen
        # by the model. Users only describe goals; they never need to name an
        # internal tool, Skill or child Provider in their request.
        prompt=PromptSpec(
            instructions=f"{config.prompt}\n\n{_AUTONOMOUS_CAPABILITY_INSTRUCTIONS}".strip()
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
        execution_strategy=ExecutionStrategySpec(
            kind=strategy,
            config={
                "max_parallel_subagents": int(execution.get("maxParallelChildren", 4)),
                "subagent_failure_mode": "partial",
            },
        ),
        sub_agents=tuple(
            SubAgentBinding.model_validate(item)
            for item in (tool_contracts or {}).get("sub_agents", ())
        ),
    )
    artifact_temp = (
        tempfile.TemporaryDirectory(prefix="ksadk-artifacts-") if state_dir is None else None
    )
    artifact_root = (
        Path(artifact_temp.name) if artifact_temp is not None
        else Path(state_dir) / "artifacts" / hashlib.sha256(agent_name.encode()).hexdigest()
    )
    if bundle_root is not None and artifact_root.resolve().is_relative_to(bundle_root.resolve()):
        raise ValueError("ArtifactStore 必须位于不可变 Bundle 之外")
    if state_dir is None and not checkpoint_dsn:
        checkpointer: Any = memory_checkpointer()
        stack = None
    else:
        from ksadk.harness.runtime_server import assemble_checkpoint_stack

        stack = await assemble_checkpoint_stack(state_dir=state_dir, dsn=checkpoint_dsn)
        checkpointer = stack.checkpointer
    artifact_store = ArtifactStore(artifact_root)
    codex_child = CodexOneShotSubagentProvider(
        # Coding children may edit only the mutable per-Agent workspace, never
        # the immutable admitted Bundle.
        project_dir=tool_workspace,
        sandbox_read_only=False,
        base_instructions=(
            "Complete only the delegated coding task inside the selected workspace. "
            "Respect the parent task boundaries and return a concise result with verification."
        ),
    )
    codex_available = await codex_child.available()
    delegation_runtime = AdaptiveDelegationRuntime(
        router=(
            SubagentProviderRouter({DEFAULT_CODEX_CHILD_PROVIDER_REF: codex_child})
            if codex_available
            else None
        ),
        codex_available=codex_available,
        max_children=int(
            execution.get("maxDynamicChildren", 8)
        ),
        child_timeout_seconds=int(
            execution.get("childTimeoutSeconds", 300)
        ),
    )
    engine = ManagedLangGraphEngine(
        reasoner=_BoundModelReasoner(reasoner, spec.model.profile_ref, config.model),
        checkpointer=checkpointer,
        context_engine=HarnessContextEngine(),
        skill_runtime=skill_runtime,
        mcp_runtime=mcp_runtime,
        artifact_store=artifact_store,
        tools=tools,
        approval_required=approvals,
        delegation_runtime=delegation_runtime,
        max_reasoning_turns=int((tool_contracts or {}).get("execution", {}).get("maxSteps", 8)),
    )
    adapter = _PluginManagedHarnessRuntimeAdapter(
        spec,
        reasoner=reasoner,
        workspace_root=workspace_root,
        engine=engine,
        durable=bool(stack and stack.durable),
        shared_across_pods=bool(checkpoint_dsn),
        execution_policy_resolver=execution_policy_resolver,
        transports=transports,
    )
    adapter._checkpoint_stack = stack  # noqa: SLF001 - 生命周期由激活层托管
    adapter._run_store = stack.run_store if stack is not None else None  # noqa: SLF001
    adapter._provider_artifact_store = artifact_store
    adapter._provider_artifact_temp = artifact_temp
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
        try:
            for handle in list(self._external_handles.values()):
                await self._engine.close(handle)
            self._external_handles.clear()
            for transport in self._provider_transports:
                await transport.close()
        finally:
            store = getattr(self, "_provider_artifact_store", None)
            if store is not None:
                store.close()
                self._provider_artifact_store = None
            temporary = getattr(self, "_provider_artifact_temp", None)
            if temporary is not None:
                temporary.cleanup()
                self._provider_artifact_temp = None


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
