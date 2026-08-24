"""Prompt Projection —— CompiledPrompt → 目标 Runner 的可审计投影（方案 §7.1 / §7.2）。

Projection 把 ``CompiledPrompt`` 的 canonical section 按目标 Runner 的合法承载形式映射
（Codex ``base_instructions``、ADK ``instruction``、LangGraph ``system_message``），并输出
可审计的 ``PromptProjectionResult``。Projection 可以改变物理承载形式，但必须保留 section
source、hash、信任级别和覆盖决策；不得改变安全优先级与信任边界（方案 §7.1）。

PR B 之前 Projection 仅用于可观测/审计，不替换 Runner 实际发送的 instructions。
"""

from __future__ import annotations

import uuid
from typing import Any

from ksadk.context_engine.capabilities import ContextAccuracy, ContextIntegrationMode
from ksadk.prompts.models import CompiledPrompt, PromptProjectionResult

PROJECTION_VERSION = "v1"

# 各 integration_mode 的合法投影承载形式（方案 §6.2 / §7.1）。
_PROJECTION_ROLES: dict[ContextIntegrationMode, tuple[str, ...]] = {
    "ksadk_hosted": ("system_message", "instruction"),
    "framework_assisted": ("system_message", "state", "instruction", "session", "memory_service"),
    "native_runtime": ("base_instructions", "thread"),
}


def project_compiled_prompt(
    compiled: CompiledPrompt,
    *,
    runner_type: str,
    integration_mode: ContextIntegrationMode,
    accounting_accuracy: ContextAccuracy,
    warnings: tuple[str, ...] = (),
) -> PromptProjectionResult:
    """投影 CompiledPrompt 到目标 Runner，输出可审计结果（方案 §7.2）。

    只读 ``compiled``，不改 Runner 输入。``projected_roles`` 反映该 integration_mode 的合法承载
    形式集合；``section_hashes`` 直接取自编译结果，保证投影前后 hash 一致、可校验顺序漂移。
    """
    roles = _PROJECTION_ROLES.get(integration_mode, ())
    if not roles:
        warnings = (*warnings, f"unknown_integration_mode:{integration_mode}")
    # 校验安全优先级未被投影改变：platform_safety 必须存在且 trust_level=platform（若编译含它）。
    safety_sections = [s for s in compiled.sections if s.kind == "platform_safety"]
    for s in safety_sections:
        if s.trust_level != "platform":
            warnings = (*warnings, f"platform_safety_wrong_trust:{s.trust_level}")
    return PromptProjectionResult(
        projection_id=f"pj_{uuid.uuid4().hex[:16]}",
        runner_type=runner_type,
        integration_mode=integration_mode,
        projection_version=PROJECTION_VERSION,
        section_hashes=tuple(
            compiled.section_hashes.get(s.section_id, "") for s in compiled.sections
        ),
        projected_roles=roles,
        accounting_accuracy=accounting_accuracy,
        estimated_tokens=compiled.estimated_tokens,
        warnings=warnings,
    )


def project_to_runner_payload(
    compiled: CompiledPrompt,
    *,
    integration_mode: ContextIntegrationMode,
) -> dict[str, Any]:
    """把 CompiledPrompt.content 投影成目标 Runner 的 payload 字段（方案 §7.1）。

    ksadk_hosted/framework_assisted(LangGraph 系) → ``{"system_message": content}``；
    framework_assisted(ADK) → ``{"instruction": content}``；
    native_runtime(Codex) → ``{"base_instructions": content}``。调用方据 capability 选字段。
    """
    if integration_mode == "native_runtime":
        return {"base_instructions": compiled.content}
    if integration_mode == "framework_assisted":
        # ADK 用 instruction；LangGraph 用 system_message。两者都返回，调用方按 capability 选。
        return {"instruction": compiled.content, "system_message": compiled.content}
    # ksadk_hosted
    return {"system_message": compiled.content, "instruction": compiled.content}


__all__ = [
    "PROJECTION_VERSION",
    "project_compiled_prompt",
    "project_to_runner_payload",
]
