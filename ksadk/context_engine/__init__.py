"""Context Engine —— Prompt/Context/Memory 的运行时上下文协调层。

数据模型、capability 合同与 shadow 可观测基线已落地；后续 PR 新增 planner / assembler /
policies / contributors 的实际逻辑。本模块导出稳定类型与运行时合同。
"""

from ksadk.context_engine.assembler import AssembledInput, ContextAssembler, assemble
from ksadk.context_engine.capabilities import (
    DEFAULT_CONTEXT_CAPABILITIES,
    CapabilityCircuitOpen,
    ContextAccuracy,
    ContextCapabilities,
    ContextIntegrationMode,
    ContextOwner,
    DeploymentMode,
    adk_context_capabilities,
    assert_capability_not_circuit_open,
    capabilities_for_runner,
    capabilities_for_runtime_type,
    capability_hash,
    codex_context_capabilities,
    deepagents_context_capabilities,
    detect_capability_mismatch,
    is_capability_circuit_open,
    langchain_context_capabilities,
    langgraph_context_capabilities,
    mark_capability_mismatch,
    reset_capability_circuit,
)
from ksadk.context_engine.models import (
    CONTEXT_POLICY_VERSION,
    ContextBudget,
    ContextDecision,
    ContextItem,
    ContextKind,
    ContextPlan,
)
from ksadk.context_engine.planner import ContextPlanner, build_budget
from ksadk.context_engine.policies import (
    ContextBudgetPolicy,
    ContextPolicy,
    SectionBudget,
    compute_budget_tokens,
)
from ksadk.context_engine.projection import PROJECTION_VERSION, ProjectionResult
from ksadk.context_engine.tokenizer import (
    HEURISTIC_TOKENIZER_NAME,
    HeuristicTokenCounter,
    TokenCounter,
    get_default_token_counter,
)

__all__ = [
    "AssembledInput",
    "CONTEXT_POLICY_VERSION",
    "ContextAssembler",
    "ContextAccuracy",
    "ContextBudget",
    "ContextBudgetPolicy",
    "ContextCapabilities",
    "ContextDecision",
    "ContextIntegrationMode",
    "ContextItem",
    "ContextKind",
    "ContextOwner",
    "ContextPlan",
    "ContextPlanner",
    "ContextPolicy",
    "DEFAULT_CONTEXT_CAPABILITIES",
    "DeploymentMode",
    "HEURISTIC_TOKENIZER_NAME",
    "HeuristicTokenCounter",
    "PROJECTION_VERSION",
    "ProjectionResult",
    "SectionBudget",
    "TokenCounter",
    "adk_context_capabilities",
    "assemble",
    "build_budget",
    "capabilities_for_runner",
    "capabilities_for_runtime_type",
    "capability_hash",
    "codex_context_capabilities",
    "compute_budget_tokens",
    "deepagents_context_capabilities",
    "detect_capability_mismatch",
    "get_default_token_counter",
    "is_capability_circuit_open",
    "langchain_context_capabilities",
    "langgraph_context_capabilities",
    "mark_capability_mismatch",
    "reset_capability_circuit",
    "assert_capability_not_circuit_open",
    "CapabilityCircuitOpen",
    "allowed_ownership_choices",
    "validate_ownership_for_runtime",
    "resolve_ownership",
]
