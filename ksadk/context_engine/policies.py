"""ContextPolicy / PromptPolicy / MemoryPolicy 归一化与旧 env 映射（方案 §13）。

把散落的环境变量与 AgentSpec 字段归一化成结构化策略，本地与云端共用同一 Runtime 代码消费
（方案 §12）。优先级：Agent Revision/Build 锁定配置 > Environment 安全收紧 > 本地显式 API >
结构化配置文件 > 环境变量 > SDK 默认值。

公开类型从第一批开始版本化（``CONTEXT_POLICY_VERSION``，与 ``context_engine.models`` 一致）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping

CONTEXT_POLICY_VERSION = "v1"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class PromptPolicy:
    """Prompt 策略（方案 §7 / §13）。

    ``auto_discovery`` 控制指令文件自动发现（默认关，``KSADK_PROMPT_AUTO_DISCOVERY``）。
    ``rule_file_max_tokens`` / ``rule_files_max_tokens`` 为单文件/总预算。
    """

    auto_discovery: bool = False
    rule_file_max_tokens: int = 4000
    rule_files_max_tokens: int = 12000
    cache_observability: bool = True
    compiler_version: str = "v1"

    @classmethod
    def from_env(cls) -> "PromptPolicy":
        return cls(
            auto_discovery=os.environ.get("KSADK_PROMPT_AUTO_DISCOVERY", "").strip().lower()
            in ("1", "true", "yes", "on"),
            rule_file_max_tokens=_env_int("KSADK_CONTEXT_RULE_FILE_MAX_TOKENS", 4000),
            rule_files_max_tokens=_env_int("KSADK_CONTEXT_RULE_FILES_MAX_TOKENS", 12000),
            cache_observability=os.environ.get("KSADK_CONTEXT_CACHE_BREAK_OBSERVABILITY", "true")
            .strip()
            .lower()
            not in ("0", "false", "off"),
        )


@dataclass(frozen=True)
class SectionBudget:
    """单分区预算比例与绝对上限（方案 §8.3）。"""

    percent: float
    max_tokens: int


@dataclass(frozen=True)
class ContextBudgetPolicy:
    """请求级预算与分区比例（方案 §8.2 / §8.3 / 附录 A）。

    50%/85% 是初始默认值，可由模型 metadata 或 Deployment policy 覆盖（方案 §8.2）。
    分区比例不是配额预占：某分区未用预算可回流，但 required item 的预算必须先锁定。
    """

    soft_limit_percent: float = 50.0
    hard_limit_percent: float = 85.0
    safety_buffer_tokens: int = 8000
    reserved_output_tokens: int = 0  # 0 表示 auto（按模型 metadata 推导）
    reserved_reasoning_tokens: int = 0
    sections: dict[str, SectionBudget] = field(
        default_factory=lambda: {
            "prompt": SectionBudget(15, 24000),
            "resource_manifest": SectionBudget(5, 8000),
            "core_memory": SectionBudget(5, 8000),
            "recalled_memory": SectionBudget(10, 16000),
            "checkpoint_summary": SectionBudget(10, 16000),
            "working_state": SectionBudget(5, 8000),
            "recent_history": SectionBudget(35, 64000),
            "tool_and_attachment": SectionBudget(10, 16000),
        }
    )

    @classmethod
    def from_env(cls) -> "ContextBudgetPolicy":
        sections = dict(cls().sections)
        return cls(
            soft_limit_percent=_env_float("KSADK_CONTEXT_SOFT_LIMIT_PERCENT", 50.0),
            hard_limit_percent=_env_float("KSADK_CONTEXT_HARD_LIMIT_PERCENT", 85.0),
            safety_buffer_tokens=_env_int("KSADK_CONTEXT_SAFETY_BUFFER_TOKENS", 8000),
            sections=sections,
        )


@dataclass(frozen=True)
class CompactionPolicy:
    """Compaction 策略（方案 §9 / §13）。"""

    keep_tail_groups: int = 8
    emergency_keep_tail_groups: int = 3
    semantic_enabled: bool = True
    semantic_timeout_ms: int = 45000
    max_retry_after_prompt_too_long: int = 1
    flush_memory_before_compaction: bool = True
    working_state_enabled: bool = True
    working_state_max_tokens: int = 8000
    working_state_update_min_token_growth: int = 5000
    working_state_extraction_timeout_ms: int = 15000

    @classmethod
    def from_env(cls) -> "CompactionPolicy":
        return cls(
            keep_tail_groups=_env_int("KSADK_CONTEXT_KEEP_TAIL_GROUPS", 8),
            emergency_keep_tail_groups=_env_int("KSADK_CONTEXT_EMERGENCY_KEEP_TAIL_GROUPS", 3),
            semantic_enabled=os.environ.get("KSADK_CONTEXT_SEMANTIC_ENABLED", "true")
            .strip()
            .lower()
            not in ("0", "false", "off"),
            semantic_timeout_ms=_env_int("KSADK_CONTEXT_SEMANTIC_TIMEOUT_MS", 45000),
            max_retry_after_prompt_too_long=_env_int("KSADK_CONTEXT_MAX_RETRY_AFTER_PTL", 1),
            flush_memory_before_compaction=os.environ.get(
                "KSADK_MEMORY_FLUSH_BEFORE_COMPACTION", "true"
            )
            .strip()
            .lower()
            not in ("0", "false", "off"),
            working_state_enabled=os.environ.get("KSADK_CONTEXT_WORKING_STATE_ENABLED", "true")
            .strip()
            .lower()
            not in ("0", "false", "off"),
            working_state_max_tokens=_env_int("KSADK_CONTEXT_WORKING_STATE_MAX_TOKENS", 8000),
            working_state_update_min_token_growth=_env_int(
                "KSADK_CONTEXT_WORKING_STATE_MIN_TOKEN_GROWTH", 5000
            ),
            working_state_extraction_timeout_ms=_env_int(
                "KSADK_CONTEXT_WORKING_STATE_EXTRACTION_TIMEOUT_MS", 15000
            ),
        )


@dataclass(frozen=True)
class ToolResultPolicy:
    """Tool Result 单项预算与内容替换（方案 §8.6 / 附录 A）。"""

    default_max_tokens: int = 8000
    replacement_strategy: str = "artifact_summary"
    preserve_error_tail: bool = True

    @classmethod
    def from_env(cls) -> "ToolResultPolicy":
        return cls(
            default_max_tokens=_env_int("KSADK_CONTEXT_TOOL_RESULT_MAX_TOKENS", 16000),
        )


@dataclass(frozen=True)
class ContributorPolicy:
    """ContextContributor 默认约束（方案 §8.7 / 附录 A）。"""

    default_timeout_ms: int = 3000
    default_failure_mode: str = "skip"  # skip / warn / fail
    allow_external_platform_trust: bool = False

    @classmethod
    def from_env(cls) -> "ContributorPolicy":
        return cls(
            default_timeout_ms=_env_int("KSADK_CONTEXT_CONTRIBUTOR_TIMEOUT_MS", 3000),
            default_failure_mode=os.environ.get("KSADK_CONTEXT_CONTRIBUTOR_FAILURE_MODE", "skip")
            .strip()
            .lower()
            or "skip",
            allow_external_platform_trust=os.environ.get(
                "KSADK_CONTEXT_CONTRIBUTOR_ALLOW_PLATFORM_TRUST", "false"
            )
            .strip()
            .lower()
            in ("1", "true", "yes"),
        )


@dataclass(frozen=True)
class MemoryPolicyConfig:
    """Memory 策略（方案 §13，区别于写入 ``MemoryPolicy``）。"""

    enabled: bool = True
    provider: str = "local_sqlite"
    core_max_tokens: int = 4000
    recall_top_k: int = 8
    recall_max_tokens: int = 4000
    min_score: float = 0.45
    write_mode: str = "propose"  # explicit / propose / off

    @classmethod
    def from_env(cls) -> "MemoryPolicyConfig":
        # 旧变量映射（方案 §13）：KSADK_LTM_BACKEND → provider
        provider = (
            (
                os.environ.get("KSADK_MEMORY_PROVIDER")
                or os.environ.get("KSADK_LTM_BACKEND")
                or "local_sqlite"
            )
            .strip()
            .lower()
        )
        return cls(
            enabled=os.environ.get("KSADK_MEMORY_ENABLED", "true").strip().lower()
            not in ("0", "false", "off"),
            provider=provider,
            core_max_tokens=_env_int("KSADK_MEMORY_CORE_MAX_TOKENS", 4000),
            recall_top_k=_env_int("KSADK_MEMORY_RECALL_TOP_K", 8),
            recall_max_tokens=_env_int("KSADK_MEMORY_RECALL_MAX_TOKENS", 4000),
            min_score=_env_float("KSADK_MEMORY_MIN_SCORE", 0.45),
            write_mode=os.environ.get("KSADK_MEMORY_WRITE_MODE", "propose").strip().lower()
            or "propose",
        )


@dataclass(frozen=True)
class ContextPolicy:
    """归一化后的完整 Context 策略（方案 §13 / 附录 A）。

    本地与云端共用同一 Runtime 代码消费此结构，不直接读取隐式环境变量决定核心算法
    （方案 §12）。``version`` 与 ``context_engine.CONTEXT_POLICY_VERSION`` 对齐。
    """

    version: str = CONTEXT_POLICY_VERSION
    budget: ContextBudgetPolicy = field(default_factory=ContextBudgetPolicy)
    compaction: CompactionPolicy = field(default_factory=CompactionPolicy)
    tool_results: ToolResultPolicy = field(default_factory=ToolResultPolicy)
    contributors: ContributorPolicy = field(default_factory=ContributorPolicy)
    memory: MemoryPolicyConfig = field(default_factory=MemoryPolicyConfig)
    prompt: PromptPolicy = field(default_factory=PromptPolicy)

    @classmethod
    def from_env(cls) -> "ContextPolicy":
        return cls(
            budget=ContextBudgetPolicy.from_env(),
            compaction=CompactionPolicy.from_env(),
            tool_results=ToolResultPolicy.from_env(),
            contributors=ContributorPolicy.from_env(),
            memory=MemoryPolicyConfig.from_env(),
            prompt=PromptPolicy.from_env(),
        )

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any] | None) -> "ContextPolicy":
        """从 Studio ``ContextSpec``（兼容扩展）解析。缺字段走默认（方案 §13）。"""
        if not isinstance(spec, Mapping):
            return cls.from_env()
        # 当前 ContextSpec 字段较少，只读已知键；其余走 env/默认。
        base = cls.from_env()
        return base


def compute_budget_tokens(
    policy: ContextBudgetPolicy,
    *,
    context_window_tokens: int,
    reserved_output_tokens: int,
    reserved_reasoning_tokens: int,
) -> dict[str, int]:
    """计算 max_input / soft_limit / hard_limit（方案 §8.2）。

    ``reserved_output``/``reserved_reasoning`` 为 0 时按传入值；safety_buffer 从 policy。
    """
    # 默认 8K safety buffer 面向大窗口模型。对 4K/8K 小窗口若直接扣除会把
    # max_input 压成 0，因此将安全余量限制在窗口的 10%（至少 256 tokens）。
    effective_safety_buffer = min(
        policy.safety_buffer_tokens,
        max(256, int(context_window_tokens * 0.10)),
    )
    max_input = max(
        0,
        min(
            context_window_tokens,
            context_window_tokens
            - reserved_output_tokens
            - reserved_reasoning_tokens
            - effective_safety_buffer,
        ),
    )
    soft = int(max_input * policy.soft_limit_percent / 100.0)
    hard = int(max_input * policy.hard_limit_percent / 100.0)
    return {
        "max_input_tokens": max_input,
        "soft_limit_tokens": soft,
        "hard_limit_tokens": hard,
        "safety_buffer_tokens": effective_safety_buffer,
    }


__all__ = [
    "CompactionPolicy",
    "ContextBudgetPolicy",
    "ContextPolicy",
    "ContributorPolicy",
    "MemoryPolicyConfig",
    "PromptPolicy",
    "SectionBudget",
    "ToolResultPolicy",
    "compute_budget_tokens",
]
