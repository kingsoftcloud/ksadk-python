"""Prompt 分区与编译数据模型（方案第 7 节）。

第一个 PR 只导出稳定类型：``PromptSection`` / ``CompiledPrompt`` / ``PromptProjectionResult``。
``PromptCompiler`` 的确定性编译、merge、hash、指令文件发现与预算留第二个 PR。
"""

from ksadk.prompts.compiler import (
    InconsistentSectionError,
    ProtectedSectionOverrideError,
    PromptCompiler,
    compile_prompt,
)
from ksadk.prompts.models import (
    PROMPT_COMPILER_VERSION,
    CompiledPrompt,
    PromptMergePolicy,
    PromptProjectionResult,
    PromptSection,
    PromptSectionKind,
    PromptStability,
    PromptTrustLevel,
)
from ksadk.prompts.sources import (
    PLATFORM_SAFETY_TEXT,
    agent_identity_section,
    agent_policy_section,
    discover_instruction_files,
    platform_safety_section,
    request_instructions_section,
    resource_manifest_section,
    sections_from_instructions,
)

__all__ = [
    "PLATFORM_SAFETY_TEXT",
    "PROMPT_COMPILER_VERSION",
    "CompiledPrompt",
    "InconsistentSectionError",
    "PromptCompiler",
    "PromptMergePolicy",
    "PromptProjectionResult",
    "PromptSection",
    "PromptSectionKind",
    "PromptStability",
    "PromptTrustLevel",
    "ProtectedSectionOverrideError",
    "agent_identity_section",
    "agent_policy_section",
    "compile_prompt",
    "discover_instruction_files",
    "platform_safety_section",
    "request_instructions_section",
    "resource_manifest_section",
    "sections_from_instructions",
]
