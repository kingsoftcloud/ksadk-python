from __future__ import annotations

import json
from contextlib import asynccontextmanager, nullcontext
from typing import Any, Mapping, Sequence

from ksadk.sessions import SessionEvent


def _extract_deferred_tool_names(output: Any) -> list[str]:
    if not isinstance(output, Mapping):
        return []
    raw_names = output.get("deferred_tool_names")
    if not isinstance(raw_names, Sequence) or isinstance(raw_names, (str, bytes, bytearray)):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for item in raw_names:
        name = str(item or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _latest_deferred_tool_names(events: Sequence[SessionEvent]) -> list[str]:
    for event in reversed(events):
        if event.event_type != "run_status":
            continue
        metadata = event.metadata or {}
        if metadata.get("detail") != "deferred_tools_selected":
            continue
        return _extract_deferred_tool_names(metadata)
    return []


def _normalize_usage_payload(usage: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(usage, Mapping):
        return {}
    normalized: dict[str, Any] = {}
    for key in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
    ):
        value = usage.get(key)
        if value is None:
            continue
        try:
            normalized[key] = int(value)
        except (TypeError, ValueError):
            continue
    for key in ("input_token_details", "output_token_details"):
        value = usage.get(key)
        if isinstance(value, Mapping):
            normalized[key] = dict(value)
    prompt_details = usage.get("prompt_tokens_details")
    if isinstance(prompt_details, Mapping):
        normalized["prompt_tokens_details"] = dict(prompt_details)
        cached_tokens = prompt_details.get("cached_tokens")
        if cached_tokens is not None:
            try:
                normalized.setdefault("input_token_details", {})["cached"] = int(cached_tokens)
            except (TypeError, ValueError):
                pass
    completion_details = usage.get("completion_tokens_details")
    if isinstance(completion_details, Mapping):
        normalized["completion_tokens_details"] = dict(completion_details)
        reasoning_tokens = completion_details.get("reasoning_tokens")
        if reasoning_tokens is not None:
            try:
                normalized.setdefault("output_token_details", {})["reasoning"] = int(
                    reasoning_tokens
                )
            except (TypeError, ValueError):
                pass
    return normalized


def _usage_from_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(metadata, Mapping):
        return {}
    return _normalize_usage_payload(metadata.get("usage"))


def _responses_usage_payload(usage: Mapping[str, Any] | None) -> dict[str, Any] | None:
    normalized = _normalize_usage_payload(usage)
    if not normalized:
        return None
    input_tokens = normalized.get("input_tokens", normalized.get("prompt_tokens", 0))
    output_tokens = normalized.get("output_tokens", normalized.get("completion_tokens", 0))
    total_tokens = normalized.get("total_tokens", input_tokens + output_tokens)
    payload = {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": total_tokens,
    }
    input_details = normalized.get("input_token_details")
    if isinstance(input_details, Mapping):
        cached_tokens = input_details.get(
            "cached_tokens",
            input_details.get("cached", input_details.get("cache_read", 0)),
        )
        payload["input_tokens_details"]["cached_tokens"] = int(cached_tokens or 0)
    output_details = normalized.get("output_token_details")
    if isinstance(output_details, Mapping):
        reasoning_tokens = output_details.get(
            "reasoning_tokens",
            output_details.get("reasoning", 0),
        )
        payload["output_tokens_details"]["reasoning_tokens"] = int(reasoning_tokens or 0)
    return payload


def _chat_usage_payload(usage: Mapping[str, Any] | None) -> dict[str, Any] | None:
    normalized = _normalize_usage_payload(usage)
    if not normalized:
        return None
    prompt_tokens = normalized.get("prompt_tokens", normalized.get("input_tokens", 0))
    completion_tokens = normalized.get("completion_tokens", normalized.get("output_tokens", 0))
    total_tokens = normalized.get("total_tokens", prompt_tokens + completion_tokens)
    payload = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }
    prompt_details = normalized.get("prompt_tokens_details")
    if isinstance(prompt_details, Mapping):
        payload["prompt_tokens_details"] = dict(prompt_details)
    input_token_details = normalized.get("input_token_details")
    if isinstance(input_token_details, Mapping) and input_token_details:
        prompt_details = dict(payload.get("prompt_tokens_details") or {})
        cached_tokens = input_token_details.get("cached")
        if cached_tokens is not None:
            try:
                prompt_details["cached_tokens"] = int(cached_tokens)
            except (TypeError, ValueError):
                pass
        if prompt_details:
            payload["prompt_tokens_details"] = prompt_details
    completion_details = normalized.get("completion_tokens_details")
    if isinstance(completion_details, Mapping):
        payload["completion_tokens_details"] = dict(completion_details)
    output_token_details = normalized.get("output_token_details")
    if isinstance(output_token_details, Mapping) and output_token_details:
        completion_details = dict(payload.get("completion_tokens_details") or {})
        reasoning_tokens = output_token_details.get("reasoning")
        if reasoning_tokens is not None:
            try:
                completion_details["reasoning_tokens"] = int(reasoning_tokens)
            except (TypeError, ValueError):
                pass
        if completion_details:
            payload["completion_tokens_details"] = completion_details
    return payload


def _get_conversation_tracer() -> Any | None:
    try:
        from opentelemetry import trace

        return trace.get_tracer("ksadk.conversations")
    except Exception:
        return None


def _current_span_feedback_metadata() -> dict[str, str]:
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
    except Exception:
        return {}
    return _span_feedback_metadata(span)


def _span_feedback_metadata(span: Any | None) -> dict[str, str]:
    if span is None:
        return {}
    try:
        context = span.get_span_context()
    except Exception:
        return {}
    if not getattr(context, "is_valid", False):
        return {}
    return {
        "trace_id": format(context.trace_id, "032x"),
        "root_span_id": format(context.span_id, "016x"),
    }


def _get_current_span() -> Any | None:
    try:
        from opentelemetry import trace

        return trace.get_current_span()
    except Exception:
        return None


def _span_current_context(span: Any | None):
    if span is None:
        return nullcontext()
    try:
        from opentelemetry.trace import use_span

        return use_span(
            span, end_on_exit=False, record_exception=False, set_status_on_exception=False
        )
    except Exception:
        return nullcontext()


@asynccontextmanager
async def _conversation_span_scope(name: str, *, manual_end: bool = False):
    tracer = _get_conversation_tracer()
    if tracer is None:
        yield None
        return
    if manual_end:
        span = tracer.start_span(name)
        try:
            yield span
        finally:
            try:
                span.end()
            except Exception:
                pass
        return
    span = tracer.start_span(name)
    try:
        yield span
    finally:
        try:
            span.end()
        except Exception:
            pass


def _set_span_attribute(span: Any | None, key: str, value: Any) -> None:
    if span is None:
        return
    if value is None:
        return
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return
    try:
        span.set_attribute(key, value)
    except Exception:
        return


def _set_context_plan_attributes(span: Any | None, plan: Any | None) -> None:
    """把 shadow ContextPlan 的统计/ownership/精度挂到 conversation span。

    ``plan`` 为 None 或空时直接 return，对现有 span 无影响。只记录 hash/统计/精度，
    不落完整 Prompt/Memory/Tool 内容（方案 8.8 / 安全要求）。第一个 PR 只挂 plan_id/
    policy_version/tokenizer/planned_input_tokens/integration_mode/accounting_accuracy/
    tokens_by_kind(json)/stable_prefix_hash + ownership 摘要；projected/runtime_reported
    等留后续 PR。
    """
    if span is None or not plan:
        return
    if not isinstance(plan, Mapping):
        return
    _set_span_attribute(span, "context.plan_id", plan.get("plan_id"))
    _set_span_attribute(span, "context.policy_version", plan.get("policy_version"))
    _set_span_attribute(span, "context.tokenizer", plan.get("tokenizer"))
    _set_span_attribute(span, "context.deployment_mode", plan.get("deployment_mode"))
    _set_span_attribute(span, "context.runtime_type", plan.get("runtime_type"))
    _set_span_attribute(span, "context.planned_input_tokens", plan.get("planned_input_tokens"))
    # 方案 §6.3：projected/runtime_reported 贯穿 Trace（缺口 6）。projected=Adapter 实际投影给
    # Runner 的；runtime_reported=Provider/Runner 回报的实际。None 表示该口径不可得（诚实标注）。
    _set_span_attribute(span, "context.projected_input_tokens", plan.get("projected_input_tokens"))
    _set_span_attribute(
        span, "context.runtime_reported_input_tokens", plan.get("runtime_reported_input_tokens")
    )
    _set_span_attribute(span, "context.integration_mode", plan.get("integration_mode"))
    _set_span_attribute(span, "context.accounting_accuracy", plan.get("accounting_accuracy"))
    _set_span_attribute(span, "context.prompt_owner", plan.get("prompt_owner"))
    _set_span_attribute(span, "context.history_owner", plan.get("history_owner"))
    _set_span_attribute(span, "context.compaction_owner", plan.get("compaction_owner"))
    _set_span_attribute(span, "context.memory_owner", plan.get("memory_owner"))
    _set_span_attribute(span, "context.skill_owner", plan.get("skill_owner"))
    # 方案 §6.3 / 缺口 7：native compaction 不可见时的统一展示规范。compaction_owner=native 且
    # actual 不可见时，标 compaction_visibility=opaque，不把 planned 伪装成 actual。
    compaction_owner = str(plan.get("compaction_owner") or "")
    accuracy = str(plan.get("accounting_accuracy") or "")
    if compaction_owner == "native" and accuracy in ("opaque", "estimated"):
        _set_span_attribute(span, "context.compaction_visibility", "opaque")
        _set_span_attribute(
            span,
            "context.compaction_note",
            "native runtime 内部 compaction 不可见，仅记录平台 projection",
        )
    tokens_by_kind = plan.get("tokens_by_kind")
    if tokens_by_kind:
        try:
            _set_span_attribute(
                span,
                "context.tokens_by_kind",
                json.dumps(dict(tokens_by_kind), ensure_ascii=False),
            )
        except (TypeError, ValueError):
            pass
    _set_span_attribute(span, "context.stable_prefix_hash", plan.get("stable_prefix_hash"))


def _set_conversation_input_attributes(span: Any | None, input_text: str | None) -> None:
    text = " ".join(str(input_text or "").split())
    if not text:
        return
    for key in (
        "langfuse.trace.input",
        "langfuse.observation.input",
        "input.value",
        "gen_ai.prompt",
    ):
        _set_span_attribute(span, key, text)


def _set_conversation_output_attributes(span: Any | None, output_text: str | None) -> None:
    text = " ".join(str(output_text or "").split())
    if not text:
        return
    for key in (
        "langfuse.trace.output",
        "langfuse.observation.output",
        "output.value",
        "gen_ai.completion",
    ):
        _set_span_attribute(span, key, text)


def _set_conversation_usage_attributes(
    span: Any | None,
    usage: Mapping[str, Any] | None,
) -> None:
    normalized = _normalize_usage_payload(usage)
    if not normalized:
        return

    def _usage_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    input_tokens = _usage_int(normalized.get("input_tokens"))
    output_tokens = _usage_int(normalized.get("output_tokens"))
    total_tokens = _usage_int(normalized.get("total_tokens") or (input_tokens + output_tokens))
    input_details = normalized.get("input_token_details")
    output_details = normalized.get("output_token_details")
    cache_read_tokens = 0
    reasoning_tokens = 0
    if isinstance(input_details, Mapping):
        cache_read_tokens = _usage_int(
            input_details.get("cache_read")
            or input_details.get("cached")
            or input_details.get("cached_tokens")
        )
    if isinstance(output_details, Mapping):
        reasoning_tokens = _usage_int(
            output_details.get("reasoning") or output_details.get("reasoning_tokens")
        )

    attributes = {
        "gen_ai.usage.input_tokens": input_tokens,
        "gen_ai.usage.output_tokens": output_tokens,
        "gen_ai.usage.total_tokens": total_tokens,
        "llm.usage.prompt_tokens": input_tokens,
        "llm.usage.completion_tokens": output_tokens,
        "llm.usage.total_tokens": total_tokens,
    }
    if cache_read_tokens:
        attributes["gen_ai.usage.cache_read.input_tokens"] = cache_read_tokens
        attributes["llm.usage.cache_read.input_tokens"] = cache_read_tokens
    if reasoning_tokens:
        attributes["gen_ai.usage.reasoning.output_tokens"] = reasoning_tokens
        attributes["llm.usage.reasoning_tokens"] = reasoning_tokens

    for key, value in attributes.items():
        if value:
            _set_span_attribute(span, key, value)


def _set_conversation_span_attributes(
    span: Any,
    *,
    agent_id: str,
    user_id: str,
    session_id: str,
    invocation_id: str,
    runner_name: str,
    model: str | None,
    response_id: str | None = None,
) -> None:
    if span is None:
        return
    try:
        span.set_attribute("ksadk.agent_id", agent_id)
        span.set_attribute("ksadk.user_id", user_id)
        span.set_attribute("ksadk.session_id", session_id)
        span.set_attribute("ksadk.invocation_id", invocation_id)
        span.set_attribute("ksadk.runner", runner_name)
        span.set_attribute("langfuse.trace.name", runner_name)
        span.set_attribute("langfuse.session.id", session_id)
        span.set_attribute("session.id", session_id)
        span.set_attribute("langfuse.user.id", user_id)
        span.set_attribute("user.id", user_id)
        if model:
            span.set_attribute("llm.model_name", model)
            span.set_attribute("gen_ai.request.model", model)
        if response_id:
            span.set_attribute("ksadk.response_id", response_id)
    except Exception:
        return


def _set_prompt_cache_attributes(
    span: Any | None,
    *,
    session_id: str | None,
    plan: Any | None,
    usage: Mapping[str, Any] | None,
) -> None:
    """PR2：记录 shadow CompiledPrompt hash + Provider prompt cache 信号 + 失效诊断到 span。

    ``plan`` 为 ``PreparedConversationTurn.shadow_context_plan``（plain dict）。``usage`` 为
    Runtime 返回的 normalized usage。诊断用进程内 best-effort registry 记录上一稳定前缀，
    pod 重启后清空，精度如实标注。只记录 hash/usage/break reason，不落完整 Prompt（安全要求）。
    plan/usage 缺失时 no-op，对现有 span 无影响。
    """
    if span is None or not isinstance(plan, Mapping):
        return
    from ksadk.context_engine.cache_observability import (
        diagnose_cache_break,
        get_default_cache_break_registry,
    )

    stable_prefix_hash = str(
        plan.get("prompt_stable_prefix_hash") or plan.get("stable_prefix_hash") or ""
    )
    accounting_accuracy = str(plan.get("accounting_accuracy") or "opaque")
    _set_span_attribute(span, "prompt.content_hash", plan.get("prompt_content_hash"))
    _set_span_attribute(span, "prompt.stable_prefix_hash", stable_prefix_hash or None)
    section_hashes = plan.get("prompt_section_hashes")
    if isinstance(section_hashes, Mapping) and section_hashes:
        _set_span_attribute(span, "prompt.section_count", len(section_hashes))

    registry = get_default_cache_break_registry()
    previous_hash = registry.previous(session_id) if session_id else None
    diagnosis = diagnose_cache_break(
        stable_prefix_hash=stable_prefix_hash,
        previous_stable_prefix_hash=previous_hash,
        usage=usage,
        accounting_accuracy=accounting_accuracy,  # type: ignore[arg-type]
    )
    _set_span_attribute(span, "prompt.cache.read_input_tokens", diagnosis.cache_read_tokens or None)
    _set_span_attribute(
        span, "prompt.cache.creation_input_tokens", diagnosis.cache_creation_tokens or None
    )
    _set_span_attribute(
        span, "prompt.cache.expected_invalidation", diagnosis.expected_invalidation or None
    )
    _set_span_attribute(span, "prompt.cache.unexpected_break", diagnosis.unexpected_break or None)
    _set_span_attribute(span, "prompt.cache.break_reason", diagnosis.break_reason or None)
    _set_span_attribute(span, "prompt.cache.status", diagnosis.status)
    # 记录本轮稳定前缀供下一轮诊断（best-effort，进程内）。
    if session_id and stable_prefix_hash:
        registry.record(session_id, stable_prefix_hash)


def _set_prompt_source_attributes(span: Any | None, compiled_prompt: Any | None) -> None:
    """PR A：记录真实 CompiledPrompt 的 source hash/version/section count 到 span。

    ``compiled_prompt`` 为 ``PreparedConversationTurn.compiled_prompt``（plain dict，agent_system/
    agent_task 非空时由 ResolvedPromptSources 编译）。None 时 no-op。只记 hash/version/count，
    不记 Prompt 正文（安全要求）。
    """
    if span is None or not isinstance(compiled_prompt, Mapping):
        return
    section_hashes = compiled_prompt.get("prompt_section_hashes")
    if isinstance(section_hashes, Mapping) and section_hashes:
        _set_span_attribute(
            span, "prompt.source.agent_system_hash", section_hashes.get("agent_identity")
        )
        _set_span_attribute(
            span, "prompt.source.agent_task_hash", section_hashes.get("agent_policy")
        )
        _set_span_attribute(span, "prompt.source.section_count", len(section_hashes))
    _set_span_attribute(
        span,
        "prompt.source.platform_policy_version",
        compiled_prompt.get("prompt_platform_policy_version"),
    )
    _set_span_attribute(
        span,
        "prompt.source.resolved_sources_version",
        compiled_prompt.get("prompt_resolved_sources_version"),
    )
