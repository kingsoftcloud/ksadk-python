"""Prompt 分区数据模型（方案第 7 节）。

第一个 PR 只落地稳定数据结构，公开类型从第一批开始版本化。``compiler.py`` / ``sources.py``
（确定性编译、merge、hash、指令文件发现）留第二个 PR；本模块不接管线，不改任何 Runner
现有 instructions→new_message/SystemMessage/base_instructions 的拼装。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ksadk.context_engine.capabilities import ContextAccuracy, ContextIntegrationMode

PROMPT_COMPILER_VERSION = "v1"

PromptSectionKind = Literal[
    "platform_safety",
    "agent_identity",
    "agent_policy",
    "runtime_capabilities",
    "resource_manifest",
    "request_instructions",
]
"""统一语义分区（方案 7.1）。动态历史、记忆和工具结果不属于 PromptSection。"""

PromptTrustLevel = Literal["platform", "developer", "resource", "untrusted", "user"]
PromptStability = Literal["stable", "deployment", "volatile"]
PromptMergePolicy = Literal["replace", "append", "merge_unique", "protected"]


@dataclass(frozen=True)
class PromptSection:
    """一个 Prompt 分区单元（方案 7.2）。"""

    section_id: str
    kind: PromptSectionKind
    content: str
    source: str
    priority: int
    trust_level: PromptTrustLevel
    stability: PromptStability = "stable"
    merge_policy: PromptMergePolicy = "append"
    overridable: bool = False
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CompiledPrompt:
    """按确定规则编译后的稳定 Prompt（方案 7.2）。

    ``content`` 是 canonical content，不保证等于 Runner 最终物理输入。``stable_prefix_hash``
    覆盖稳定前缀（platform_safety / agent_identity / agent_policy），用于 Prompt Cache
    失效诊断（第二个 PR 实现）。
    """

    sections: tuple[PromptSection, ...]
    content: str
    content_hash: str
    estimated_tokens: int
    stable_prefix_hash: str
    section_hashes: dict[str, str]
    tokens_by_section: dict[str, int]
    compiler_version: str = PROMPT_COMPILER_VERSION


@dataclass(frozen=True)
class PromptProjectionResult:
    """Runner 投影 Prompt 后的可审计结果（方案 7.2）。

    第一个 PR 只保留类型信封，不接管线；由后续 PR 的 projection 逻辑填充。
    """

    projection_id: str
    runner_type: str
    integration_mode: ContextIntegrationMode
    projection_version: str
    section_hashes: tuple[str, ...]
    projected_roles: tuple[str, ...]
    accounting_accuracy: ContextAccuracy
    estimated_tokens: int | None
    warnings: tuple[str, ...] = ()
