"""Prompt Source Contract —— ResolvedPromptSources + PlatformPolicySource（PR A）。

把 Studio agent 的 instructions.system/task 与 request_instructions 聚合成统一来源，
编译真实 ``CompiledPrompt``（带稳定 section hash），用于 hash/trace/future projection。

PR A **不改 Runner 输入**：``payload["instructions"]`` 仍由 request 级 instructions 决定。
``compiled_prompt`` 只挂在 ``PreparedConversationTurn`` 供可观测与后续 PR B 投影。

platform_safety 暂不注入生产：``PlatformPolicySource`` 接口存在，``EnvPlatformPolicySource``
仅本地 dev override（``KSADK_PLATFORM_SAFETY_TEXT``），未设时不产 platform_safety section。
当前硬编码 ``PLATFORM_SAFETY_TEXT`` 只作测试/shadow fixture。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

from ksadk.prompts.compiler import PromptCompiler
from ksadk.prompts.models import PromptSection
from ksadk.prompts.sources import (
    agent_identity_section,
    agent_policy_section,
    platform_safety_section,
    request_instructions_section,
)

RESOLVED_PROMPT_SOURCES_VERSION = "v1"


class PlatformPolicySource(Protocol):
    """可信平台安全规则来源接口（方案 7.5 / 评测 PCM-PROMPT-001）。

    生产实现应由部署级配置提供（带版本与来源标识）。未提供可信来源时返回 ``None``，
    不产 ``platform_safety`` section——不硬编码生产安全文本。
    """

    source: str
    version: str

    def resolve(self) -> str | None: ...


@dataclass(frozen=True)
class EnvPlatformPolicySource:
    """本地开发 override：从 env ``KSADK_PLATFORM_SAFETY_TEXT`` 读平台安全文本。

    仅用于本地 dev / 测试。未设 env 时 ``resolve()`` 返回 ``None``。
    生产环境应替换为部署级 ``PlatformPolicySource`` 实现。
    """

    env_var: str = "KSADK_PLATFORM_SAFETY_TEXT"
    source: str = "env_local_dev"
    version: str = "env"

    def resolve(self) -> str | None:
        text = (os.environ.get(self.env_var, "") or "").strip()
        return text or None


def get_default_platform_policy_source() -> PlatformPolicySource | None:
    """返回默认 PlatformPolicySource（EnvPlatformPolicySource）。

    当前唯一实现是 env override；生产实现留后续 PR。返回值供
    ``compile_resolved_prompt_dict`` 决定是否产 platform_safety。
    """
    return EnvPlatformPolicySource()


@dataclass(frozen=True)
class ResolvedPromptSources:
    """一次模型调用的 Prompt 来源聚合（方案 7.6）。

    ``agent_system`` / ``agent_task`` 来自 Studio agent 配置（``Instructions.system/task``），
    ``request_instructions`` 来自 API request 级 instructions，
    ``platform_policy_source`` 为可信平台安全来源（可空）。
    """

    agent_system: str = ""
    agent_task: str = ""
    request_instructions: str = ""
    platform_policy_source: PlatformPolicySource | None = None
    version: str = RESOLVED_PROMPT_SOURCES_VERSION


def sections_from_resolved_sources(sources: ResolvedPromptSources) -> list[PromptSection]:
    """把 ResolvedPromptSources 投影成 PromptSection 列表（canonical 顺序）。

    空 content 的 section 跳过。platform_safety 仅在可信来源返回非空文本时产生。
    """
    sections: list[PromptSection] = []
    if sources.agent_system.strip():
        sections.append(agent_identity_section(sources.agent_system.strip()))
    if sources.agent_task.strip():
        sections.append(agent_policy_section(sources.agent_task.strip()))
    if sources.request_instructions.strip():
        sections.append(request_instructions_section(sources.request_instructions.strip()))
    policy_source = sources.platform_policy_source
    if policy_source is not None:
        policy_text = policy_source.resolve()
        if policy_text:
            sections.append(
                platform_safety_section(content=policy_text, source=policy_source.source)
            )
    return sections


def compile_resolved_prompt_dict(sources: ResolvedPromptSources) -> dict[str, Any] | None:
    """编译真实 CompiledPrompt 的 plain dict 投影（PR A，shadow/trace 用）。

    返回 ``None`` 表示无任何非空 section（agent_system/agent_task/request_instructions
    全空且无 platform_policy）。键名沿用 ``compile_shadow_prompt_dict`` 的 ``prompt_*``
    前缀，保证可被 ``build_shadow_context_plan_dict`` 直接 spread，且
    ``_set_prompt_cache_attributes`` 自动拿到真实 hash。
    """
    sections = sections_from_resolved_sources(sources)
    if not sections:
        return None
    compiled = PromptCompiler().compile(sections)
    policy_source = sources.platform_policy_source
    policy_active = bool(policy_source is not None and policy_source.resolve())
    return {
        "prompt_content_hash": compiled.content_hash,
        "prompt_stable_prefix_hash": compiled.stable_prefix_hash,
        "prompt_section_hashes": dict(compiled.section_hashes),
        "prompt_tokens_by_section": dict(compiled.tokens_by_section),
        "prompt_estimated_tokens": compiled.estimated_tokens,
        "prompt_section_count": len(compiled.sections),
        "prompt_compiler_version": compiled.compiler_version,
        "prompt_resolved_sources_version": sources.version,
        "prompt_platform_policy_version": (policy_source.version if policy_active else None),
        "prompt_platform_policy_source": (policy_source.source if policy_active else None),
        # PR B：真实正文。供接管注入读 ``prepared.compiled_prompt["prompt_content"]``。
        # 注意：含明文，**不得**进 shadow plan/trace（build_shadow_context_plan_dict 会剥离）。
        "prompt_content": compiled.content,
    }
