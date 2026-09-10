"""Harness Execution Strategy Registry（plan §14）——多 Agent 是可选能力。

一期策略：
- ``single-agent``：默认（对应 engine.base.single_agent_plan）；
- ``plan-execute``：规划与执行分离；
- ``plan-execute-review``：增加独立审查；
- ``custom-imported``：兼容导入路径占位。

模板可推荐策略，但不把技术拓扑写成模板核心描述（§14.2）——策略由创建者
或平台策略选择。
"""

from __future__ import annotations

from dataclasses import dataclass

from ksadk.harness.engine.base import ExecutionPlan, single_agent_plan
from ksadk.harness.spec import HarnessSpec


class StrategyRegistryError(ValueError):
    """未知/冲突策略注册。"""


@dataclass(frozen=True)
class StrategyDescriptor:
    strategy_kind: str
    description: str
    #: 单 Agent 模板无关性守卫：默认策略不得携带多 Agent 专属字符串。
    is_default: bool = False


def plan_execute_plan() -> ExecutionPlan:
    """plan-execute：规划与执行分离（§14.1）。"""
    return ExecutionPlan(
        strategy_kind="plan-execute",
        nodes=("prepare_context", "plan", "execute", "reason", "tool_calls", "final"),
        edges=(
            ("prepare_context", "plan"),
            ("plan", "execute"),
            ("execute", "reason"),
            ("reason", "tool_calls"),
            ("tool_calls", "reason"),
            ("reason", "final"),
        ),
    )


def plan_execute_review_plan() -> ExecutionPlan:
    """plan-execute-review：增加独立审查节点（§14.1）。"""
    return ExecutionPlan(
        strategy_kind="plan-execute-review",
        nodes=(
            "prepare_context",
            "plan",
            "execute",
            "reason",
            "tool_calls",
            "review",
            "final",
        ),
        edges=(
            ("prepare_context", "plan"),
            ("plan", "execute"),
            ("execute", "reason"),
            ("reason", "tool_calls"),
            ("tool_calls", "reason"),
            ("reason", "review"),
            ("review", "final"),
        ),
    )


#: 多 Agent 专属节点名——single-agent 拓扑不得包含（§17 Phase 5 验收）。
_MULTI_AGENT_NODES = frozenset({"plan", "execute", "review", "manager", "reviewer"})


class ExecutionStrategyRegistry:
    """策略注册表：compile(spec) → ExecutionPlan（纯数据，引擎无关）。"""

    def __init__(self) -> None:
        self._strategies: dict[str, StrategyDescriptor] = {
            "single-agent": StrategyDescriptor(
                strategy_kind="single-agent",
                description="默认单 Agent 循环（reason 自环 + final）",
                is_default=True,
            ),
            "plan-execute": StrategyDescriptor(
                strategy_kind="plan-execute",
                description="规划与执行分离",
            ),
            "plan-execute-review": StrategyDescriptor(
                strategy_kind="plan-execute-review",
                description="规划-执行-独立审查",
            ),
            "custom-imported": StrategyDescriptor(
                strategy_kind="custom-imported",
                description="导入已有项目的兼容路径",
            ),
        }
        self._compilers = {
            "single-agent": single_agent_plan,
            "plan-execute": plan_execute_plan,
            "plan-execute-review": plan_execute_review_plan,
        }

    def register(self, descriptor: StrategyDescriptor, compiler=None) -> None:
        if descriptor.strategy_kind in self._strategies:
            raise StrategyRegistryError(f"strategy 重复注册: {descriptor.strategy_kind}")
        self._strategies[descriptor.strategy_kind] = descriptor
        if compiler is not None:
            self._compilers[descriptor.strategy_kind] = compiler

    def list_strategies(self) -> list[StrategyDescriptor]:
        return list(self._strategies.values())

    def default(self) -> str:
        for descriptor in self._strategies.values():
            if descriptor.is_default:
                return descriptor.strategy_kind
        return "single-agent"

    def compile(self, spec: HarnessSpec, *, strategy: str | None = None) -> ExecutionPlan:
        """按策略编译拓扑；未指定时使用注册表默认。"""
        kind = strategy or self.default()
        if kind == "custom-imported":
            raise StrategyRegistryError("custom-imported 策略无通用拓扑：由导入的 Graph 自行提供")
        compiler = self._compilers.get(kind)
        if compiler is None:
            raise StrategyRegistryError(f"unknown strategy: {kind}")
        return compiler()

    # ------------------------------------------------------------- 守卫

    @staticmethod
    def assert_single_agent_purity(plan: ExecutionPlan) -> None:
        """验收守卫：single-agent 拓扑不包含多 Agent 专属节点。"""
        polluted = _MULTI_AGENT_NODES & set(plan.nodes)
        if polluted:
            raise StrategyRegistryError(f"single-agent 拓扑混入多 Agent 节点: {sorted(polluted)}")


__all__ = [
    "ExecutionStrategyRegistry",
    "StrategyDescriptor",
    "StrategyRegistryError",
    "plan_execute_plan",
    "plan_execute_review_plan",
]
