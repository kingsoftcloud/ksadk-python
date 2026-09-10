"""引擎无关的 Agent Loop 纯逻辑（plan §16）。

本包下模块不 import LangGraph / StateGraph / Command / interrupt —— 它们只
依赖 HarnessSpec / HarnessState / RuntimeEvent 等领域模型，以及 reasoner /
working_context 等已引擎无关的抽象。图节点（``engine/langgraph.py``）是调用
这些纯函数的薄封装：把返回的待发事件 append、把 route 写回图 State、把
interrupt 映射到 LangGraph ``interrupt()``。

这样引擎无关逻辑可在无 LangGraph 的环境单测，同时服务 Conformance（§15）。
"""

from ksadk.harness.loop.reason import (
    ModelFailoverExhausted,
    ReasoningLimitError,
    ReasonInput,
    ReasonOutput,
    reason_turn_async,
)
from ksadk.harness.loop.tools import (
    ApprovalResolver,
    ContextualToolExecutor,
    ParallelSafeDecider,
    ToolCallInput,
    ToolCallOutput,
    ToolExecutionContext,
    ToolExecutor,
    execute_tool_calls,
)

__all__ = [
    "ApprovalResolver",
    "ContextualToolExecutor",
    "ModelFailoverExhausted",
    "ParallelSafeDecider",
    "ReasonInput",
    "ReasonOutput",
    "ReasoningLimitError",
    "ToolCallInput",
    "ToolCallOutput",
    "ToolExecutionContext",
    "ToolExecutor",
    "execute_tool_calls",
    "reason_turn_async",
]
