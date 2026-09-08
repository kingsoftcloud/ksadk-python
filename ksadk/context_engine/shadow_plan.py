"""shadow ContextPlan 构造器（第一个 PR 私有，非公开稳定类型）。

在现有调用链旁路构造一个 ``ContextPlan`` 的 dict 投影，用启发式 tokenizer 按 kind 累加
``tokens_by_kind``，标注 ``accounting_accuracy`` / ``integration_mode``，只写入
``PreparedConversationTurn.shadow_context_plan`` 和 trace span，**不进任何决策路径**。

返回 plain ``dict`` 而非 ``ContextPlan`` 对象，避免 ``runtime_payloads`` 顶层 import
``context_engine`` 形成循环依赖。
"""

from __future__ import annotations

import uuid
from typing import Any, Mapping

from ksadk.context_engine.capabilities import (
    DEFAULT_CONTEXT_CAPABILITIES,
    capabilities_for_runner,
    capabilities_for_runtime_type,
    capability_hash,
)
from ksadk.context_engine.models import CONTEXT_POLICY_VERSION
from ksadk.context_engine.tokenizer import HEURISTIC_TOKENIZER_NAME, get_default_token_counter
from ksadk.prompts.compiler import PromptCompiler
from ksadk.prompts.sources import sections_from_instructions

# shadow plan 默认初始化的 kind 字典，保证 Trace 字段稳定。
_SHADOW_KINDS = (
    "compiled_prompt",
    "history_round",
    "current_input",
    "recalled_memory",
    "attachment_context",
)


def _empty_tokens_by_kind() -> dict[str, int]:
    return {kind: 0 for kind in _SHADOW_KINDS}


def _history_tokens(history: Any, counter: Any) -> int:
    if not history:
        return 0
    total = 0
    for turn in history:
        if isinstance(turn, Mapping):
            for key in ("role", "content", "text"):
                value = turn.get(key)
                if isinstance(value, str):
                    total += counter.count_text(value)
                elif isinstance(value, list):
                    for part in value:
                        if isinstance(part, Mapping):
                            text = part.get("text") or part.get("content")
                            if isinstance(text, str):
                                total += counter.count_text(text)
                        elif isinstance(part, str):
                            total += counter.count_text(part)
        elif isinstance(turn, str):
            total += counter.count_text(turn)
    return total


def _ambient_text(section: Any) -> str:
    """从 memory_context / kb_context 等 ambient 字段里取 formatted_text。"""
    if isinstance(section, Mapping):
        text = section.get("formatted_text")
        if isinstance(text, str) and text.strip():
            return text
    return ""


def _resolve_caps(*, runner: Any | None, runtime_type: str | None) -> tuple[Any, str]:
    """解析 capability：优先 runner（adapter/runner 实例），否则 runtime_type，再退 DEFAULT。

    返回 ``(caps, runtime_type)``。``runtime_type`` 用于 Plan 记录；优先取 runner 的
    ``runtime_type`` 属性（RuntimeAdapter/CodexRuntimeAdapter），其次 detection_result.type.value
    （framework BaseRunner），最后传入值。
    """
    if runner is not None:
        caps = capabilities_for_runner(runner)
        rt = (
            str(getattr(runner, "runtime_type", "") or "")
            or _runner_detection_type_value(runner)
            or str(runtime_type or "")
        )
        return caps, rt.strip().lower()
    if runtime_type:
        caps = capabilities_for_runtime_type(runtime_type)
        return caps, str(runtime_type).strip().lower()
    return DEFAULT_CONTEXT_CAPABILITIES(), ""


def _runner_detection_type_value(runner: Any) -> str:
    """读 runner.detection_result.type.value（framework BaseRunner 的类型标识）。"""
    detection_result = getattr(runner, "detection_result", None)
    if detection_result is None:
        return ""
    detection_type = getattr(detection_result, "type", None)
    if detection_type is None:
        return ""
    value = getattr(detection_type, "value", detection_type)
    return str(value or "").strip().lower()


def compile_shadow_prompt_dict(instructions: str | None) -> dict[str, Any]:
    """编译 instructions → shadow CompiledPrompt 的 plain dict 投影（PR2）。

    只把 ``request_instructions``（volatile）纳入编译，不引入未发送的 platform_safety，
    保证 shadow hash 如实反映当前发送的 instructions。``stable_prefix_hash`` 在仅有
    volatile section 时为空（cache-break 诊断据此如实标 ``no_cache_info``）。
    供 ``build_shadow_context_plan_dict`` 与 trace 使用，不进决策路径、不替换 Runner 发送内容。
    """
    sections = sections_from_instructions(instructions)
    if not sections:
        return {
            "prompt_content_hash": "",
            "prompt_stable_prefix_hash": "",
            "prompt_section_hashes": {},
            "prompt_tokens_by_section": {},
            "prompt_estimated_tokens": 0,
            "prompt_section_count": 0,
        }
    compiled = PromptCompiler().compile(sections)
    return {
        "prompt_content_hash": compiled.content_hash,
        "prompt_stable_prefix_hash": compiled.stable_prefix_hash,
        "prompt_section_hashes": dict(compiled.section_hashes),
        "prompt_tokens_by_section": dict(compiled.tokens_by_section),
        "prompt_estimated_tokens": compiled.estimated_tokens,
        "prompt_section_count": len(compiled.sections),
    }


def build_shadow_context_plan_dict(
    *,
    instructions: str = "",
    history: Any = None,
    user_input: str = "",
    request_metadata: Mapping[str, Any] | None = None,
    runner: Any | None = None,
    runtime_type: str | None = None,
    model_metadata: Mapping[str, Any] | None = None,
    prompt_shadow: Mapping[str, Any] | None = None,
    prompt_integration_mode: str = "",
    deployment_mode: str = "local",
) -> dict[str, Any]:
    """构造 shadow ContextPlan 的 plain dict 投影。

    所有参数来自 ``PreparedConversationTurn`` 已有字段，不读取任何额外状态。capability
    解析顺序：``runner``（adapter/runner 实例，含 RuntimeAdapter）→ ``runtime_type``
    （canonical conversation execution 路径，build_run_input 阶段尚未拿到 adapter）→ DEFAULT。
    canonical 路径因此不再落成默认 opaque（方案 6.1 / ADR-009）。

    ``deployment_mode``（方案 §4.3 / §6.1）：与 Context ownership 正交，独立写入 plan/trace。
    默认 ``local``，云端由控制面传入 ``ksadk_managed_cloud``/``external_managed``。不得据它
    推断 ``integration_mode``。

    ``prompt_shadow``：调用方可传入预编译的真实 CompiledPrompt dict（PR A，含 agent_system/
    agent_task 的稳定 section）。为 None 时回退到 ``compile_shadow_prompt_dict(instructions)``
    （仅 request_instructions volatile）。传入真实 dict 时，``prompt_*`` 键全部来自真实编译，
    ``stable_prefix_hash`` 非空（stable section 进了编译）。

    ``prompt_integration_mode``（PR B）：per-Build 接管标记。仅当为 ``ksadk_hosted`` 且
    capability ``prompt_owner==ksadk`` 且 ``runtime_type==langgraph`` 时，``integration_mode``
    显示字段覆盖为 ``ksadk_hosted``（表示本 turn 由 ksadk 编译并接管 instructions）。
    ``capability_hash`` 仍用原 caps（稳定，不随 per-request 接管状态抖动）。
    """
    counter = get_default_token_counter()
    tokens_by_kind = _empty_tokens_by_kind()

    tokens_by_kind["compiled_prompt"] = counter.count_text(instructions or "")
    tokens_by_kind["history_round"] = _history_tokens(history, counter)
    tokens_by_kind["current_input"] = counter.count_text(user_input or "")

    metadata = request_metadata or {}
    memory_text = _ambient_text(metadata.get("memory_context"))
    if memory_text:
        tokens_by_kind["recalled_memory"] = counter.count_text(memory_text)
    kb_text = _ambient_text(metadata.get("kb_context"))
    if kb_text:
        tokens_by_kind["attachment_context"] = counter.count_text(kb_text)

    caps, resolved_runtime_type = _resolve_caps(runner=runner, runtime_type=runtime_type)
    planned = sum(tokens_by_kind.values())
    prompt_shadow_dict = (
        prompt_shadow if prompt_shadow is not None else compile_shadow_prompt_dict(instructions)
    )
    # PR B：prompt_content 是真实正文，含明文，不得进 shadow plan/trace。这里剥离，
    # 只保留 hash/统计键（与 _set_prompt_source_attributes 只读 hash 一致）。
    shadow_prompt_keys = {
        key: value for key, value in prompt_shadow_dict.items() if key != "prompt_content"
    }
    # PR B：接管态显示。capability_hash 不变（不随 per-request 抖动）。
    effective_mode = caps.integration_mode
    if (
        prompt_integration_mode == "ksadk_hosted"
        and caps.prompt_owner == "ksadk"
        and resolved_runtime_type == "langgraph"
    ):
        effective_mode = "ksadk_hosted"

    return {
        "plan_id": f"ctxplan_{uuid.uuid4().hex[:16]}",
        "policy_version": CONTEXT_POLICY_VERSION,
        "tokenizer": counter.name or HEURISTIC_TOKENIZER_NAME,
        "integration_mode": effective_mode,
        "accounting_accuracy": caps.token_accounting,
        "tokens_by_kind": tokens_by_kind,
        "planned_input_tokens": planned,
        "projected_input_tokens": None,
        "runtime_reported_input_tokens": None,
        "stable_prefix_hash": shadow_prompt_keys["prompt_stable_prefix_hash"],
        "projection_id": None,
        "contributor_status": {},
        # capability 摘要，便于 Trace 单独解释 ownership（不替代 conformance 测试）。
        "prompt_owner": caps.prompt_owner,
        "history_owner": caps.history_owner,
        "compaction_owner": caps.compaction_owner,
        "memory_owner": caps.memory_owner,
        "skill_owner": caps.skill_owner,
        # 接线修正：记录 runtime_type + capability_hash，使 canonical 路径的 Plan 可解释、
        # 可比对 adapter 声明一致性（capability mismatch 检测留后续 PR）。
        "runtime_type": resolved_runtime_type,
        "deployment_mode": str(deployment_mode or "local"),
        "capability_hash": capability_hash(caps),
        # PR2/PR A：shadow CompiledPrompt hash/section 统计，供 cache-break 诊断与可观测。
        # shadow_prompt_keys 来自真实编译（PR A 含 agent_system/agent_task）
        # 或 instructions-only 回退，
        # 已剥离 prompt_content（明文不进 shadow plan/trace）。
        **shadow_prompt_keys,
    }


def minimal_shadow_context_plan_dict(
    *,
    runner: Any | None = None,
    runtime_type: str | None = None,
    deployment_mode: str = "local",
) -> dict[str, Any]:
    """resume / 空输入场景的最小 shadow plan：只带 ownership 与精度，不累加 token。"""
    caps, resolved_runtime_type = _resolve_caps(runner=runner, runtime_type=runtime_type)
    prompt_shadow = compile_shadow_prompt_dict(None)
    return {
        "plan_id": f"ctxplan_{uuid.uuid4().hex[:16]}",
        "policy_version": CONTEXT_POLICY_VERSION,
        "tokenizer": HEURISTIC_TOKENIZER_NAME,
        "integration_mode": caps.integration_mode,
        "accounting_accuracy": caps.token_accounting,
        "tokens_by_kind": _empty_tokens_by_kind(),
        "planned_input_tokens": 0,
        "projected_input_tokens": None,
        "runtime_reported_input_tokens": None,
        "stable_prefix_hash": "",
        "projection_id": None,
        "contributor_status": {},
        "prompt_owner": caps.prompt_owner,
        "history_owner": caps.history_owner,
        "compaction_owner": caps.compaction_owner,
        "memory_owner": caps.memory_owner,
        "skill_owner": caps.skill_owner,
        "runtime_type": resolved_runtime_type,
        "deployment_mode": str(deployment_mode or "local"),
        "capability_hash": capability_hash(caps),
        **prompt_shadow,
    }
