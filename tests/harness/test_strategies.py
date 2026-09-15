"""Phase 5 Execution Strategy 测试（plan §17 验收项）。"""

from __future__ import annotations

import pytest

from ksadk.harness.engine.base import single_agent_plan
from ksadk.harness.spec import HarnessSpec, ModelBinding, PromptSpec
from ksadk.harness.strategies import (
    ExecutionStrategyRegistry,
    StrategyRegistryError,
    plan_execute_plan,
    plan_execute_review_plan,
)


def _spec() -> HarnessSpec:
    return HarnessSpec(
        agent_revision_ref="agent-revision://proj-1@2",
        model=ModelBinding(profile_ref="model-profile://kimi-k3@1.0.0"),
        prompt=PromptSpec(instructions="你是财务分析助手。"),
    )


class TestStrategies:
    def test_default_is_single_agent(self):
        registry = ExecutionStrategyRegistry()
        assert registry.default() == "single-agent"
        plan = registry.compile(_spec())
        assert plan.strategy_kind == "single-agent"
        assert set(plan.nodes) == {"prepare_context", "reason", "tool_calls", "final"}

    def test_same_spec_can_choose_different_strategies(self):
        """同一模板/Spec 可选择不同策略（§17 Phase 5 验收）。"""
        registry = ExecutionStrategyRegistry()
        spec = _spec()
        single = registry.compile(spec, strategy="single-agent")
        pe = registry.compile(spec, strategy="plan-execute")
        per = registry.compile(spec, strategy="plan-execute-review")
        assert single.strategy_kind == "single-agent"
        assert "plan" in pe.nodes and "review" not in pe.nodes
        assert "review" in per.nodes

    def test_single_agent_plan_contains_no_multi_agent_nodes(self):
        """单 Agent 拓扑不包含无关多 Agent 字符串（§17 验收）。"""
        ExecutionStrategyRegistry.assert_single_agent_purity(single_agent_plan())
        ExecutionStrategyRegistry.assert_single_agent_purity(
            ExecutionStrategyRegistry().compile(_spec())
        )

    def test_multi_agent_plan_fails_purity_guard(self):
        with pytest.raises(StrategyRegistryError, match="多 Agent 节点"):
            ExecutionStrategyRegistry.assert_single_agent_purity(plan_execute_plan())

    def test_unknown_strategy_rejected(self):
        with pytest.raises(StrategyRegistryError, match="unknown strategy"):
            ExecutionStrategyRegistry().compile(_spec(), strategy="swarm")

    def test_custom_imported_requires_graph(self):
        """custom-imported 无通用拓扑：导入的 Graph 自行提供。"""
        with pytest.raises(StrategyRegistryError, match="custom-imported"):
            ExecutionStrategyRegistry().compile(_spec(), strategy="custom-imported")

    def test_register_custom_strategy(self):
        registry = ExecutionStrategyRegistry()
        from ksadk.harness.engine.base import ExecutionPlan

        def compiler() -> ExecutionPlan:
            return ExecutionPlan(strategy_kind="finance-orchestration", nodes=("a", "b"))

        registry.register(
            type(
                "D",
                (),
                {
                    "strategy_kind": "finance-orchestration",
                    "description": "财务编排",
                    "is_default": False,
                },
            )(),
            compiler,
        )
        plan = registry.compile(_spec(), strategy="finance-orchestration")
        assert plan.nodes == ("a", "b")

    def test_duplicate_registration_rejected(self):
        registry = ExecutionStrategyRegistry()

        class _Dup:
            strategy_kind = "single-agent"
            description = "dup"
            is_default = False

        with pytest.raises(StrategyRegistryError, match="重复注册"):
            registry.register(_Dup())

    def test_strategy_list_has_descriptions(self):
        strategies = {s.strategy_kind for s in ExecutionStrategyRegistry().list_strategies()}
        assert {"single-agent", "plan-execute", "plan-execute-review"} <= strategies


class TestPlanShapes:
    def test_plan_execute_review_topology(self):
        plan = plan_execute_review_plan()
        edges = set(plan.edges)
        assert ("reason", "review") in edges
        assert ("review", "final") in edges

    def test_plans_are_frozen_data(self):
        plan = plan_execute_plan()
        with pytest.raises(Exception):
            plan.nodes = ("x",)  # type: ignore[misc]
