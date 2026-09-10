"""Revision -> HarnessSpec Compiler（plan §6.1 / §12.2 / Phase 1）。

把 AgentRevisionSpec 编译为引擎无关的 HarnessSpec：引用保持版本固定、
orchestration pattern 映射到 ExecutionStrategy、policy/deployment 约束下沉。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ksadk.harness.spec import (
    ApprovalPolicy,
    CapabilityBinding,
    CapabilityBindings,
    ExecutionStrategyKind,
    ExecutionStrategySpec,
    HarnessSpec,
    MemoryPolicy,
    ModelBinding,
    ModelFailureCategory,
    ModelProviderPolicy,
    PromptSpec,
    SandboxPolicy,
    SubAgentBinding,
)


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


class _RevisionInputModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class RevisionRoleInput(_RevisionInputModel):
    name: str = ""
    objective: str = ""
    instructions_ref: str | None = None


class OrchestrationInput(_RevisionInputModel):
    pattern: str
    config_ref: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class RevisionModelProviderPolicyInput(_RevisionInputModel):
    max_attempts_per_model: int = Field(default=2, ge=1, le=5)
    total_attempt_budget: int = Field(default=6, ge=1, le=20)
    initial_backoff_ms: int = Field(default=200, ge=0, le=30_000)
    max_backoff_ms: int = Field(default=2_000, ge=0, le=60_000)
    retryable_categories: tuple[ModelFailureCategory, ...] = tuple(ModelFailureCategory)
    failover_categories: tuple[ModelFailureCategory, ...] = tuple(ModelFailureCategory)


class RevisionModelInput(_RevisionInputModel):
    profile_ref: str
    fallback_profile_refs: tuple[str, ...] = Field(default=(), max_length=4)
    provider_policy: RevisionModelProviderPolicyInput = Field(
        default_factory=RevisionModelProviderPolicyInput
    )


class MCPBindingInput(_RevisionInputModel):
    binding_ref: str


class SkillBindingInput(_RevisionInputModel):
    skill_ref: str
    content_hash: str | None = None


class KnowledgeBindingInput(_RevisionInputModel):
    knowledge_ref: str


class RevisionCapabilitiesInput(_RevisionInputModel):
    mcp_bindings: list[MCPBindingInput] = Field(default_factory=list)
    skill_bindings: list[SkillBindingInput] = Field(default_factory=list)
    knowledge_bindings: list[KnowledgeBindingInput] = Field(default_factory=list)


class RevisionMemoryInput(_RevisionInputModel):
    policy_ref: str | None = None


class SubAgentInput(_RevisionInputModel):
    name: str
    instructions: str
    description: str = ""
    tools: tuple[str, ...] = ()
    timeout_seconds: float = 120.0
    max_turns: int = 4
    max_total_tokens: int | None = None
    max_artifacts: int | None = None
    max_tool_calls: int | None = None
    output_schema: dict[str, Any] | None = None
    depends_on: tuple[str, ...] = ()
    failure_policy: str = "propagate"
    max_retries: int = 0
    inherit_skills: bool = True
    inherit_mcp: bool = False


class HarnessRevisionInput(_RevisionInputModel):
    """Minimal control-plane-neutral input accepted by the Harness compiler."""

    role: RevisionRoleInput
    orchestration: OrchestrationInput | None = None
    sub_agents: tuple[SubAgentInput, ...] = ()
    model: RevisionModelInput | None = None
    capabilities: RevisionCapabilitiesInput = Field(default_factory=RevisionCapabilitiesInput)
    memory: RevisionMemoryInput = Field(default_factory=RevisionMemoryInput)
    evaluation: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)
    deployment: dict[str, Any] = Field(default_factory=dict)


#: orchestration.pattern -> ExecutionStrategyKind（plan §14.2：模板推荐策略，
#: 但策略是 Execution 层概念，不写入模板核心描述）。
_STRATEGY_BY_PATTERN: dict[str, ExecutionStrategyKind] = {
    "single-agent": ExecutionStrategyKind.SINGLE_AGENT,
    "plan-execute": ExecutionStrategyKind.PLAN_EXECUTE,
    "plan-execute-review": ExecutionStrategyKind.PLAN_EXECUTE_REVIEW,
    # manager-executor-reviewer 是多 Agent 形态的技术编排（§14.2）；
    # 单进程 Harness 引擎以 plan-execute-review 图近似承载（plan≈manager、
    # reason 循环≈executor、review≈reviewer），正式多 Agent 由平台承担。
    "manager-executor-reviewer": ExecutionStrategyKind.PLAN_EXECUTE_REVIEW,
}


class HarnessCompileError(ValueError):
    """Revision 无法编译为合法 HarnessSpec。"""


def compile_revision_to_spec(
    spec: HarnessRevisionInput,
    *,
    revision_ref: str,
    harness_version: str = "0.1.0",
) -> HarnessSpec:
    """编译入口。``revision_ref`` 必须是固定版本的 agent-revision 引用。"""

    if spec.model is None or not spec.model.profile_ref:
        raise HarnessCompileError(
            "Revision 缺少 model.profileRef——Managed Agent 必须绑定固定版本的 Model Profile"
        )
    if not (spec.role.instructions_ref or _inline_instructions(spec)):
        raise HarnessCompileError("Revision 缺少指令（role.instructionsRef 或内联 objective）")

    strategy = _compile_strategy(spec.orchestration)
    model_binding = ModelBinding(
        profile_ref=spec.model.profile_ref,
        fallback_profile_refs=spec.model.fallback_profile_refs,
        provider_policy=ModelProviderPolicy(**spec.model.provider_policy.model_dump()),
    )

    prompt = (
        PromptSpec(
            instructions_ref=spec.role.instructions_ref,
        )
        if spec.role.instructions_ref
        else PromptSpec(
            instructions=_inline_instructions(spec) or "",
        )
    )

    capabilities = CapabilityBindings(
        mcp_bindings=tuple(
            CapabilityBinding(capability_ref=b.binding_ref, required=True)
            for b in spec.capabilities.mcp_bindings
        ),
        skill_bindings=tuple(
            CapabilityBinding(
                capability_ref=b.skill_ref,
                required=True,
                load_policy="on_demand",  # Skill 渐进披露（plan §10.4）
            )
            for b in spec.capabilities.skill_bindings
        ),
    )

    memory = MemoryPolicy(
        enabled=bool(spec.memory.policy_ref),
        scopes=("session", "agent") if spec.memory.policy_ref else ("session",),
        core_block_refs=(spec.memory.policy_ref,) if spec.memory.policy_ref else (),
    )

    approval = ApprovalPolicy(
        mode="policy",
        timeout_seconds=None,
    )

    sandbox = SandboxPolicy(read_only=True)

    sub_agents = tuple(
        SubAgentBinding(
            name=sub.name,
            instructions=sub.instructions,
            description=sub.description,
            tools=tuple(sub.tools),
            timeout_seconds=sub.timeout_seconds,
            max_turns=sub.max_turns,
            max_total_tokens=sub.max_total_tokens,
            max_artifacts=sub.max_artifacts,
            max_tool_calls=sub.max_tool_calls,
            output_schema=sub.output_schema,
            depends_on=sub.depends_on,
            failure_policy=sub.failure_policy,
            max_retries=sub.max_retries,
            inherit_skills=sub.inherit_skills,
            inherit_mcp=sub.inherit_mcp,
        )
        for sub in spec.sub_agents
    )

    return HarnessSpec(
        agent_revision_ref=revision_ref,
        model=model_binding,
        prompt=prompt,
        memory_policy=memory,
        capabilities=capabilities,
        sub_agents=sub_agents,
        execution_strategy=strategy,
        approval_policy=approval,
        sandbox_policy=sandbox,
        harness_version=harness_version,
    )


def _compile_strategy(orchestration: OrchestrationInput | None) -> ExecutionStrategySpec:
    if orchestration is None:
        return ExecutionStrategySpec(kind=ExecutionStrategyKind.SINGLE_AGENT)
    kind = _STRATEGY_BY_PATTERN.get(orchestration.pattern)
    if kind is None:
        # 未知 pattern 不静默回退（与 HarnessConfig 的严格校验一致）。
        raise HarnessCompileError(
            f"orchestration.pattern {orchestration.pattern!r} 不在支持列表 "
            f"{sorted(_STRATEGY_BY_PATTERN)}"
        )
    config = dict(orchestration.config)
    if orchestration.config_ref:
        config["configRef"] = orchestration.config_ref
    if "run_control" in config:
        from ksadk.harness.run_control import run_control_spec_from_config

        try:
            control = run_control_spec_from_config(config)
        except ValueError as exc:
            raise HarnessCompileError(f"orchestration.config.run_control 无效: {exc}") from exc
        assert control is not None
        config["run_control"] = control.model_dump(mode="json")
    return ExecutionStrategySpec(kind=kind, config=config)


def _inline_instructions(spec: HarnessRevisionInput) -> str | None:
    """无 instructionsRef 时的内联指令来源：role.objective（Phase 1 最小实现）。"""
    objective = (spec.role.objective or "").strip()
    return objective or None


def compile_revision_payload(payload: dict[str, Any], *, revision_ref: str) -> HarnessSpec:
    """从 revision spec 的 camelCase JSON（API/存储形态）编译。"""
    try:
        revision_spec = HarnessRevisionInput.model_validate(payload)
    except Exception as exc:  # noqa: BLE001
        raise HarnessCompileError(f"revision spec 校验失败: {exc}") from exc
    return compile_revision_to_spec(revision_spec, revision_ref=revision_ref)


__all__ = [
    "HarnessCompileError",
    "HarnessRevisionInput",
    "compile_revision_payload",
    "compile_revision_to_spec",
]
