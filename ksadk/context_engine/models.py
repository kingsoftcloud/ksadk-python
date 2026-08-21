"""Context Engine 数据模型 —— ContextItem / ContextBudget / ContextPlan / ContextDecision。

这些公开类型用于稳定表达请求级上下文的预算、选择、裁剪和投影决策，并由
``shadow_plan`` 旁路及正式规划链路共同消费。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ksadk.context_engine.capabilities import ContextAccuracy, ContextIntegrationMode

CONTEXT_POLICY_VERSION = "v1"

ContextKind = Literal[
    "compiled_prompt",
    "core_memory",
    "resource_manifest",
    "checkpoint_summary",
    "working_state",
    "recalled_memory",
    "history_round",
    "skill_content",
    "tool_result",
    "attachment_context",
    "current_input",
]
"""可进入一次模型调用的最小上下文单元类型（方案 8.1）。"""

ContextTrustLevel = Literal["platform", "developer", "resource", "user", "untrusted"]


@dataclass
class ContextItem:
    """可进入一次模型调用的最小上下文单元。

    ``group_id`` 用于原子保留/原子丢弃：一轮 user/assistant 对话、tool call 与对应
    tool result、approval request/response、Responses API function call/output item。
    所有 Memory/Knowledge/Tool/Hook/外部文件内容即使来自受信基础设施，也按 ``untrusted``
    处理，不能覆盖 PromptSection。
    """

    item_id: str
    kind: ContextKind
    content: Any
    source: str
    trust_level: ContextTrustLevel
    priority: int
    estimated_tokens: int
    required: bool = False
    droppable: bool = True
    truncatable: bool = False
    stable: bool = False
    group_id: str | None = None
    seq_start: int | None = None
    seq_end: int | None = None
    score: float | None = None
    content_hash: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextBudget:
    """一次模型调用的 token 预算合同（方案 8.2）。

    第一个 PR 只定义结构；50%/85% 双阈值计算与分区比例落盘留后续 PR
    （``soft_limit_tokens`` / ``hard_limit_tokens`` 暂由调用方按需填充）。
    """

    context_window_tokens: int
    reserved_output_tokens: int
    reserved_reasoning_tokens: int
    safety_buffer_tokens: int
    max_input_tokens: int
    soft_limit_tokens: int
    hard_limit_tokens: int
    section_limits: dict[str, int] = field(default_factory=dict)


@dataclass
class ContextDecision:
    """Planner 对单个候选项的保留/裁剪决策（方案 8.8）。"""

    item_id: str
    action: Literal["included", "summarized", "truncated", "dropped"]
    reason: str
    tokens_before: int
    tokens_after: int


@dataclass
class ContextPlan:
    """本次调用的候选项、预算与决策计划（方案 8.8）。

    ``ContextPlan`` 是平台的选择和投影计划。仅在 ``accounting_accuracy=exact`` 且 Projection
    成功时它才代表最终模型输入；native/assisted 模式下必须结合 Runner 回报生成实际使用记录。
    第一个 PR 中 ``selected`` / ``decisions`` 留空，仅 ``tokens_by_kind`` / ``planned_*``
    由 shadow 旁路填充。
    """

    plan_id: str
    policy_version: str
    tokenizer: str
    integration_mode: ContextIntegrationMode
    accounting_accuracy: ContextAccuracy
    budget: ContextBudget | None
    selected: list[ContextItem]
    decisions: list[ContextDecision]
    tokens_by_kind: dict[str, int]
    planned_input_tokens: int
    projected_input_tokens: int | None
    runtime_reported_input_tokens: int | None
    stable_prefix_hash: str
    projection_id: str | None = None
    contributor_status: dict[str, str] = field(default_factory=dict)
