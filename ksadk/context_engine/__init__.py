"""Context Engine —— Prompt/Context/Memory 的运行时上下文协调层。

第一个 PR（shadow 可观测基线）只导出稳定数据模型与 capability 合同：
``ContextCapabilities`` / ``ContextItem`` / ``ContextBudget`` / ``ContextPlan`` /
``ContextDecision`` / ``TokenCounter`` / ``HeuristicTokenCounter`` / ``ProjectionResult``。
planner / assembler / contributors / policies / cache_observability 的实际逻辑留后续 PR。
"""

from ksadk.context_engine.capabilities import (
    DEFAULT_CONTEXT_CAPABILITIES,
    ContextAccuracy,
    ContextCapabilities,
    ContextIntegrationMode,
    ContextOwner,
    adk_context_capabilities,
    capabilities_for_runner,
    capabilities_for_runtime_type,
    capability_hash,
    codex_context_capabilities,
    deepagents_context_capabilities,
    langchain_context_capabilities,
    langgraph_context_capabilities,
)
from ksadk.context_engine.models import (
    CONTEXT_POLICY_VERSION,
    ContextBudget,
    ContextDecision,
    ContextItem,
    ContextKind,
    ContextPlan,
)
from ksadk.context_engine.projection import PROJECTION_VERSION, ProjectionResult
from ksadk.context_engine.tokenizer import (
    HEURISTIC_TOKENIZER_NAME,
    HeuristicTokenCounter,
    TokenCounter,
    get_default_token_counter,
)

__all__ = [
    "CONTEXT_POLICY_VERSION",
    "ContextAccuracy",
    "ContextBudget",
    "ContextCapabilities",
    "ContextDecision",
    "ContextIntegrationMode",
    "ContextItem",
    "ContextKind",
    "ContextOwner",
    "ContextPlan",
    "DEFAULT_CONTEXT_CAPABILITIES",
    "HEURISTIC_TOKENIZER_NAME",
    "HeuristicTokenCounter",
    "PROJECTION_VERSION",
    "ProjectionResult",
    "TokenCounter",
    "adk_context_capabilities",
    "capabilities_for_runner",
    "capabilities_for_runtime_type",
    "capability_hash",
    "codex_context_capabilities",
    "deepagents_context_capabilities",
    "get_default_token_counter",
    "langchain_context_capabilities",
    "langgraph_context_capabilities",
]
