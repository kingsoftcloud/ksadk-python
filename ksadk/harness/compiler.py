"""Revision -> HarnessSpec Compiler（plan §6.1 / §12.2 / Phase 1）。

把 AgentRevisionSpec 编译为引擎无关的 HarnessSpec：引用保持版本固定、
orchestration pattern 映射到 ExecutionStrategy、policy/deployment 约束下沉。
"""

from __future__ import annotations

from typing import Any

from ksadk.harness.spec import (
    ApprovalPolicy,
    CapabilityBinding,
    CapabilityBindings,
    ExecutionStrategyKind,
    ExecutionStrategySpec,
    HarnessSpec,
    MemoryPolicy,
    ModelBinding,
    PromptSpec,
    SandboxPolicy,
)
from ksadk.studio.domain.agent_revision import AgentRevisionSpec, OrchestrationSpec

#: orchestration.pattern -> ExecutionStrategyKind（plan §14.2：模板推荐策略，
#: 但策略是 Execution 层概念，不写入模板核心描述）。
_STRATEGY_BY_PATTERN: dict[str, ExecutionStrategyKind] = {
    "single-agent": ExecutionStrategyKind.SINGLE_AGENT,
    "plan-execute": ExecutionStrategyKind.PLAN_EXECUTE,
    "plan-execute-review": ExecutionStrategyKind.PLAN_EXECUTE_REVIEW,
}


class HarnessCompileError(ValueError):
    """Revision 无法编译为合法 HarnessSpec。"""


def compile_revision_to_spec(
    spec: AgentRevisionSpec,
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
    model_binding = ModelBinding(profile_ref=spec.model.profile_ref)

    prompt = PromptSpec(
        instructions_ref=spec.role.instructions_ref,
    ) if spec.role.instructions_ref else PromptSpec(
        instructions=_inline_instructions(spec) or "",
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

    return HarnessSpec(
        agent_revision_ref=revision_ref,
        model=model_binding,
        prompt=prompt,
        memory_policy=memory,
        capabilities=capabilities,
        execution_strategy=strategy,
        approval_policy=approval,
        sandbox_policy=sandbox,
        harness_version=harness_version,
    )


def _compile_strategy(orchestration: OrchestrationSpec | None) -> ExecutionStrategySpec:
    if orchestration is None:
        return ExecutionStrategySpec(kind=ExecutionStrategyKind.SINGLE_AGENT)
    kind = _STRATEGY_BY_PATTERN.get(orchestration.pattern)
    if kind is None:
        # 未知 pattern 不静默回退（与 HarnessConfig 的严格校验一致）。
        raise HarnessCompileError(
            f"orchestration.pattern {orchestration.pattern!r} 不在支持列表 "
            f"{sorted(_STRATEGY_BY_PATTERN)}"
        )
    return ExecutionStrategySpec(
        kind=kind,
        config={"configRef": orchestration.config_ref} if orchestration.config_ref else {},
    )


def _inline_instructions(spec: AgentRevisionSpec) -> str | None:
    """无 instructionsRef 时的内联指令来源：role.objective（Phase 1 最小实现）。"""
    objective = (spec.role.objective or "").strip()
    return objective or None


def compile_revision_payload(payload: dict[str, Any], *, revision_ref: str) -> HarnessSpec:
    """从 revision spec 的 camelCase JSON（API/存储形态）编译。"""
    try:
        revision_spec = AgentRevisionSpec.model_validate(payload)
    except Exception as exc:  # noqa: BLE001
        raise HarnessCompileError(f"revision spec 校验失败: {exc}") from exc
    return compile_revision_to_spec(revision_spec, revision_ref=revision_ref)


__all__ = [
    "HarnessCompileError",
    "compile_revision_payload",
    "compile_revision_to_spec",
]
