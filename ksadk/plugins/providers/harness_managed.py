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
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ksadk.harness.artifact_store import ArtifactStore
from ksadk.harness.config import HarnessConfig
from ksadk.harness.context_engine import HarnessContextEngine
from ksadk.harness.engine.langgraph import ManagedLangGraphEngine, memory_checkpointer
from ksadk.harness.execution_policy import ExecutionPolicyResolver
from ksadk.harness.managed_runtime import ManagedHarnessRuntimeAdapter
from ksadk.harness.reasoner import HarnessReasoner, HarnessReasoningTurn
from ksadk.harness.skill_intent import is_skill_binding_question
from ksadk.harness.skill_runtime import SkillRuntime
from ksadk.harness.spec import (
    CapabilityBinding,
    CapabilityBindings,
    ExecutionStrategyKind,
    ExecutionStrategySpec,
    HarnessSpec,
    ModelBinding,
    ModelProviderPolicy,
    PromptSpec,
    SubAgentBinding,
)
from ksadk.plugins.providers.harness_contributions import (
    ProviderSkillSource as _ProviderSkillSource,
)
from ksadk.plugins.providers.harness_contributions import (
    mcp_runtime as _mcp_runtime,
)
from ksadk.plugins.providers.harness_contributions import (
    resource_id as _resource_id,
)
from ksadk.plugins.providers.harness_delegation import AdaptiveDelegationRuntime
from ksadk.plugins.subagent_providers.codex import (
    DEFAULT_CODEX_CHILD_PROVIDER_REF,
    CodexOneShotSubagentProvider,
)
from ksadk.plugins.subagents import SubagentProviderRouter

_AUTONOMOUS_CAPABILITY_INSTRUCTIONS = """
根据用户目标自主选择已提供的 Tool、Skill、MCP 与子智能体；不要要求用户指定内部能力名称。
执行前先判断交付意图是否足够明确。若同一句请求存在会显著改变交付物的多个合理解释
（例如网页、桌面或命令行应用），先只问一个能决定后续路线的问题，并给出 2～3 个简短选项；
这一轮不要读取工作区、Skill、MCP，也不要委派。用户已经明确平台、输出形式或允许你自行决定时，
不要重复确认。
Skill/MCP 是按需能力，不是启动清单：仅当其摘要与当前目标直接相关，或普通工具无法完成任务时才读取。
标记为“推荐”的 Skill 已由确定性元数据匹配当前目标，必须在调用其他工具前依次读取它的 Manifest
和完整说明并按说明执行；标记为“未推荐”的 Skill 默认不得读取。不要为了证明能力存在而枚举、读取
或调用无关资源。
当用户要求安装或绑定当前未提供的公开 Skill 时，不要把“当前 Agent 未绑定”误报为“网上不存在”。
若提供了 skill_discover_github、skill_install、skill_bind_agent，
依次完成搜索、锁定 commit、风险检查、
经审批安装、绑定当前 Agent；不要跳过检查直接执行远程代码。绑定的新 Skill 从下一轮运行生效。
只有这些治理工具确实不可用时，才引导用户去 Studio「资源库 → Skill → 发现 Skill」手工完成。
**默认自己把任务做完**，只有子任务确实彼此独立、且委派能减少相互干扰时才使用 delegate_task；
对象简单、路径清晰、只需少量工具调用时不要委派（参照 DeepAgents subagents.py:333
"It is better to just complete the task directly and NOT use the task tool"）。
不要为了展示编排能力而委派，也不要把"已委派"当成"已完成"。
为每个子任务提供不超过 16 字的职责标签。
编码、仓库修改、调试和测试类子任务标记为 coding，其余调研与分析类子任务标记为 general。
**不要叙述你打算做什么，直接做**（参照 Claude Code prompts.ts:832 "Do not narrate what you're
about to do — just do it."）；先把交付物写出来，再迭代改进（"don't gold-plate, but don't
leave it half-done"）。工具、文件动作已经明确时应立即调用；动作前的内部推理保持简短，
不要为了完善计划长时间反复推演，也不要在没有新证据时重复同一动作。
当用户明确要求生成、输出或保存某种格式的文件时，必须调用已提供的工作区写入能力
创建真实文件，并在最终答复中给出文件名；只在对话中展示同格式文本不算完成。
子智能体只返回结论与分析，**不会替你产出交付物**；委派不能替代落盘——
凡是委派出去的实现/生成类工作，父智能体都必须在子任务结束后**自己**调用工作区写入能力
把真实文件写进目标目录，并确认文件确实存在；子任务返回"需要调整"时由你负责补齐，
不得以"子智能体已结束"作为交付完成的依据。
验证工具返回 ``ok`` 只表示该工具调用本身成功，不等于用户目标已经验收通过。
例如浏览器检查没有 console error 仍可能得到空白 canvas；若结果包含 visual_warnings、
失败测试或未验证的关键交互，必须继续修复和复验，或诚实作为部分结果收口，不得宣称完成。
""".strip()


async def build_managed_provider_adapter(
    config: HarnessConfig,
    *,
    agent_name: str,
    workspace_root: Path,
    user_workspace_root: Path | None = None,
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
    fallback_model_names = tuple(
        str(item.get("model") or "").strip()
        for item in (tool_contracts or {}).get("fallbackModels", ())
        if isinstance(item, Mapping) and str(item.get("model") or "").strip()
    )
    model_refs = {
        f"model-profile://{model_name}@1": config.model,
        **{
            f"model-profile://{_resource_id(name, fallback='provider-model')}@1": name
            for name in fallback_model_names
            if name != config.model
        },
    }
    primary_model_ref = next(iter(model_refs))
    agent_id = _resource_id(agent_name, fallback="provider-agent")
    skill_source = _ProviderSkillSource(skills)
    skill_runtime = SkillRuntime(skill_source) if skill_source.refs else None
    mcp_runtime, transports, mcp_bindings = _mcp_runtime(config.mcp_tools)
    from ksadk.plugins.providers.harness_tools import assemble_python_tools

    # 文件工具默认写到用户当前选择的 Studio 工作区；仅当工作区缺失或
    # 落在不可变 Bundle 内部时才退回 state_dir 的哈希目录。
    tool_workspace = user_workspace_root or workspace_root
    if (
        tool_workspace is None
        or (
            bundle_root is not None
            and Path(tool_workspace).resolve().is_relative_to(Path(bundle_root).resolve())
        )
    ) and state_dir is not None:
        tool_workspace = (
            Path(state_dir) / "tool-workspaces" / hashlib.sha256(agent_name.encode()).hexdigest()
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
    strategy_config: dict[str, Any] = {
        "max_parallel_subagents": int(execution.get("maxParallelChildren", 4)),
        "subagent_failure_mode": "partial",
    }
    # Studio agents already declare deterministic step/time limits. Project
    # those limits into the Harness run controller as well so its stagnation
    # guard is active. Without a controller, a model can successfully repeat
    # the same write under fresh call ids until the outer timeout expires.
    run_limits: dict[str, Any] = {}
    if execution.get("maxSteps") is not None:
        run_limits["max_model_calls"] = max(1, int(execution["maxSteps"]))
    if execution.get("timeoutSeconds") is not None:
        run_limits["max_wall_seconds"] = max(1.0, float(execution["timeoutSeconds"]))
    if run_limits:
        strategy_config["run_control"] = {
            "objective": "Complete the current user request with verified tool evidence.",
            "limits": run_limits,
            "stagnation": {
                "action_repeat_threshold": 3,
                "error_repeat_threshold": 2,
                "max_replans": 2,
                "fingerprint_window": 12,
            },
        }
    model_failover_mode = str((tool_contracts or {}).get("modelFailoverMode") or "strict")
    spec = HarnessSpec(
        agent_revision_ref=f"agent-revision://{agent_id}@1",
        model=ModelBinding(
            profile_ref=primary_model_ref,
            fallback_profile_refs=tuple(ref for ref in model_refs if ref != primary_model_ref),
            provider_policy=ModelProviderPolicy(
                allow_failover=model_failover_mode == "automatic"
            ),
        ),
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
            config=strategy_config,
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
    run_timeout_seconds = int(execution.get("timeoutSeconds", 120))
    child_timeout_seconds = min(
        run_timeout_seconds,
        int(execution.get("childTimeoutSeconds", run_timeout_seconds)),
    )
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
        # A delegated child is part of the parent run and must not outlive the
        # Agent's declared execution deadline.  The previous 300s fallback
        # silently ignored Studio's common 120s timeout and made one slow
        # research child hold the whole turn open for five minutes.
        child_timeout_seconds=child_timeout_seconds,
        # ``modelParameters.maxTokens`` is a per-call output ceiling, not a
        # whole-child input+output budget.  A 4096 default here made ordinary
        # research children fail as soon as search results were added to their
        # next prompt.  Keep the total budget opt-in; an explicitly configured
        # child ceiling and any parent run ceiling are still enforced.
        child_max_total_tokens=(
            int(execution["childMaxTotalTokens"])
            if execution.get("childMaxTotalTokens") is not None
            else None
        ),
    )
    engine = ManagedLangGraphEngine(
        reasoner=_BoundModelsReasoner(reasoner, model_refs),
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


class _BoundModelsReasoner:
    """Resolve only this provider's locked primary/fallback model references."""

    def __init__(self, delegate: HarnessReasoner, models: dict[str, str]) -> None:
        self._delegate = delegate
        self._models = dict(models)
        # graph_builder 以 engine._reasoner._streaming / hasattr(stream_complete)
        # 决定是否走增量推理；包装器不透传时 managed Harness 会静默回退到
        # 整段 complete()，前端看不到任何 TEXT_DELTA。
        self._streaming = getattr(delegate, "_streaming", None) is True

    async def complete(
        self, *, model, prompt, messages, tools, max_output_tokens=None,
        reasoning_effort=None,
    ):
        clarification = _deterministic_product_guidance(messages, tools)
        if clarification is not None:
            return HarnessReasoningTurn(final_text=clarification)
        resolved = self._models.get(model)
        if resolved is None:
            raise ValueError(f"Unbound model profile: {model}")
        kwargs = dict(model=resolved, prompt=prompt, messages=messages, tools=tools)
        parameters = inspect.signature(self._delegate.complete).parameters
        if "max_output_tokens" in parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()
        ):
            kwargs["max_output_tokens"] = max_output_tokens
        if reasoning_effort and (
            "reasoning_effort" in parameters
            or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())
        ):
            kwargs["reasoning_effort"] = reasoning_effort
        return await self._delegate.complete(**kwargs)

    def stream_complete(
        self, *, model, prompt, messages, tools, max_output_tokens=None,
        reasoning_effort=None,
    ):
        clarification = _deterministic_product_guidance(messages, tools)
        if clarification is not None:
            return self._clarification_stream(clarification)
        resolved = self._models.get(model)
        if resolved is None:
            raise ValueError(f"Unbound model profile: {model}")
        stream = getattr(self._delegate, "stream_complete", None)
        if stream is None:
            return self._degenerate_stream(
                model=model, prompt=prompt, messages=messages, tools=tools,
                max_output_tokens=max_output_tokens,
                reasoning_effort=reasoning_effort,
            )
        kwargs = dict(model=resolved, prompt=prompt, messages=messages, tools=tools)
        parameters = inspect.signature(stream).parameters
        if "max_output_tokens" in parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()
        ):
            kwargs["max_output_tokens"] = max_output_tokens
        if reasoning_effort and (
            "reasoning_effort" in parameters
            or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())
        ):
            kwargs["reasoning_effort"] = reasoning_effort
        return stream(**kwargs)

    @staticmethod
    async def _clarification_stream(text: str):
        yield {"turn": HarnessReasoningTurn(final_text=text)}

    async def _degenerate_stream(
        self, *, model, prompt, messages, tools, max_output_tokens=None,
        reasoning_effort=None,
    ):
        turn = await self.complete(
            model=model, prompt=prompt, messages=messages, tools=tools,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
        )
        yield {"turn": turn}


class _BoundModelReasoner(_BoundModelsReasoner):
    """Backward-compatible one-model constructor used by external adapters."""

    def __init__(self, delegate: HarnessReasoner, profile_ref: str, model: str) -> None:
        super().__init__(delegate, {profile_ref: model})


_GREENFIELD_ARTIFACT_INTENT = re.compile(
    r"(?:做|开发|创建|实现|制作|搭建|生成).{0,12}(?:游戏|应用|程序|工具)|"
    r"(?:build|create|make|develop).{0,24}(?:game|app|application|program|tool)",
    re.IGNORECASE,
)
_EXPLICIT_ARTIFACT_FORM = re.compile(
    r"(?:网页|网站|单页|浏览器|html|canvas|web|website|react|vue|前端|桌面|pygame|"
    r"python|命令行|终端|cli|terminal|移动端|手机|ios|android|小程序)",
    re.IGNORECASE,
)
_DELEGATED_FORM_DECISION = re.compile(
    r"(?:你决定|你来定|自行决定|你选择|最合适|推荐的方式|随便|默认|"
    r"you decide|use your judgment|whatever works|best approach)",
    re.IGNORECASE,
)
_SKILL_INSTALL_INTENT = re.compile(
    # This gate may bypass the model entirely. Only intercept an imperative
    # action, never a question about a Skill that may already be bound.
    r"(?:^|[，,。.!！?？；;\n])\s*(?:请|帮我|给我|直接|现在|立刻|先|麻烦你)?\s*"
    r"(?:安装|装上|添加|绑定).{0,40}(?:skill|技能|插件)|"
    r"(?:^|[，,。.!！?？；;\n])\s*(?:请)?\s*(?:把|将).{0,32}"
    r"(?:skill|技能|插件).{0,20}(?:安装|添加|绑定)|"
    r"(?:^|[,.!?;\n])\s*(?:please\s+)?(?:install|add|bind).{0,40}(?:skill|plugin)",
    re.IGNORECASE,
)
_SKILL_MANAGEMENT_TOOLS = frozenset(
    {"skill_discover_github", "skill_install", "skill_bind_agent"}
)
_SKILL_DISCLOSURE_TOOLS = frozenset(
    {"skill_read_manifest", "skill_read_instructions", "skill_read_resource"}
)
def _deterministic_product_guidance(
    messages: Sequence[dict[str, Any]],
    tools: Sequence[Any],
) -> str | None:
    user_text = _latest_user_text(messages)
    tool_names = {str(getattr(tool, "name", "")) for tool in tools}
    if _SKILL_INSTALL_INTENT.search(user_text) and not (_SKILL_MANAGEMENT_TOOLS & tool_names):
        return (
            "当前 Agent 运行时没有被授权直接安装未绑定的远程 Skill；这只表示当前未绑定，"
            "不表示网上不存在。请打开 Studio **资源库 → Skill → 发现 Skill**，选择"
            "**公开 GitHub 仓库**并输入官方仓库地址。Studio 会先锁定不可变 commit，展示"
            "识别到的版本、脚本和网络访问风险；你明确确认后才能安装，并可选择绑定到 Agent"
            "后立即构建。我不会在未经确认时下载或执行远程代码。"
        )
    # The bound Skill catalog is authoritative for this run. A short question
    # about availability cannot be answered by reading arbitrary workspace
    # files; doing so caused an unbound Studio Agent to spend 85 tool calls on
    # an unrelated project before its 100-call soft limit interrupted the run.
    # Keep remote discovery and actual Skill reads on the normal tool path.
    if (
        is_skill_binding_question(user_text)
        and not (_SKILL_DISCLOSURE_TOOLS & tool_names)
    ):
        return (
            "当前 Agent 在这次运行中没有可用的已绑定 Skill。"
            "这不能说明你提到的 Skill 在网上不存在；如需使用它，请先在 Studio "
            "资源库中发现并绑定到这个 Agent。"
        )
    return _artifact_form_clarification(messages)


def _latest_user_text(messages: Sequence[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if str(message.get("role") or "").lower() != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, Sequence):
            return " ".join(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, Mapping)
            ).strip()
    return ""


def _artifact_form_clarification(messages: Sequence[dict[str, Any]]) -> str | None:
    """Return one deterministic question for a high-impact artifact ambiguity.

    Pi/Superpowers asks this before touching an empty workspace.  Managed
    Harness makes the same product invariant deterministic instead of hoping
    every provider model follows a prose instruction.  The gate is deliberately
    narrow: it only handles green-field software creation where web/desktop/CLI
    materially changes the deliverable.
    """

    user_messages: list[str] = []
    for message in messages:
        if str(message.get("role") or "").lower() != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, Sequence):
            text = " ".join(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, Mapping)
            ).strip()
        else:
            text = ""
        if text:
            user_messages.append(text)
    user_text = user_messages[-1] if user_messages else ""
    if not user_text:
        return None
    if not _GREENFIELD_ARTIFACT_INTENT.search(user_text):
        return None
    if _EXPLICIT_ARTIFACT_FORM.search(user_text) or _DELEGATED_FORM_DECISION.search(user_text):
        return None
    # A follow-up can rely on the immediately preceding conversation decision;
    # do not force users to repeat "网页版" in every turn of the same Session.
    recent_context = " ".join(user_messages[-4:-1])
    if _EXPLICIT_ARTIFACT_FORM.search(recent_context):
        return None
    noun = "游戏" if re.search(r"(?:游戏|game)", user_text, re.IGNORECASE) else "应用"
    return (
        f"开始前需要先确认一个会决定实现路线的问题：你希望这个{noun}做成哪种形式？\n\n"
        "- **A. 单页网页**（推荐）——浏览器直接打开，无需安装\n"
        "- **B. Python 桌面版**——适合本机运行，需要 Python 环境\n"
        "- **C. 其他形式**——请告诉我目标平台或技术栈\n\n"
        "回复 A、B、C，或直接说“你自行决定”即可。"
    )


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


__all__ = ["build_managed_provider_adapter"]
