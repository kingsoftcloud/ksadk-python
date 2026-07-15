from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, Mapping, Optional, Sequence

import httpx
from fastapi import HTTPException

from ksadk.conversations.attachments import compact_attachment_result_for_session
from ksadk.conversations.compaction_pipeline import build_working_set_metadata, run_pipeline
from ksadk.conversations.context import (
    TRANSCRIPT_EVENT_TYPES,
    build_history_from_events,
    build_request_history,
    canonical_event_type,
    compacted_until_seq_id,
    extract_event_text,
    group_events_by_api_round,
)
from ksadk.conversations.model_context import (
    estimate_text_tokens,
    get_auto_compact_threshold_percentage,
    get_auto_compact_threshold_tokens,
    normalize_model_metadata,
)
from ksadk.conversations.model_options import normalize_model_options
from ksadk.conversations.normalize import (
    canonical_input_content_from_parts,
    compact_attachment_for_session,
    normalize_kop_messages,
)
from ksadk.conversations.reasoning_markup import strip_reasoning_markup
from ksadk.conversations.run_kinds import (
    RUN_MODE_FOREGROUND,
    RUN_MODE_UNKNOWN,
    RUN_TRIGGER_APPROVAL_RESUME,
    RUN_TRIGGER_CHECKPOINT_RESUME,
    RUN_TRIGGER_NEW_RUN,
    RUN_TRIGGER_UNKNOWN,
    trigger_from_resume_input,
    validate_run_mode,
    validate_run_trigger,
)
from ksadk.conversations.semantic_summary import (
    extract_pinned_state,
    find_pinned_group_indexes,
    summarize_compaction,
)
from ksadk.conversations.session_title import (
    DEFAULT_SESSION_TITLE_TIMEOUT_MS,
    HEURISTIC_SESSION_TITLE_SOURCE,
    build_fallback_title,
    build_heuristic_title,
    build_session_title_messages,
    is_low_quality_title,
    resolve_session_title_client,
    resolve_session_title_model,
)
from ksadk.knowledge_base.service import KnowledgeBaseService
from ksadk.memory.service import LongTermMemoryService
from ksadk.model_policy import fallback_model_for_exception, model_policy_options_for_model
from ksadk.runtime_context import (
    PlatformInvocationContext,
    platform_invocation_scope,
    tool_execution_scope,
)
from ksadk.sessions import Session, SessionEvent, resolve_session_service
from ksadk.tools.gateway import (
    approval_interrupt_info_from_result,
    build_tool_receipt_idempotency_key,
)

AUTOCOMPACT_KEEP_TAIL_GROUPS = 4
PTL_RETRY_KEEP_TAIL_GROUPS = 2
PROMPT_TOO_LONG_MARKERS = (
    "prompt-too-long",
    "prompt too long",
    "maximum context length",
    "context length",
    "context_length_exceeded",
    "413",
)
SESSION_SUMMARY_MAX_CHARS = 160
ATTACHMENT_CONTEXT_STATE_KEY = "__ksadk_attachment_context__"

logger = logging.getLogger(__name__)
_MODEL_CATALOG_CACHE_TTL_SECONDS = 60.0
_MODEL_CATALOG_CACHE: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}


@dataclass
class RuntimeGovernanceState:
    max_turns: int = 0
    max_tool_calls: int = 0
    max_consecutive_tool_failures: int = 0
    max_consecutive_approval_denials: int = 0
    max_consecutive_compact_failures: int = 0
    turn_count: int = 0
    tool_calls: int = 0
    consecutive_tool_failures: int = 0
    consecutive_approval_denials: int = 0
    consecutive_compact_failures: int = 0


class RuntimeCircuitOpen(RuntimeError):
    def __init__(self, reason: str, message: str, *, metadata: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.reason = reason
        self.metadata = dict(metadata or {})


def _int_env(name: str, default: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return int(default)
    try:
        return max(0, int(raw))
    except ValueError:
        return int(default)


def _runtime_governance_from_env() -> RuntimeGovernanceState:
    return RuntimeGovernanceState(
        max_turns=_int_env("KSADK_MAX_TURNS", 0),
        max_tool_calls=_int_env("KSADK_MAX_TOOL_CALLS", 0),
        max_consecutive_tool_failures=_int_env("KSADK_MAX_CONSECUTIVE_TOOL_FAILURES", 0),
        max_consecutive_approval_denials=_int_env("KSADK_MAX_CONSECUTIVE_APPROVAL_DENIALS", 0),
        max_consecutive_compact_failures=_int_env("KSADK_MAX_CONSECUTIVE_COMPACT_FAILURES", 0),
    )


def _governance_error(
    reason: str, message: str, state: RuntimeGovernanceState
) -> RuntimeCircuitOpen:
    return RuntimeCircuitOpen(
        reason,
        message,
        metadata={
            "reason": reason,
            "max_turns": state.max_turns,
            "max_tool_calls": state.max_tool_calls,
            "max_consecutive_tool_failures": state.max_consecutive_tool_failures,
            "max_consecutive_approval_denials": state.max_consecutive_approval_denials,
            "max_consecutive_compact_failures": state.max_consecutive_compact_failures,
            "turn_count": state.turn_count,
            "tool_calls": state.tool_calls,
            "consecutive_tool_failures": state.consecutive_tool_failures,
            "consecutive_approval_denials": state.consecutive_approval_denials,
            "consecutive_compact_failures": state.consecutive_compact_failures,
        },
    )


def _governance_record_turn_start(state: RuntimeGovernanceState) -> None:
    state.turn_count += 1
    if state.max_turns and state.turn_count > state.max_turns:
        raise _governance_error("max_turns_exceeded", "runtime max_turns limit exceeded", state)


def _governance_record_tool_call(state: RuntimeGovernanceState) -> None:
    state.tool_calls += 1
    if state.max_tool_calls and state.tool_calls > state.max_tool_calls:
        raise _governance_error(
            "max_tool_calls_exceeded", "runtime max_tool_calls limit exceeded", state
        )


def _governance_record_tool_result(state: RuntimeGovernanceState, output: Any) -> None:
    failed = isinstance(output, Mapping) and output.get("ok") is False
    state.consecutive_tool_failures = state.consecutive_tool_failures + 1 if failed else 0
    if (
        state.max_consecutive_tool_failures
        and state.consecutive_tool_failures >= state.max_consecutive_tool_failures
    ):
        raise _governance_error(
            "consecutive_tool_failures", "runtime consecutive tool failure limit exceeded", state
        )


def _governance_record_approval_response(
    state: RuntimeGovernanceState, approval: Mapping[str, Any]
) -> None:
    approved = bool(approval.get("approved") or approval.get("approve"))
    state.consecutive_approval_denials = 0 if approved else state.consecutive_approval_denials + 1
    if (
        state.max_consecutive_approval_denials
        and state.consecutive_approval_denials >= state.max_consecutive_approval_denials
    ):
        raise _governance_error(
            "consecutive_approval_denials",
            "runtime consecutive approval denial limit exceeded",
            state,
        )


def _governance_record_compact_failure(state: RuntimeGovernanceState) -> None:
    state.consecutive_compact_failures += 1
    if (
        state.max_consecutive_compact_failures
        and state.consecutive_compact_failures >= state.max_consecutive_compact_failures
    ):
        raise _governance_error(
            "consecutive_compact_failures",
            "runtime consecutive compact failure limit exceeded",
            state,
        )


def _governance_record_compact_success(state: RuntimeGovernanceState) -> None:
    state.consecutive_compact_failures = 0


async def _compact_conversation_history_with_governance(
    governance: RuntimeGovernanceState | None,
    **kwargs: Any,
) -> SessionEvent | None:
    try:
        checkpoint = await compact_conversation_history(**kwargs)
    except Exception:
        if governance is not None:
            _governance_record_compact_failure(governance)
        raise
    if checkpoint is not None and governance is not None:
        _governance_record_compact_success(governance)
    return checkpoint


def _tool_observability_metadata(
    tool_name: str, output: Any, *, duration_ms: int | None = None
) -> dict[str, Any]:
    output_text = (
        json.dumps(output, ensure_ascii=False, sort_keys=True)
        if isinstance(output, Mapping)
        else str(output)
    )
    metadata: dict[str, Any] = {
        "tool_name": tool_name,
        "duration_ms": int(duration_ms or 0),
        "output_chars": len(output_text),
        "truncated": False,
        "persisted": False,
        "exit_code": None,
        "error_type": "",
    }
    if isinstance(output, Mapping):
        persisted = output.get("persisted") or output.get("persisted_outputs")
        metadata.update(
            {
                "truncated": any(
                    bool(output.get(key))
                    for key in (
                        "truncated",
                        "stdout_truncated",
                        "stderr_truncated",
                        "results_truncated",
                    )
                ),
                "persisted": bool(persisted),
                "exit_code": output.get("exit_code"),
                "error_type": str(output.get("error_type") or ""),
            }
        )
    return metadata


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
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    if isinstance(normalized.get("input_token_details"), Mapping):
        payload["input_token_details"] = dict(normalized["input_token_details"])
    if isinstance(normalized.get("output_token_details"), Mapping):
        payload["output_token_details"] = dict(normalized["output_token_details"])
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
            output_details.get("reasoning")
            or output_details.get("reasoning_tokens")
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


@dataclass
class PreparedConversationTurn:
    """一次 turn 编排后的标准输入。

    这个对象把“会话归属”“用户最新输入”“投影后的上下文 history”
    和“附件/parts”等运行时所需信息收拢到一起，避免不同 endpoint
    各自重新拼装。
    """

    session_id: str
    invocation_id: str
    user_input: str
    user_display_input: str
    history: list[dict[str, str]]
    input_content: list[dict[str, Any]]
    input_messages: list[dict[str, Any]]
    user_parts: list[dict[str, Any]]
    attachments: list[dict[str, Any]]
    attachment_results: list[dict[str, Any]]
    current_attachments: list[dict[str, Any]]
    current_attachment_results: list[dict[str, Any]]
    has_current_files: bool
    model_metadata: dict[str, Any] = field(default_factory=dict)
    model_options: dict[str, Any] = field(default_factory=dict)
    instructions: str = ""
    request_metadata: dict[str, Any] = field(default_factory=dict)
    compaction_triggered: bool = False
    compaction_trigger: str | None = None
    compacted_until_seq_id: int | None = None
    resume_input: dict[str, Any] | None = None
    # 双维度 run 标识：run_mode=怎么跑（background/foreground），
    # run_trigger=怎么开始（new_run/checkpoint_resume/approval_resume）
    run_mode: str = RUN_MODE_FOREGROUND
    run_trigger: str = RUN_TRIGGER_NEW_RUN


@dataclass
class CompactionPlan:
    """一次 compaction 规划结果。

    预览阶段和真正落 checkpoint 阶段都复用这份规划，避免 `/run_sse`
    与 conversation runtime 各自写一套“是否需要压缩”的条件判断。
    """

    should_compact: bool
    groups_to_compact: list[list[SessionEvent]]
    total_chars: int
    total_estimated_tokens: int
    group_count: int
    tail_groups: int
    auto_compact_threshold_tokens: int | None = None
    auto_compact_threshold_percentage: int | None = None
    compacted_until_seq_id: int | None = None
    pinned_group_indexes: list[int] = field(default_factory=list)
    pinned_state: dict[str, Any] = field(default_factory=dict)


def build_responses_payload(
    *,
    output_text: str,
    model: Optional[str],
    session_id: str,
    response_id: str | None = None,
    created_at: int | None = None,
    status: str = "completed",
    metadata: Mapping[str, Any] | None = None,
    incomplete_details: Mapping[str, Any] | None = None,
    error: Mapping[str, Any] | None = None,
    output_items: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    response_id = response_id or f"resp_{uuid.uuid4().hex}"
    created_at = created_at or int(time.time())
    message_id = f"msg_{uuid.uuid4().hex[:12]}"
    output_item_status = "completed" if status == "completed" else status
    message_item = {
        "id": message_id,
        "type": "message",
        "status": output_item_status,
        "role": "assistant",
        "content": [{"type": "output_text", "text": output_text}],
    }
    normalized_output_items = [
        dict(item) for item in list(output_items or []) if isinstance(item, Mapping)
    ]
    if normalized_output_items:
        output = normalized_output_items
        if not any(str(item.get("type") or "") == "message" for item in output):
            output = [message_item, *output]
    else:
        output = [message_item]
    usage = _responses_usage_payload(_usage_from_metadata(metadata))
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "error": dict(error) if isinstance(error, Mapping) else None,
        "incomplete_details": (
            dict(incomplete_details) if isinstance(incomplete_details, Mapping) else None
        ),
        "instructions": None,
        "metadata": dict(metadata or {}),
        "model": model or "agent",
        "parallel_tool_calls": True,
        "temperature": None,
        "top_p": None,
        "tools": [],
        "output": output,
        "output_text": output_text,
        "usage": usage,
        "session_id": session_id,
    }


def extract_responses_resume_input(input_payload: Any) -> dict[str, Any] | None:
    """Extract OpenAI Responses approval resume input without exposing runner details."""
    if isinstance(input_payload, Mapping):
        candidates = [input_payload]
    elif isinstance(input_payload, Sequence) and not isinstance(
        input_payload, (str, bytes, bytearray)
    ):
        candidates = [item for item in input_payload if isinstance(item, Mapping)]
    else:
        return None

    for item in candidates:
        item_type = str(item.get("type") or "").strip()
        if item_type == "agentengine.resume_checkpoint":
            resume_input = {"type": "agentengine.resume_checkpoint"}
            for key in (
                "run_id",
                "checkpoint_id",
                "resume_attempt_id",
                "framework",
                "framework_ref",
            ):
                if key in item:
                    resume_input[key] = item.get(key)
            return resume_input

        if item_type == "mcp_approval_response":
            resume_input: dict[str, Any] = {"type": "mcp_approval_response"}
            if item.get("id"):
                resume_input["id"] = str(item.get("id"))
            approval_request_id = item.get("approval_request_id")
            if approval_request_id:
                resume_input["approval_request_id"] = str(approval_request_id)
            if "approve" in item:
                resume_input["approve"] = item.get("approve")
            elif "approved" in item:
                resume_input["approve"] = item.get("approved")
            if item.get("reason") is not None:
                resume_input["reason"] = str(item.get("reason") or "")
            return resume_input

        if item_type == "function_call_output":
            call_id = item.get("call_id")
            if not call_id:
                continue
            resume_input = {
                "type": "function_call_output",
                "call_id": str(call_id),
                "output": item.get("output", ""),
            }
            if item.get("id"):
                resume_input["id"] = str(item.get("id"))
            return resume_input

        if item_type in {"ksadk_resume", "ksadk.approval_response"}:
            resume_input = {"type": "ksadk_resume"}
            interrupt_id = (
                item.get("interrupt_id") or item.get("approval_request_id") or item.get("id")
            )
            if interrupt_id:
                resume_input["interrupt_id"] = str(interrupt_id)
            if "value" in item:
                resume_input["value"] = item.get("value")
            elif "resume" in item:
                resume_input["value"] = item.get("resume")
            else:
                resume_input["value"] = {
                    key: value
                    for key, value in item.items()
                    if key not in {"type", "interrupt_id", "approval_request_id", "id"}
                }
            return resume_input

    return None


def build_chat_completions_payload(
    *,
    output_text: str,
    model: Optional[str],
    session_id: str,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    usage = _chat_usage_payload(_usage_from_metadata(metadata))
    payload = {
        "id": f"chatcmpl-{uuid.uuid4()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or "agent",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": output_text},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
        "session_id": session_id,
    }
    if isinstance(metadata, Mapping) and metadata:
        payload["metadata"] = dict(metadata)
    return payload


def build_compaction_sse_event(
    *,
    phase: str,
    trigger: str,
    compacted_until_seq_id: int | None = None,
    total_chars: int | None = None,
    total_estimated_tokens: int | None = None,
    group_count: int | None = None,
    threshold_percentage: int | None = None,
) -> str:
    """统一生成 compaction 相关 SSE，方便不同入口保持同一语义。"""

    payload: dict[str, Any] = {
        "phase": phase,
        "trigger": trigger,
        "timestamp": int(time.time() * 1000),
    }
    if compacted_until_seq_id is not None:
        payload["compacted_until_seq_id"] = compacted_until_seq_id
    if total_chars is not None:
        payload["total_chars"] = total_chars
    if total_estimated_tokens is not None:
        payload["total_estimated_tokens"] = total_estimated_tokens
    if group_count is not None:
        payload["group_count"] = group_count
    if threshold_percentage is not None:
        payload["threshold_percentage"] = threshold_percentage
    return (
        f"event: response.compaction.{phase}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )


def _is_prompt_too_long_error(exc: Exception) -> bool:
    """尽量用宽松规则识别 PTL，兼容不同 runtime/模型返回格式。"""
    lowered = str(exc or "").lower()
    return any(marker in lowered for marker in PROMPT_TOO_LONG_MARKERS)


def _runner_name(runner: Any) -> str:
    return str(getattr(getattr(runner, "detection_result", None), "name", "assistant"))


def _runner_type_name(runner: Any) -> str:
    runner_type = getattr(getattr(runner, "detection_result", None), "type", None)
    runner_value = getattr(runner_type, "value", runner_type)
    normalized = str(runner_value or "").strip()
    if normalized:
        return normalized
    return runner.__class__.__name__.lower()


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = str(raw).strip().lower()
    if not normalized:
        return default
    return normalized not in {"0", "false", "no", "off"}


def _ltm_auto_save_enabled() -> bool:
    backend = str(os.getenv("KSADK_LTM_BACKEND") or "").strip().lower()
    namespace = str(os.getenv("KSADK_LTM_NAMESPACE") or "").strip()
    if not backend and not namespace:
        return False
    default = backend == "sdk" and bool(namespace)
    return _env_flag("KSADK_LTM_AUTO_SAVE", default)


def _ambient_policy(prefix: str, default: str = "on_demand") -> str:
    if not _env_flag(f"{prefix}_AMBIENT_ENABLED", True):
        return "disabled"

    raw = str(os.getenv(f"{prefix}_AMBIENT_POLICY", default) or "").strip().lower()
    if raw in {"", "on_demand", "ondemand", "heuristic", "auto"}:
        return "on_demand"
    if raw in {"always", "eager"}:
        return "always"
    if raw in {"disabled", "off", "false", "0"}:
        return "disabled"
    return default


def _normalize_ambient_query(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _contains_any_fragment(text: str, fragments: Sequence[str]) -> bool:
    return any(fragment in text for fragment in fragments)


def _ambient_context_has_error(context: Any) -> bool:
    if not isinstance(context, dict):
        return True

    formatted_text = str(context.get("formatted_text") or "").strip()
    if not formatted_text:
        return True

    failure_prefixes = (
        "知识库检索失败",
        "长期记忆检索失败",
    )
    return formatted_text.startswith(failure_prefixes)


def _is_chitchat_query(text: str) -> bool:
    normalized = _normalize_ambient_query(text)
    if not normalized:
        return True

    exact_matches = {
        "hi",
        "hello",
        "hey",
        "你好",
        "您好",
        "嗨",
        "在吗",
        "收到",
        "好的",
        "ok",
        "okay",
        "thanks",
        "thank you",
        "谢谢",
        "测试",
        "test",
        "ping",
    }
    if normalized in exact_matches:
        return True

    chatter_fragments = (
        "介绍一下你自己",
        "介绍你自己",
        "你是谁",
        "你能做什么",
        "what can you do",
        "who are you",
        "introduce yourself",
        "一句测试",
    )
    return any(fragment in normalized for fragment in chatter_fragments)


def _should_load_kb_ambient_context(user_input: str) -> bool:
    normalized = _normalize_ambient_query(user_input)
    if not normalized or _is_chitchat_query(normalized):
        return False

    kb_fragments = (
        "知识库",
        "文档",
        "手册",
        "说明",
        "wiki",
        "资料",
        "教程",
        "api",
        "接口",
        "参数",
        "配置",
        "规格",
        "机型",
        "实例",
        "部署",
        "步骤",
        "区别",
        "差异",
        "原理",
        "架构",
        "能力",
        "限制",
        "最佳实践",
        "价格",
        "套餐",
        "支持",
        "有哪些",
        "什么是",
        "为什么",
        "怎么",
        "如何",
        "查询",
        "查一下",
        "列一下",
        "介绍一下",
        "总结",
        "概述",
        "解释",
        "说明一下",
        "对比",
        "比较",
        "what",
        "how",
        "why",
        "which",
        "list",
        "show me",
        "tell me",
        "summarize",
        "summary",
        "explain",
        "difference",
        "steps",
        "deployment",
        "lookup",
        "look up",
        "search",
        "compare",
    )
    if _contains_any_fragment(normalized, kb_fragments):
        return True

    query_verbs = (
        "查",
        "查一下",
        "查询",
        "列出",
        "列一下",
        "总结",
        "概述",
        "解释",
        "说明",
        "介绍",
        "对比",
        "比较",
        "看看",
        "告诉我",
        "what",
        "how",
        "why",
        "which",
        "list",
        "show",
        "tell",
        "summarize",
        "explain",
        "compare",
    )
    kb_subjects = (
        "知识库",
        "文档",
        "手册",
        "教程",
        "wiki",
        "部署",
        "步骤",
        "api",
        "接口",
        "参数",
        "配置",
        "规格",
        "机型",
        "实例",
        "价格",
        "套餐",
        "支持",
        "区别",
        "差异",
        "原理",
        "架构",
        "能力",
        "限制",
    )
    return _contains_any_fragment(normalized, query_verbs) and _contains_any_fragment(
        normalized, kb_subjects
    )


def _should_load_memory_ambient_context(user_input: str) -> bool:
    normalized = _normalize_ambient_query(user_input)
    if not normalized or _is_chitchat_query(normalized):
        return False

    explicit_memory_fragments = (
        "记得",
        "记住",
        "记忆",
        "回忆",
        "历史",
        "偏好",
        "习惯",
        "还记得",
        "记得我",
        "记住这个",
        "remember",
        "memory",
        "recall",
        "history",
        "preference",
    )
    profile_fragments = (
        "我的名字",
        "我叫什么",
        "你知道我的名字",
        "我的风格",
        "按我的风格",
        "按照我的风格",
        "我的偏好",
        "我的习惯",
        "我的背景",
        "关于我的",
        "我喜欢",
        "我不喜欢",
        "my name",
        "my style",
        "my preference",
        "about me",
    )
    short_term_fragments = (
        "前面的回答",
        "前面的内容",
        "上面的回答",
        "上面的内容",
        "刚才的回答",
        "刚刚的回答",
        "上一条",
        "上一轮",
        "继续刚才",
        "继续上面",
        "翻译成英文",
        "翻译成中文",
    )
    temporal_fragments = ("上次", "之前", "以前", "earlier", "last time", "previous")
    speech_fragments = ("聊过", "说过", "提过", "告诉过", "mentioned", "told")

    if _contains_any_fragment(normalized, short_term_fragments) and not _contains_any_fragment(
        normalized, profile_fragments
    ):
        return False

    if _contains_any_fragment(normalized, explicit_memory_fragments) or _contains_any_fragment(
        normalized, profile_fragments
    ):
        return True

    return _contains_any_fragment(normalized, temporal_fragments) and _contains_any_fragment(
        normalized, speech_fragments
    )


def _should_use_platform_ambient_context(runner: Any) -> bool:
    detection_type = getattr(getattr(runner, "detection_result", None), "type", None)
    runner_type = str(getattr(detection_type, "value", detection_type) or "").strip().lower()
    if runner_type:
        return runner_type != "adk"

    class_name = runner.__class__.__name__.lower()
    module_name = getattr(runner.__class__, "__module__", "").lower()
    return class_name != "adkrunner" and "google_adk" not in module_name


def _build_runner_ambient_contexts(
    *,
    runner: Any,
    user_id: str,
    user_input: str,
) -> dict[str, Any]:
    contexts: dict[str, Any] = {
        "kb_context": None,
        "memory_context": None,
    }
    normalized_input = str(user_input or "").strip()
    if not normalized_input or not _should_use_platform_ambient_context(runner):
        return contexts

    kb_policy = _ambient_policy("KSADK_KB", "on_demand")
    if (
        kb_policy == "always"
        or (kb_policy == "on_demand" and _should_load_kb_ambient_context(normalized_input))
    ) and KnowledgeBaseService.is_configured():
        try:
            kb_context = KnowledgeBaseService.from_env().build_context(normalized_input)
            if not _ambient_context_has_error(kb_context):
                contexts["kb_context"] = kb_context
        except Exception as exc:
            logger.warning("Failed to build ambient knowledge context: %s", exc)

    ltm_policy = _ambient_policy("KSADK_LTM", "on_demand")
    if (
        ltm_policy == "always"
        or (ltm_policy == "on_demand" and _should_load_memory_ambient_context(normalized_input))
    ) and LongTermMemoryService.is_configured():
        try:
            memory_context = LongTermMemoryService.from_env().build_context(
                user_id=user_id,
                query=normalized_input,
            )
            if not _ambient_context_has_error(memory_context):
                contexts["memory_context"] = memory_context
        except Exception as exc:
            logger.warning("Failed to build ambient memory context: %s", exc)

    return contexts


def _build_runner_request_payload(
    *,
    prepared: PreparedConversationTurn,
    model: str | None,
    runtime_context: PlatformInvocationContext,
) -> dict[str, Any]:
    payload = {
        "session_id": prepared.session_id,
        "input": prepared.user_input,
        "history": prepared.history,
        "input_content": prepared.input_content,
        "input_messages": prepared.input_messages,
        "input_parts": prepared.user_parts,
        "attachments": prepared.attachments,
        "attachment_results": prepared.attachment_results,
        "current_attachments": prepared.current_attachments,
        "current_attachment_results": prepared.current_attachment_results,
        "has_current_files": prepared.has_current_files,
        "model": model,
        "model_metadata": prepared.model_metadata,
        "model_options": prepared.model_options,
        "platform_context": runtime_context.to_payload(),
        "kb_context": runtime_context.kb_context,
        "memory_context": runtime_context.memory_context,
        "invocation_id": prepared.invocation_id,
    }
    if prepared.instructions:
        payload["instructions"] = prepared.instructions
    if prepared.resume_input is not None:
        if _is_checkpoint_resume_input(prepared.resume_input):
            payload["input"] = prepared.resume_input
            payload["checkpoint_resume"] = True
            payload["run_id"] = str(prepared.resume_input.get("run_id") or "")
            payload["checkpoint_id"] = str(prepared.resume_input.get("checkpoint_id") or "")
            payload["framework_ref"] = dict(prepared.resume_input.get("framework_ref") or {})
            payload["metadata"] = dict(prepared.resume_input.get("metadata") or {})
            payload["checkpoint_metadata"] = dict(
                prepared.resume_input.get("checkpoint_metadata") or {}
            )
        else:
            payload["input"] = prepared.resume_input
            payload["resume"] = True
    previous_response_id = prepared.request_metadata.get("previous_response_id")
    if previous_response_id:
        payload["previous_response_id"] = str(previous_response_id)
    conversation = prepared.request_metadata.get("conversation")
    if conversation:
        payload["conversation"] = conversation
    if prepared.request_metadata.get("responses_conversation"):
        payload["responses_conversation"] = True
    deferred_tool_names = _extract_deferred_tool_names(prepared.request_metadata)
    if deferred_tool_names:
        payload["deferred_tool_names"] = deferred_tool_names
    return payload


def _inject_runner_deferred_tools_for_request(
    runner: Any, prepared: PreparedConversationTurn
) -> None:
    deferred_tool_names = _extract_deferred_tool_names(prepared.request_metadata)
    if not deferred_tool_names:
        return
    inject = getattr(runner, "inject_deferred_tools_for_request", None)
    if not callable(inject):
        return
    try:
        inject(deferred_tool_names)
    except Exception as exc:
        logger.warning("Failed to inject deferred tools into runner: %s", exc)


def _attachment_summary_for_memory(
    attachments: Sequence[Mapping[str, Any]],
    attachment_results: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for item in attachment_results:
        if not isinstance(item, Mapping):
            continue
        summary = {
            "kind": str(item.get("kind") or "file"),
            "display_name": str(
                item.get("display_name") or item.get("filename") or "uploaded_file"
            ),
            "mime_type": str(item.get("mime_type") or "application/octet-stream"),
        }
        summaries.append(summary)

    if summaries:
        return summaries

    for item in attachments:
        if not isinstance(item, Mapping):
            continue
        mime_type = str(item.get("mime_type") or "application/octet-stream")
        display_name = str(item.get("display_name") or item.get("filename") or "uploaded_file")
        kind = "image" if mime_type.startswith("image/") else "file"
        summaries.append(
            {
                "kind": kind,
                "display_name": display_name,
                "mime_type": mime_type,
            }
        )
    return summaries


def _memory_turn_event_strings(
    *,
    prepared: PreparedConversationTurn,
    output_text: str,
    metadata: Mapping[str, Any],
) -> list[str]:
    event_strings: list[str] = []
    user_text = _input_text_for_memory(prepared)
    if user_text:
        user_metadata = dict(metadata)
        attachment_summary = _attachment_summary_for_memory(
            prepared.current_attachments,
            prepared.current_attachment_results,
        )
        if attachment_summary:
            user_metadata["attachments"] = attachment_summary
        event_strings.append(
            json.dumps(
                {
                    "role": "user",
                    "parts": [{"text": user_text}],
                    "metadata": user_metadata,
                },
                ensure_ascii=False,
            )
        )

    assistant_text = strip_reasoning_markup(str(output_text or "")).strip()
    if assistant_text:
        event_strings.append(
            json.dumps(
                {
                    "role": "assistant",
                    "parts": [{"text": assistant_text}],
                    "metadata": dict(metadata),
                },
                ensure_ascii=False,
            )
        )
    return event_strings


def _input_text_for_memory(prepared: PreparedConversationTurn) -> str:
    text_parts: list[str] = []
    for item in prepared.input_content:
        if isinstance(item, Mapping) and item.get("type") == "input_text":
            text = str(item.get("text") or "").strip()
            if text:
                text_parts.append(text)
    if text_parts:
        return "\n".join(text_parts).strip()
    return str(prepared.user_input or prepared.user_display_input or "").strip()


async def _auto_save_ltm_turn(
    *,
    agent_id: str,
    user_id: str,
    prepared: PreparedConversationTurn,
    output_text: str,
    runner_type: str,
    model: str | None,
) -> None:
    if prepared.resume_input is not None or not _ltm_auto_save_enabled():
        return

    metadata: dict[str, Any] = {
        "agent_id": str(agent_id or ""),
        "session_id": prepared.session_id,
        "invocation_id": prepared.invocation_id,
        "runner_type": runner_type,
    }
    if model:
        metadata["model"] = model

    platform_context = prepared.request_metadata.get("platform_context")
    if isinstance(platform_context, Mapping):
        metadata["agent_id"] = str(platform_context.get("agent_id") or metadata["agent_id"])

    if not metadata["agent_id"]:
        metadata["agent_id"] = str(os.getenv("KSADK_LTM_AGENT_ID") or "")

    event_strings = _memory_turn_event_strings(
        prepared=prepared,
        output_text=output_text,
        metadata=metadata,
    )
    if not event_strings:
        return

    try:
        service = LongTermMemoryService.from_env()
        service.save_event_strings(
            user_id=user_id,
            event_strings=event_strings,
            metadata=metadata,
        )
    except Exception as exc:
        logger.warning("Failed to auto-save conversation turn to long-term memory: %s", exc)


def _merge_request_history_with_session_history(
    request_history: Sequence[dict[str, str]],
    session_history: Sequence[dict[str, str]],
) -> list[dict[str, str]]:
    if not request_history:
        return list(session_history)
    if not session_history:
        return list(request_history)

    normalized_request = [
        {
            "role": str(item.get("role") or ""),
            "content": str(item.get("content") or "").strip(),
        }
        for item in request_history
    ]
    normalized_session = [
        {
            "role": str(item.get("role") or ""),
            "content": str(item.get("content") or "").strip(),
        }
        for item in session_history
    ]
    prefix_len = min(len(normalized_request), len(normalized_session))
    if normalized_request[:prefix_len] == normalized_session[:prefix_len]:
        return [*list(request_history), *list(session_history)[prefix_len:]]
    return [*list(request_history), *list(session_history)]


def _has_pending_approval(events: Sequence[SessionEvent]) -> bool:
    pending = 0
    for event in events:
        event_type = canonical_event_type(
            event.event_type,
            author=event.author,
            role=str((event.content or {}).get("role") or ""),
        )
        if event_type == "approval_request":
            pending += 1
        elif event_type == "approval_response" and pending > 0:
            pending -= 1
    return pending > 0


def _approval_request_id_from_event(event: SessionEvent) -> str:
    metadata = event.metadata or {}
    interrupt_info = metadata.get("interrupt_info")
    if isinstance(interrupt_info, Mapping):
        value = interrupt_info.get("approval_request_id") or interrupt_info.get("id")
        if value:
            return str(value)
    return str(event.id or "")


def _pending_approval_events(events: Sequence[SessionEvent]) -> list[SessionEvent]:
    pending: list[SessionEvent] = []
    for event in events:
        event_type = canonical_event_type(
            event.event_type,
            author=event.author,
            role=str((event.content or {}).get("role") or ""),
        )
        if event_type == "approval_request":
            pending.append(event)
            continue
        if event_type != "approval_response" or not pending:
            continue
        resume_input = (event.metadata or {}).get("resume_input")
        response_id = ""
        if isinstance(resume_input, Mapping):
            response_id = str(
                resume_input.get("approval_request_id")
                or resume_input.get("interrupt_id")
                or resume_input.get("id")
                or ""
            )
        if response_id:
            pending = [
                item for item in pending if _approval_request_id_from_event(item) != response_id
            ]
        else:
            pending.pop()
    return pending


def _approval_request_events(events: Sequence[SessionEvent]) -> list[SessionEvent]:
    return [
        event
        for event in events
        if canonical_event_type(
            event.event_type,
            author=event.author,
            role=str((event.content or {}).get("role") or ""),
        )
        == "approval_request"
    ]


def _approval_resume_run_mode(
    events: Sequence[SessionEvent],
    resume_input: Mapping[str, Any],
    *,
    fallback: str,
) -> str:
    target_ids = {
        str(value)
        for value in (
            resume_input.get("approval_request_id"),
            resume_input.get("interrupt_id"),
            resume_input.get("id"),
        )
        if value
    }
    approval_events = _pending_approval_events(events) or _approval_request_events(events)
    for approval_event in reversed(approval_events):
        approval_id = _approval_request_id_from_event(approval_event)
        if target_ids and approval_id not in target_ids:
            continue
        approval_invocation_id = str(approval_event.invocation_id or "")
        if not approval_invocation_id:
            continue
        for event in reversed(events):
            if event.event_type != "run_status" or event.invocation_id != approval_invocation_id:
                continue
            metadata = event.metadata or {}
            state_delta = event.state_delta or {}
            active_run = state_delta.get("active_run") if isinstance(state_delta, Mapping) else None
            state_mode = active_run.get("run_mode") if isinstance(active_run, Mapping) else None
            mode = validate_run_mode(str(metadata.get("run_mode") or state_mode or ""))
            if mode != RUN_MODE_UNKNOWN:
                return mode
            break
    return validate_run_mode(fallback)


def _parse_approval_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _approval_decision_from_resume(resume_input: Mapping[str, Any]) -> dict[str, Any]:
    if "approve" in resume_input:
        approved = bool(resume_input.get("approve"))
    elif "approved" in resume_input:
        approved = bool(resume_input.get("approved"))
    else:
        approved = bool(
            resume_input.get("value", {}).get("approved")
            if isinstance(resume_input.get("value"), Mapping)
            else True
        )
    decision = {
        "approved": approved,
        "approval_request_id": str(
            resume_input.get("approval_request_id")
            or resume_input.get("interrupt_id")
            or resume_input.get("id")
            or ""
        ),
    }
    if resume_input.get("reason") is not None:
        decision["reason"] = str(resume_input.get("reason") or "")
    return decision


def _consecutive_approval_denials_from_events(events: Sequence[SessionEvent]) -> int:
    denials = 0
    for event in reversed(events):
        event_type = canonical_event_type(
            event.event_type,
            author=event.author,
            role=str((event.content or {}).get("role") or ""),
        )
        if event_type != "approval_response":
            continue
        resume_input = (event.metadata or {}).get("resume_input")
        if not isinstance(resume_input, Mapping):
            break
        decision = resume_input.get("approval")
        if not isinstance(decision, Mapping):
            decision = _approval_decision_from_resume(resume_input)
        if bool(decision.get("approved") or decision.get("approve")):
            break
        denials += 1
    return denials


def _normalize_approval_resume_input(
    resume_input: Mapping[str, Any],
    events: Sequence[SessionEvent],
    *,
    include_resolved: bool = False,
) -> dict[str, Any]:
    normalized = dict(resume_input)
    if not _is_approval_resume_input(normalized):
        return normalized

    approval_request_id = str(
        normalized.get("approval_request_id") or normalized.get("interrupt_id") or ""
    )
    pending_events = (
        _approval_request_events(events) if include_resolved else _pending_approval_events(events)
    )
    matched_event = None
    for event in reversed(pending_events):
        if not approval_request_id or _approval_request_id_from_event(event) == approval_request_id:
            matched_event = event
            break
    if matched_event is None:
        return normalized

    interrupt_info = (matched_event.metadata or {}).get("interrupt_info")
    if not isinstance(interrupt_info, Mapping):
        return normalized
    if not (interrupt_info.get("tool_name") or normalized.get("tool_name")):
        return normalized

    decision = _approval_decision_from_resume(normalized)
    tool_args = _parse_approval_arguments(
        interrupt_info.get("arguments")
        or interrupt_info.get("tool_args")
        or interrupt_info.get("args")
        or {}
    )
    tool_args["approval"] = decision

    if not normalized.get("approval_request_id"):
        request_id = _approval_request_id_from_event(matched_event)
        if request_id:
            normalized["approval_request_id"] = request_id
            decision["approval_request_id"] = request_id
    if interrupt_info.get("tool_name") and not normalized.get("tool_name"):
        normalized["tool_name"] = str(interrupt_info.get("tool_name"))
    if interrupt_info.get("run_id") and not normalized.get("run_id"):
        normalized["run_id"] = str(interrupt_info.get("run_id"))
    normalized["approval"] = decision
    normalized["tool_args"] = tool_args
    return normalized


def _builtin_tool_callable(tool_name: str):
    name = str(tool_name or "").strip()
    if not name:
        return None
    if name in {
        "write_workspace_file",
        "write_workspace_files",
        "delete_workspace_file",
    }:
        from ksadk.toolsets import workspace

        return getattr(workspace, name, None)
    if name == "execute_skills":
        from ksadk.toolsets.skills import execute_skills

        return execute_skills
    if name in {"run_command", "run_code"}:
        from ksadk.toolsets import sandbox

        return getattr(sandbox, name, None)
    return None


def _tool_receipt_metadata(
    *,
    session_id: str,
    run_id: str,
    tool_name: str,
    tool_args: Mapping[str, Any],
    tool_call_id: str | None = None,
    checkpoint_id: str | None = None,
    framework: str | None = None,
    framework_ref: Mapping[str, Any] | None = None,
    status: str = "completed",
) -> dict[str, Any]:
    idempotency_key = build_tool_receipt_idempotency_key(
        session_id=session_id,
        run_id=run_id,
        checkpoint_id=checkpoint_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        tool_args=tool_args,
    )
    return {
        "receipt_id": f"tr_{idempotency_key.removeprefix('tool_receipt:')[:24]}",
        "idempotency_key": idempotency_key,
        "tool_name": tool_name,
        "tool_call_id": tool_call_id or run_id,
        "run_id": run_id,
        "checkpoint_id": checkpoint_id or "",
        "framework": framework or "",
        "framework_ref": dict(framework_ref or {}),
        "status": status,
        "created_at": time.time(),
    }


def _tool_receipt_status_from_output(output: Any) -> str:
    if not isinstance(output, Mapping):
        return "failed"
    status = str(output.get("status") or "").strip().lower()
    if status == "accepted_not_extracted":
        return "completed"
    return "completed" if output.get("ok") is not False else "failed"


def _tool_resume_run_id(resume_input: Mapping[str, Any]) -> str:
    return str(
        resume_input.get("run_id")
        or resume_input.get("call_id")
        or resume_input.get("approval_request_id")
        or resume_input.get("interrupt_id")
        or ""
    )


def _tool_receipt_idempotency_key_for_resume(
    *,
    session_id: str,
    resume_input: Mapping[str, Any],
) -> str | None:
    tool_name = str(resume_input.get("tool_name") or "").strip()
    if not tool_name:
        return None
    tool_args = resume_input.get("tool_args")
    if not isinstance(tool_args, Mapping):
        return None
    run_id = _tool_resume_run_id(resume_input)
    if not run_id:
        return None
    return build_tool_receipt_idempotency_key(
        session_id=session_id,
        run_id=run_id,
        tool_call_id=run_id,
        tool_name=tool_name,
        tool_args=dict(tool_args),
    )


def _find_tool_receipt_event_by_key(
    events: Sequence[SessionEvent],
    idempotency_key: str,
) -> SessionEvent | None:
    for event in reversed(events):
        if event.event_type != "tool_result":
            continue
        metadata = event.metadata or {}
        receipt = metadata.get("tool_receipt")
        if not isinstance(receipt, Mapping):
            continue
        if str(receipt.get("idempotency_key") or "") == idempotency_key:
            return event
    return None


def _latest_checkpoint_metadata_for_run(
    events: Sequence[SessionEvent],
    run_id: str,
) -> dict[str, Any]:
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        return {}
    for event in reversed(events):
        if event.event_type != "run_checkpoint":
            continue
        metadata = event.metadata or {}
        if str(metadata.get("run_id") or "").strip() != normalized_run_id:
            continue
        checkpoint_id = str(metadata.get("checkpoint_id") or "").strip()
        if not checkpoint_id:
            continue
        framework_ref = metadata.get("framework_ref")
        return {
            "checkpoint_id": checkpoint_id,
            "framework": str(metadata.get("framework") or ""),
            "framework_ref": dict(framework_ref) if isinstance(framework_ref, Mapping) else {},
        }
    return {}


async def _execute_approved_builtin_tool_resume(
    *,
    session_id: str,
    invocation_id: str,
    resume_input: Mapping[str, Any],
    session_service_provider: Callable[[], Any],
) -> dict[str, Any] | None:
    approval = resume_input.get("approval")
    if not isinstance(approval, Mapping) or not bool(approval.get("approved")):
        return None
    tool_name = str(resume_input.get("tool_name") or "").strip()
    tool_func = _builtin_tool_callable(tool_name)
    if tool_func is None:
        return None

    tool_args = resume_input.get("tool_args")
    if not isinstance(tool_args, Mapping):
        return None
    call_args = dict(tool_args)
    run_id = _tool_resume_run_id(resume_input)
    service = session_service_provider()
    existing_events = await service.get_events(session_id)
    checkpoint_metadata = _latest_checkpoint_metadata_for_run(existing_events, run_id)
    receipt = _tool_receipt_metadata(
        session_id=session_id,
        run_id=run_id,
        tool_call_id=run_id,
        tool_name=tool_name,
        tool_args=call_args,
        checkpoint_id=checkpoint_metadata.get("checkpoint_id"),
        framework=checkpoint_metadata.get("framework"),
        framework_ref=checkpoint_metadata.get("framework_ref"),
    )
    existing_event = _find_tool_receipt_event_by_key(
        existing_events,
        receipt["idempotency_key"],
    )
    if existing_event is not None:
        existing_metadata = existing_event.metadata or {}
        output = existing_metadata.get("tool_output", "")
        if isinstance(output, Mapping):
            output = {**dict(output), "replayed": True}
        replayed_receipt = {
            **dict((existing_metadata.get("tool_receipt") or receipt)),
            "replayed": True,
            "replayed_from_event_id": existing_event.id,
        }
        await append_conversation_event(
            session_id=session_id,
            author="tool",
            role="user",
            text=str(output),
            invocation_id=invocation_id,
            event_type="tool_result",
            session_service_provider=session_service_provider,
            metadata={
                "tool_name": tool_name,
                "tool_args": call_args,
                "tool_output": output,
                "run_id": run_id,
                "approval_request_id": resume_input.get("approval_request_id")
                or resume_input.get("interrupt_id"),
                "tool_receipt": replayed_receipt,
                "replayed": True,
            },
        )
        return {
            "type": "function_call_output",
            "call_id": run_id,
            "output": output,
        }

    try:
        output = tool_func(**call_args)
    except Exception as exc:
        output = {"ok": False, "error_type": type(exc).__name__, "error_message": str(exc)}

    receipt["status"] = _tool_receipt_status_from_output(output)
    await append_conversation_event(
        session_id=session_id,
        author="tool",
        role="user",
        text=str(output),
        invocation_id=invocation_id,
        event_type="tool_result",
        session_service_provider=session_service_provider,
        metadata={
            "tool_name": tool_name,
            "tool_args": call_args,
            "tool_output": output,
            "run_id": run_id,
            "approval_request_id": resume_input.get("approval_request_id")
            or resume_input.get("interrupt_id"),
            "tool_receipt": receipt,
        },
    )
    return {
        "type": "function_call_output",
        "call_id": run_id,
        "output": output,
    }


def _is_approval_resume_input(resume_input: Mapping[str, Any]) -> bool:
    return str(resume_input.get("type") or "").strip() in {
        "mcp_approval_response",
        "ksadk_resume",
        "ksadk.approval_response",
    }


def _is_checkpoint_resume_input(resume_input: Mapping[str, Any]) -> bool:
    return str(resume_input.get("type") or "").strip() == "agentengine.resume_checkpoint"


def _failed_status_for_resume(resume_input: Mapping[str, Any] | None) -> str:
    """checkpoint resume 失败时返回 resume_failed，否则返回 failed。

    仅 checkpoint resume 的失败才写 resume_failed（独立终态，触发 SSE [DONE]
    并让前端展示"恢复失败"）；approval/ksadk_resume 等其他 resume 失败仍写 failed。
    """
    if resume_input is not None and _is_checkpoint_resume_input(resume_input):
        return "resume_failed"
    return "failed"


def _normalize_checkpoint_resume_input(resume_input: Mapping[str, Any]) -> dict[str, Any]:
    run_id = str(resume_input.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("Checkpoint resume requires run_id")

    checkpoint_id = str(resume_input.get("checkpoint_id") or "").strip()
    if not checkpoint_id:
        raise ValueError("Checkpoint resume requires checkpoint_id")

    framework = str(resume_input.get("framework") or "langgraph").strip() or "langgraph"
    raw_framework_ref = resume_input.get("framework_ref")
    framework_ref = dict(raw_framework_ref) if isinstance(raw_framework_ref, Mapping) else {}
    raw_framework_detail = framework_ref.get(framework)
    framework_detail = (
        dict(raw_framework_detail) if isinstance(raw_framework_detail, Mapping) else {}
    )
    framework_detail.setdefault("checkpoint_id", checkpoint_id)
    if resume_input.get("thread_id") and not framework_detail.get("thread_id"):
        framework_detail["thread_id"] = str(resume_input.get("thread_id"))
    framework_ref[framework] = framework_detail

    resume_attempt_id = str(resume_input.get("resume_attempt_id") or "").strip()
    if not resume_attempt_id:
        resume_attempt_id = f"resume_{uuid.uuid4().hex}"

    return {
        "type": "agentengine.resume_checkpoint",
        "run_id": run_id,
        "checkpoint_id": checkpoint_id,
        "resume_attempt_id": resume_attempt_id,
        "framework": framework,
        "framework_ref": framework_ref,
        "metadata": dict(resume_input.get("metadata") or resume_input.get("Metadata") or {}),
        "checkpoint_metadata": dict(
            resume_input.get("checkpoint_metadata")
            or resume_input.get("CheckpointMetadata")
            or resume_input.get("metadata")
            or resume_input.get("Metadata")
            or {}
        ),
        "resume_instruction_enabled": bool(
            resume_input.get("resume_instruction_enabled")
            or resume_input.get("ResumeInstructionEnabled")
        ),
        "resume_instruction": str(
            resume_input.get("resume_instruction") or resume_input.get("ResumeInstruction") or ""
        ).strip(),
    }


def _agentengine_resume_metadata(resume_input: Mapping[str, Any] | None) -> dict[str, Any]:
    if not resume_input or not _is_checkpoint_resume_input(resume_input):
        return {}
    return {
        "agentengine": {
            "action": "resume_checkpoint",
            "run_id": str(resume_input.get("run_id") or ""),
            "checkpoint_id": str(resume_input.get("checkpoint_id") or ""),
            "resume_attempt_id": str(resume_input.get("resume_attempt_id") or ""),
            "framework": str(resume_input.get("framework") or ""),
            "framework_ref": dict(resume_input.get("framework_ref") or {}),
        }
    }


def _extract_agentengine_metadata(result: Mapping[str, Any] | None) -> dict[str, Any]:
    if not result:
        return {}
    metadata = result.get("metadata")
    if not isinstance(metadata, Mapping):
        return {}
    agentengine = metadata.get("agentengine")
    if not isinstance(agentengine, Mapping):
        return {}
    return {"agentengine": dict(agentengine)}


def _checkpoint_event_args_from_agentengine_metadata(
    agentengine_metadata: Mapping[str, Any] | None,
    *,
    fallback_run_id: str,
) -> dict[str, Any] | None:
    if not isinstance(agentengine_metadata, Mapping):
        return None
    framework = str(agentengine_metadata.get("framework") or "langgraph").strip() or "langgraph"
    raw_framework_ref = agentengine_metadata.get("framework_ref")
    if not isinstance(raw_framework_ref, Mapping):
        return None
    framework_ref = dict(raw_framework_ref)
    raw_framework_detail = framework_ref.get(framework)
    if not isinstance(raw_framework_detail, Mapping):
        return None
    framework_detail = dict(raw_framework_detail)
    checkpoint_id = str(framework_detail.get("checkpoint_id") or "").strip()
    if not checkpoint_id:
        return None
    run_id = str(agentengine_metadata.get("run_id") or fallback_run_id or "").strip()
    if not run_id:
        return None
    phase = str(agentengine_metadata.get("phase") or "").strip()
    display_metadata = {
        key: value
        for key, value in agentengine_metadata.items()
        if key
        not in {
            "run_id",
            "checkpoint_id",
            "framework",
            "framework_ref",
            "phase",
        }
    }
    return {
        "run_id": run_id,
        "checkpoint_id": checkpoint_id,
        "framework": framework,
        "framework_ref": framework_ref,
        "phase": phase,
        "metadata": display_metadata,
    }


def _merge_agentengine_metadata(
    *metadata_items: Mapping[str, Any] | None,
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for metadata in metadata_items:
        if not isinstance(metadata, Mapping):
            continue
        agentengine = metadata.get("agentengine")
        if not isinstance(agentengine, Mapping):
            continue
        next_agentengine = dict(agentengine)
        merged.update(next_agentengine)
        if isinstance(agentengine.get("framework_ref"), Mapping):
            existing_framework_ref = (
                merged.get("framework_ref")
                if isinstance(merged.get("framework_ref"), Mapping)
                else {}
            )
            merged["framework_ref"] = {
                **dict(existing_framework_ref),
                **dict(agentengine.get("framework_ref") or {}),
            }
    return {"agentengine": merged} if merged else {}


def _format_resume_response_text(resume_input: Mapping[str, Any]) -> str:
    item_type = str(resume_input.get("type") or "resume")
    if item_type == "mcp_approval_response":
        approval_request_id = str(resume_input.get("approval_request_id") or "")
        approve = resume_input.get("approve")
        reason = str(resume_input.get("reason") or "").strip()
        parts = [f"mcp_approval_response approval_request_id={approval_request_id}"]
        if approve is not None:
            parts.append(f"approve={bool(approve)}")
        if reason:
            parts.append(f"reason={reason}")
        return " ".join(parts)

    if item_type == "function_call_output":
        output = resume_input.get("output", "")
        if isinstance(output, (dict, list)):
            output_text = json.dumps(output, ensure_ascii=False, sort_keys=True)
        else:
            output_text = str(output)
        return (
            f"function_call_output call_id={resume_input.get('call_id') or ''} output={output_text}"
        )

    return f"{item_type} {json.dumps(dict(resume_input), ensure_ascii=False, sort_keys=True)}"


def _stringify_responses_item_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def _responses_output_item_text(item: Mapping[str, Any]) -> str:
    for text_field in ("output_text", "text", "summary_text", "delta"):
        value = item.get(text_field)
        if isinstance(value, str) and value:
            return value
    summary = item.get("summary")
    if isinstance(summary, Sequence) and not isinstance(summary, (str, bytes, bytearray)):
        parts: list[str] = []
        for part in summary:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Mapping):
                text = part.get("text") or part.get("summary_text")
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            return "".join(parts)
    content = item.get("content")
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Mapping):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    if isinstance(content, str):
        return content
    return ""


def _semantic_events_from_responses_output(output: Sequence[Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    tool_names: dict[str, str] = {}
    for raw_item in output:
        if not isinstance(raw_item, Mapping):
            continue
        item = dict(raw_item)
        item_type = str(item.get("type") or "").strip()
        item_id = str(item.get("id") or item.get("item_id") or "")
        call_id = str(item.get("call_id") or "")
        if item_type == "function_call":
            name = str(item.get("name") or item.get("tool_name") or "tool")
            args = _stringify_responses_item_value(
                item.get("arguments")
                if "arguments" in item
                else item.get("args", item.get("input"))
            )
            if item_id:
                tool_names[item_id] = name
            if call_id:
                tool_names[call_id] = name
            events.append(
                {
                    "type": "tool_call",
                    "name": name,
                    "args": args,
                    "run_id": call_id or item_id or None,
                }
            )
            continue
        if item_type == "function_call_output":
            name = (
                tool_names.get(call_id)
                or tool_names.get(item_id)
                or str(item.get("name") or "tool")
            )
            output_text = _stringify_responses_item_value(
                item.get("output") if "output" in item else item.get("result", item.get("content"))
            )
            events.append(
                {
                    "type": "tool_result",
                    "name": name,
                    "output": output_text,
                    "run_id": call_id or item_id or None,
                }
            )
            continue
        if item_type in {"reasoning", "reasoning_summary", "reasoning_summary_text"}:
            text = _responses_output_item_text(item)
            if text:
                events.append({"type": "thinking", "delta": text})
    return events


def _model_options_disable_reasoning(model_options: Mapping[str, Any] | None) -> bool:
    normalized = normalize_model_options(model_options)
    reasoning = normalized.get("reasoning")
    if isinstance(reasoning, Mapping):
        effort = str(reasoning.get("effort") or "").strip().lower()
        if effort in {"none", "off", "disabled", "disable", "false", "0"}:
            return True
    max_reasoning_tokens = normalized.get("max_reasoning_tokens")
    if max_reasoning_tokens is not None:
        try:
            return int(max_reasoning_tokens) <= 0
        except (TypeError, ValueError):
            pass
    thinking = normalized.get("thinking")
    if isinstance(thinking, Mapping):
        raw_type = str(thinking.get("type") or thinking.get("status") or "").strip().lower()
        return raw_type in {"disabled", "disable", "off", "none", "false", "0"}
    if isinstance(thinking, bool):
        return not thinking
    raw_thinking = str(thinking or "").strip().lower()
    return raw_thinking in {"disabled", "disable", "off", "none", "false", "0"}


def _filter_responses_reasoning_output(output: Sequence[Any]) -> list[Any]:
    filtered: list[Any] = []
    for raw_item in output:
        if isinstance(raw_item, Mapping):
            item_type = str(raw_item.get("type") or "").strip()
            if item_type in {"reasoning", "reasoning_summary", "reasoning_summary_text"}:
                continue
        filtered.append(raw_item)
    return filtered


def _truncate_text(text: str | None, limit: int) -> str:
    raw = " ".join(str(text or "").strip().split())
    if len(raw) <= limit:
        return raw
    return f"{raw[: max(limit - 1, 0)].rstrip()}…"


async def _update_session_metadata_after_user_turn(
    *,
    service: Any,
    session: Session,
    user_input: str,
) -> None:
    text = _truncate_text(user_input, SESSION_SUMMARY_MAX_CHARS)
    if not text:
        return
    updates: dict[str, str] = {"last_prompt": text}
    if not (session.first_prompt or "").strip():
        updates["first_prompt"] = text
    if not (session.title or "").strip():
        updates["title"] = build_fallback_title(session.first_prompt or text)
        updates["title_source"] = "fallback_first_prompt"
    await service.update_session_metadata(session.id, **updates)


async def prime_session_metadata_for_user_turn(
    *,
    service: Any,
    session: Session,
    messages: Sequence[Mapping[str, Any]] | None = None,
    user_input: str | None = None,
) -> None:
    text = str(user_input or "").strip()
    if not text and messages:
        text, _display, _content, _parts, _attachments, _attachment_results = _latest_user_turn(
            messages
        )
    await _update_session_metadata_after_user_turn(
        service=service,
        session=session,
        user_input=text,
    )


async def _update_session_metadata_after_assistant_turn(
    *,
    service: Any,
    session_id: str,
    assistant_text: str,
    model: str | None,
) -> None:
    summary = _truncate_text(strip_reasoning_markup(assistant_text), SESSION_SUMMARY_MAX_CHARS)
    if summary:
        await service.update_session_metadata(session_id, summary=summary)

    session = await service.get_session(session_id)
    if not session:
        return
    if (session.title_source or "").strip() != "fallback_first_prompt":
        return
    first_prompt = str(session.first_prompt or "").strip()
    if not first_prompt or not summary:
        return

    next_title = build_heuristic_title(first_prompt=first_prompt, assistant_text=summary)
    next_title_source = (
        HEURISTIC_SESSION_TITLE_SOURCE
        if next_title and next_title != (session.title or "").strip()
        else ""
    )
    if next_title and next_title != (session.title or "").strip():
        await service.update_session_metadata(
            session_id,
            title=next_title,
            title_source=next_title_source,
        )

    title_client = resolve_session_title_client()
    title_model = resolve_session_title_model(model)
    if title_client.is_available and title_model:
        asyncio.create_task(
            _refine_session_title_in_background(
                service=service,
                session_id=session_id,
                first_prompt=first_prompt,
                assistant_text=summary,
                model=title_model,
            )
        )


async def _refine_session_title_in_background(
    *,
    service: Any,
    session_id: str,
    first_prompt: str,
    assistant_text: str,
    model: str,
) -> None:
    title_client = resolve_session_title_client()
    try:
        title, _usage = await title_client.generate_title(
            model=model,
            messages=build_session_title_messages(
                first_prompt=first_prompt,
                assistant_text=assistant_text,
            ),
            timeout_ms=DEFAULT_SESSION_TITLE_TIMEOUT_MS,
        )
    except Exception:
        logger.debug("failed to refine session title", exc_info=True)
        return

    if not title or is_low_quality_title(title, first_prompt=first_prompt):
        return
    session = await service.get_session(session_id)
    if not session:
        return
    if title == (session.title or "").strip():
        return
    await service.update_session_metadata(
        session_id,
        title=title,
        title_source="ai",
    )


def _resolve_model_metadata(
    model: Optional[str],
    *,
    model_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """统一收口模型上下文配置。

    当前阶段还没把远端 /v1/models 的完整 metadata 缓存接进 runtime，
    所以这里只用默认值 + model id。后续模型目录接口上线 richer metadata
    后，只需要把这层改成真正的 resolver，compaction 逻辑本身不用再动。
    """

    if isinstance(model_metadata, Mapping):
        resolved = dict(model_metadata)
        if model and not str(resolved.get("id") or "").strip():
            resolved["id"] = model
        return normalize_model_metadata(resolved)
    return normalize_model_metadata({"id": model or "agent"})


def _model_catalog_endpoint(api_base: str) -> str:
    base_url = str(api_base or "").rstrip("/")
    if not base_url:
        return ""
    if base_url.endswith("/v1"):
        return f"{base_url}/models"
    return f"{base_url}/v1/models"


async def _fetch_remote_model_catalog(api_base: str, api_key: str) -> list[dict[str, Any]]:
    url = _model_catalog_endpoint(api_base)
    if not url:
        return []

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(verify=False, timeout=10) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        payload = response.json()

    raw_models = payload if isinstance(payload, list) else list(payload.get("data", []))
    normalized: list[dict[str, Any]] = []
    for item in raw_models:
        if isinstance(item, Mapping) or isinstance(item, str):
            normalized.append(normalize_model_metadata(item))
    return normalized


async def _resolve_runtime_model_metadata(
    model: Optional[str],
    *,
    model_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    resolved = _resolve_model_metadata(model, model_metadata=model_metadata)
    if isinstance(model_metadata, Mapping) or not model:
        return resolved

    api_base = os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE") or ""
    if not api_base:
        return resolved

    api_key = os.getenv("OPENAI_API_KEY", "")
    cache_key = (api_base.rstrip("/"), api_key)
    now = time.monotonic()
    cached = _MODEL_CATALOG_CACHE.get(cache_key)
    models: list[dict[str, Any]]
    if cached and (now - cached[0]) < _MODEL_CATALOG_CACHE_TTL_SECONDS:
        models = cached[1]
    else:
        try:
            models = await _fetch_remote_model_catalog(api_base, api_key)
            _MODEL_CATALOG_CACHE[cache_key] = (now, models)
        except Exception as exc:
            logger.debug("Failed to fetch remote model metadata for %s: %s", model, exc)
            return resolved

    target = str(model).strip()
    for item in models:
        if str(item.get("id") or "").strip() == target:
            return item
    return resolved


def _normalized_conversation_messages(messages: Sequence[Dict[str, Any]]) -> list[dict[str, Any]]:
    """把不同入口的 message 形态收敛成统一内部格式。"""

    normalized_messages: list[dict[str, Any]] = []
    for message in list(messages or []):
        if isinstance(message, dict) and any(
            key in message
            for key in ("display_content", "attachments", "attachment_results", "parts")
        ):
            normalized_messages.append(
                {
                    "role": str(message.get("role") or "user"),
                    "content": str(message.get("content") or ""),
                    "display_content": str(
                        message.get("display_content") or message.get("content") or ""
                    ),
                    "parts": list(message.get("parts") or []),
                    "input_content": list(
                        message.get("input_content")
                        or canonical_input_content_from_parts(list(message.get("parts") or []))
                    ),
                    "attachments": list(message.get("attachments") or []),
                    "attachment_results": list(message.get("attachment_results") or []),
                }
            )
            continue
        normalized_messages.extend(normalize_kop_messages([message]))
    return normalized_messages


def _latest_user_turn(
    normalized_messages: Sequence[Dict[str, Any]],
) -> tuple[
    str,
    str,
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    latest_user_message = next(
        (message for message in reversed(normalized_messages) if message.get("role") == "user"),
        {},
    )
    user_input = str(latest_user_message.get("content") or "")
    user_display_input = str(latest_user_message.get("display_content") or user_input)
    input_content = list(latest_user_message.get("input_content") or [])
    user_parts = list(latest_user_message.get("parts") or [])
    attachments = list(latest_user_message.get("attachments") or [])
    attachment_results = list(latest_user_message.get("attachment_results") or [])
    return (
        user_input,
        user_display_input,
        input_content,
        user_parts,
        attachments,
        attachment_results,
    )


def _canonical_input_messages(
    normalized_messages: Sequence[Dict[str, Any]],
) -> list[dict[str, Any]]:
    input_messages: list[dict[str, Any]] = []
    for message in normalized_messages or []:
        role = str(message.get("role") or "user")
        content = list(message.get("input_content") or [])
        if not content:
            text = str(message.get("content") or "")
            if text:
                content = [{"type": "input_text", "text": text}]
        input_messages.append({"role": role, "content": content})
    return input_messages


def _parts_include_file(parts: Sequence[dict[str, Any]]) -> bool:
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        if part.get("inlineData") is not None or part.get("fileData") is not None:
            return True
    return False


def _latest_attachment_context_from_messages(
    normalized_messages: Sequence[Dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    attachments: list[dict[str, Any]] = []
    attachment_results: list[dict[str, Any]] = []
    for message in normalized_messages:
        if str(message.get("role") or "user") != "user":
            continue
        message_attachments = list(message.get("attachments") or [])
        message_attachment_results = list(message.get("attachment_results") or [])
        if message_attachments or message_attachment_results:
            attachments = message_attachments
            attachment_results = message_attachment_results
    return attachments, attachment_results


def _attachment_context_from_session(
    session: Session | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    state = getattr(session, "state", None) or {}
    payload = state.get(ATTACHMENT_CONTEXT_STATE_KEY)
    if not isinstance(payload, dict):
        return [], []
    return (
        [
            compact_attachment_for_session(item)
            for item in payload.get("attachments") or []
            if isinstance(item, dict)
        ],
        [
            compact_attachment_result_for_session(item)
            for item in payload.get("attachment_results") or []
            if isinstance(item, dict)
        ],
    )


def _build_attachment_context_state_delta(
    *,
    base_state_delta: dict[str, Any] | None,
    attachments: Sequence[dict[str, Any]],
    attachment_results: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    merged = dict(base_state_delta or {})
    if attachments or attachment_results:
        merged[ATTACHMENT_CONTEXT_STATE_KEY] = {
            "attachments": [
                compact_attachment_for_session(item)
                for item in attachments
                if isinstance(item, dict)
            ],
            "attachment_results": [
                compact_attachment_result_for_session(item)
                for item in attachment_results
                if isinstance(item, dict)
            ],
        }
    return merged


def _resolve_effective_attachment_context(
    *,
    normalized_messages: Sequence[Dict[str, Any]],
    session: Session | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    message_attachments, message_attachment_results = _latest_attachment_context_from_messages(
        normalized_messages
    )
    if message_attachments or message_attachment_results:
        return message_attachments, message_attachment_results

    session_attachments, session_attachment_results = _attachment_context_from_session(session)
    return session_attachments, session_attachment_results


def _transcript_event_type(event: SessionEvent) -> str:
    return canonical_event_type(
        event.event_type,
        author=event.author,
        role=str((event.content or {}).get("role") or ""),
    )


def _build_pending_user_event(
    *,
    session_id: str,
    invocation_id: str,
    user_input: str,
    user_display_input: str,
    attachments: Sequence[dict[str, Any]],
    attachment_results: Sequence[dict[str, Any]],
) -> SessionEvent:
    """构造一条未落库的用户事件，专供 compaction 预览使用。"""

    return SessionEvent.from_dict(
        {
            "id": f"preview-{uuid.uuid4()}",
            "author": "user",
            "event_type": "user_message",
            "invocationId": invocation_id,
            "content": {"role": "user", "parts": [{"text": user_display_input or user_input}]},
            "timestamp": int(time.time() * 1000),
            "metadata": {
                "agent_input": user_input,
                "attachments": [
                    compact_attachment_for_session(item) for item in attachments if item
                ],
                "attachment_results": [
                    compact_attachment_result_for_session(item)
                    for item in attachment_results
                    if item
                ],
            },
            "stateDelta": {},
        },
        session_id=session_id,
    )


def _user_event_content(
    *,
    user_input: str,
    user_display_input: str,
    input_content: Sequence[dict[str, Any]],
    user_parts: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    parts = list(input_content or [])
    if not parts:
        parts = canonical_input_content_from_parts(list(user_parts or []))
    if not parts:
        text = user_display_input or user_input
        parts = [{"text": text}] if text else []
    return {"role": "user", "parts": parts}


def _plan_compaction(
    events: Sequence[SessionEvent],
    *,
    model: Optional[str] = None,
    model_metadata: Mapping[str, Any] | None = None,
    pending_events: Sequence[SessionEvent] | None = None,
    force: bool = False,
    keep_tail_groups: int | None = None,
) -> CompactionPlan:
    """根据当前 transcript 计算是否需要做 checkpoint compaction。"""

    compacted_until = compacted_until_seq_id(list(events))
    transcript_events = [
        event
        for event in events
        if event.seq_id > compacted_until
        and _transcript_event_type(event) in TRANSCRIPT_EVENT_TYPES
        and _transcript_event_type(event) != "context_checkpoint"
    ]
    pending_transcript_events = [
        event
        for event in (pending_events or [])
        if _transcript_event_type(event) in TRANSCRIPT_EVENT_TYPES
        and _transcript_event_type(event) != "context_checkpoint"
    ]
    combined_events = [*transcript_events, *pending_transcript_events]
    groups = group_events_by_api_round(combined_events)
    pinned_group_indexes = sorted(find_pinned_group_indexes(groups))
    pinned_state = extract_pinned_state(groups)
    tail_groups = (
        keep_tail_groups
        if keep_tail_groups is not None
        else (PTL_RETRY_KEEP_TAIL_GROUPS if force else AUTOCOMPACT_KEEP_TAIL_GROUPS)
    )
    resolved_model_metadata = _resolve_model_metadata(model, model_metadata=model_metadata)
    auto_compact_threshold_tokens = get_auto_compact_threshold_tokens(resolved_model_metadata)
    auto_compact_threshold_percentage = get_auto_compact_threshold_percentage(
        resolved_model_metadata
    )
    total_chars = sum(len(extract_event_text(event)) for event in combined_events)
    total_estimated_tokens = sum(
        estimate_text_tokens(extract_event_text(event)) for event in combined_events
    )
    if not force and (
        len(groups) <= tail_groups or total_estimated_tokens <= auto_compact_threshold_tokens
    ):
        return CompactionPlan(
            should_compact=False,
            groups_to_compact=[],
            total_chars=total_chars,
            total_estimated_tokens=total_estimated_tokens,
            group_count=len(groups),
            tail_groups=tail_groups,
            auto_compact_threshold_tokens=auto_compact_threshold_tokens,
            auto_compact_threshold_percentage=auto_compact_threshold_percentage,
            pinned_group_indexes=pinned_group_indexes,
            pinned_state=pinned_state,
        )

    compactable_indexes = [
        index for index in range(len(groups)) if index not in pinned_group_indexes
    ]
    retained_tail_indexes = set(compactable_indexes[-tail_groups:]) if tail_groups > 0 else set()
    preserved_indexes = set(pinned_group_indexes) | retained_tail_indexes
    first_preserved_index = min(preserved_indexes) if preserved_indexes else len(groups)
    groups_to_compact = [
        group
        for index, group in enumerate(groups[:first_preserved_index])
        if index not in pinned_group_indexes
    ]
    if not groups_to_compact:
        return CompactionPlan(
            should_compact=False,
            groups_to_compact=[],
            total_chars=total_chars,
            total_estimated_tokens=total_estimated_tokens,
            group_count=len(groups),
            tail_groups=tail_groups,
            auto_compact_threshold_tokens=auto_compact_threshold_tokens,
            auto_compact_threshold_percentage=auto_compact_threshold_percentage,
            pinned_group_indexes=pinned_group_indexes,
            pinned_state=pinned_state,
        )

    compacted_until_seq_id_value = groups_to_compact[-1][-1].seq_id or None
    return CompactionPlan(
        should_compact=True,
        groups_to_compact=groups_to_compact,
        total_chars=total_chars,
        total_estimated_tokens=total_estimated_tokens,
        group_count=len(groups),
        tail_groups=tail_groups,
        auto_compact_threshold_tokens=auto_compact_threshold_tokens,
        auto_compact_threshold_percentage=auto_compact_threshold_percentage,
        compacted_until_seq_id=compacted_until_seq_id_value,
        pinned_group_indexes=pinned_group_indexes,
        pinned_state=pinned_state,
    )


async def preview_auto_compaction(
    *,
    agent_id: str,
    user_id: str,
    session_id: Optional[str],
    messages: Sequence[Dict[str, Any]],
    model: Optional[str] = None,
    model_metadata: Mapping[str, Any] | None = None,
    session_service_provider: Callable[[], Any] | None = None,
) -> CompactionPlan:
    """在真正写入 turn 之前预估是否会触发自动压缩。

    这个预览只用于给 UI 提前打一条“正在压缩上下文”的流式提示，不会修改会话。
    """

    if not session_id:
        return CompactionPlan(
            should_compact=False,
            groups_to_compact=[],
            total_chars=0,
            total_estimated_tokens=0,
            group_count=0,
            tail_groups=AUTOCOMPACT_KEEP_TAIL_GROUPS,
        )

    provider = session_service_provider or resolve_session_service
    service = provider()
    existing_session = await service.get_session(session_id)
    if not existing_session:
        return CompactionPlan(
            should_compact=False,
            groups_to_compact=[],
            total_chars=0,
            total_estimated_tokens=0,
            group_count=0,
            tail_groups=AUTOCOMPACT_KEEP_TAIL_GROUPS,
        )

    resolved_user_id = existing_session.user_id or user_id
    if existing_session.agent_id != agent_id or resolved_user_id != user_id:
        return CompactionPlan(
            should_compact=False,
            groups_to_compact=[],
            total_chars=0,
            total_estimated_tokens=0,
            group_count=0,
            tail_groups=AUTOCOMPACT_KEEP_TAIL_GROUPS,
        )

    normalized_messages = _normalized_conversation_messages(messages)
    resolved_model_metadata = await _resolve_runtime_model_metadata(
        model,
        model_metadata=model_metadata,
    )
    user_input, user_display_input, _, _, attachments, attachment_results = _latest_user_turn(
        normalized_messages
    )
    effective_attachments, effective_attachment_results = _resolve_effective_attachment_context(
        normalized_messages=normalized_messages,
        session=existing_session,
    )
    pending_event = _build_pending_user_event(
        session_id=session_id,
        invocation_id=f"preview-{uuid.uuid4()}",
        user_input=user_input,
        user_display_input=user_display_input or user_input,
        attachments=effective_attachments,
        attachment_results=effective_attachment_results,
    )
    events = await service.get_events(session_id)
    return _plan_compaction(
        events,
        model=model,
        model_metadata=resolved_model_metadata,
        pending_events=[pending_event],
    )


async def ensure_conversation_session(
    *,
    agent_id: str,
    user_id: str,
    session_id: Optional[str],
    session_service_provider: Callable[[], Any] | None = None,
) -> Session:
    """确保会话存在，并在显式 session_id 冲突时做 owner 校验。"""
    service = (session_service_provider or resolve_session_service)()
    if session_id:
        existing = await service.get_session(session_id)
        if existing:
            if existing.agent_id != agent_id or existing.user_id != user_id:
                raise HTTPException(
                    status_code=409,
                    detail="Session id belongs to a different agent or user",
                )
            return existing
        return await service.create_session(agent_id, user_id, session_id=session_id)
    return await service.create_session(agent_id, user_id)


async def append_conversation_event(
    *,
    session_id: str,
    author: str,
    role: str,
    text: str,
    invocation_id: Optional[str] = None,
    state_delta: Optional[dict[str, Any]] = None,
    metadata: Optional[dict[str, Any]] = None,
    event_type: Optional[str] = None,
    content: Optional[dict[str, Any]] = None,
    session_service_provider: Callable[[], Any] | None = None,
) -> SessionEvent:
    """统一的 canonical event 追加入口。

    所有协议层最终都应该落到这里，而不是各自直接 new `SessionEvent`，
    这样 event_type / invocation_id / metadata 的语义才不会再漂移。
    """
    service = (session_service_provider or resolve_session_service)()
    payload_content = content if content is not None else {"role": role, "parts": [{"text": text}]}
    return await service.append_event(
        session_id,
        SessionEvent.from_dict(
            {
                "id": str(uuid.uuid4()),
                "author": author,
                "event_type": event_type or canonical_event_type(None, author=author, role=role),
                "invocationId": invocation_id,
                "content": payload_content,
                "timestamp": int(time.time() * 1000),
                "stateDelta": state_delta or {},
                "metadata": metadata or {},
            },
            session_id=session_id,
        ),
    )


async def append_run_status_event(
    *,
    session_id: str,
    author: str,
    status: str,
    invocation_id: Optional[str] = None,
    detail: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    session_service_provider: Callable[[], Any] | None = None,
    run_mode: str = RUN_MODE_UNKNOWN,
    run_trigger: str = RUN_TRIGGER_UNKNOWN,
) -> SessionEvent:
    """记录运行态事件，供 UI/恢复逻辑区分 turn 生命周期。

    run_mode/run_trigger 是双维度字段（怎么跑/怎么开始），写入 metadata 与
    state_delta.active_run，供前端区分后台长任务、checkpoint 恢复、approval 续跑。
    """
    run_mode = validate_run_mode(run_mode)
    run_trigger = validate_run_trigger(run_trigger)
    service = (session_service_provider or resolve_session_service)()
    if invocation_id:
        try:
            for event in reversed(await service.get_events(session_id)):
                if event.event_type != "run_status" or event.invocation_id != invocation_id:
                    continue
                event_status = str(
                    (event.metadata or {}).get("status")
                    or (event.content or {}).get("status")
                    or ""
                )
                if event_status == status:
                    return event
        except Exception:
            pass
    content = {"status": status}
    if detail:
        content["detail"] = detail
    event_metadata = {
        "status": status,
        **({"detail": detail} if detail else {}),
        "run_mode": run_mode,
        "run_trigger": run_trigger,
        **dict(metadata or {}),
    }
    # state_delta.active_run：与 agentengine-server _append_run_status 对齐，
    # 让 session.state.active_run 反映当前 run 状态（postgres/local backend 会自动合并）。
    # server 侧 ActiveRunStatus 来源是 state_delta 而非扫事件，不写则 server 在 resume
    # 期间仍持旧 active_run 值。run_mode/run_trigger 同步写入，供 _serialize_session 读取。
    state_delta = {
        "active_run": {
            "invocation_id": invocation_id or "",
            "status": status,
            "run_mode": run_mode,
            "run_trigger": run_trigger,
        }
    }
    return await append_conversation_event(
        session_id=session_id,
        author=author,
        role="model",
        text="",
        invocation_id=invocation_id,
        event_type="run_status",
        content=content,
        metadata=event_metadata,
        state_delta=state_delta,
        session_service_provider=lambda: service,
    )


async def append_deferred_tools_event(
    *,
    session_id: str,
    author: str,
    deferred_tool_names: Sequence[str],
    invocation_id: Optional[str] = None,
    source_tool_name: str = "tool_search",
    session_service_provider: Callable[[], Any] | None = None,
    run_mode: str = RUN_MODE_UNKNOWN,
    run_trigger: str = RUN_TRIGGER_UNKNOWN,
) -> SessionEvent | None:
    names = _extract_deferred_tool_names({"deferred_tool_names": list(deferred_tool_names)})
    if not names:
        return None
    run_mode = validate_run_mode(run_mode)
    run_trigger = validate_run_trigger(run_trigger)
    return await append_conversation_event(
        session_id=session_id,
        author=author,
        role="model",
        text="",
        invocation_id=invocation_id,
        event_type="run_status",
        content={"status": "in_progress", "detail": "deferred_tools_selected"},
        metadata={
            "status": "in_progress",
            "detail": "deferred_tools_selected",
            "run_mode": run_mode,
            "run_trigger": run_trigger,
            "source_tool_name": source_tool_name,
            "deferred_tool_names": names,
        },
        state_delta={
            "active_run": {
                "invocation_id": invocation_id or "",
                "status": "in_progress",
                "run_mode": run_mode,
                "run_trigger": run_trigger,
            }
        },
        session_service_provider=session_service_provider,
    )


async def append_run_checkpoint_event(
    *,
    session_id: str,
    author: str,
    run_id: str,
    checkpoint_id: str,
    framework: str,
    framework_ref: Mapping[str, Any],
    phase: str = "",
    invocation_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    session_service_provider: Callable[[], Any] | None = None,
) -> SessionEvent:
    service = (session_service_provider or resolve_session_service)()
    for event in reversed(await service.get_events(session_id)):
        if event.event_type != "run_checkpoint":
            continue
        event_metadata = event.metadata or {}
        if (
            str(event_metadata.get("run_id") or "") == str(run_id)
            and str(event_metadata.get("checkpoint_id") or "") == str(checkpoint_id)
            and str(event_metadata.get("framework") or "") == str(framework)
        ):
            return event

    event_metadata = dict(metadata or {})
    framework_ref_dict = dict(framework_ref)
    langgraph_ref = framework_ref_dict.get("langgraph")
    next_node = ""
    if isinstance(langgraph_ref, Mapping):
        next_node = str(langgraph_ref.get("next_node") or "").strip()
    is_terminal = bool(event_metadata.get("is_terminal", False))
    if "is_terminal" not in event_metadata and next_node:
        is_terminal = False
    raw_is_resumable = event_metadata.get("is_resumable")
    is_resumable: bool | None
    if isinstance(raw_is_resumable, bool):
        is_resumable = raw_is_resumable
    else:
        is_resumable = None
    resume_status = str(event_metadata.get("resume_status") or "").strip()
    if not resume_status:
        if is_resumable is True:
            resume_status = "resumable"
        elif is_resumable is False:
            resume_status = "disabled"
        else:
            resume_status = "unknown"
    backend = str(event_metadata.get("backend") or "unknown").strip() or "unknown"
    scope = str(event_metadata.get("scope") or "unknown").strip() or "unknown"
    durable = bool(event_metadata.get("durable", False))
    event_metadata.update(
        {
            "run_id": str(run_id),
            "checkpoint_id": str(checkpoint_id),
            "framework": str(framework),
            "framework_ref": framework_ref_dict,
            "phase": str(phase or ""),
            "is_resumable": is_resumable,
            "is_terminal": is_terminal,
            "resume_status": resume_status,
            "resume_disabled_reason": str(event_metadata.get("resume_disabled_reason") or ""),
            "next_node": str(event_metadata.get("next_node") or next_node or ""),
            "stage_key": str(event_metadata.get("stage_key") or ""),
            "stage_name": str(
                event_metadata.get("stage_name")
                or event_metadata.get("stage")
                or event_metadata.get("title")
                or ""
            ),
            "stage_index": event_metadata.get("stage_index"),
            "total_stages": event_metadata.get("total_stages"),
            "backend": backend,
            "scope": scope,
            "durable": durable,
            "artifact_preview": event_metadata.get("artifact_preview") or {},
        }
    )
    return await append_conversation_event(
        session_id=session_id,
        author=author,
        role="model",
        text="checkpoint saved",
        invocation_id=invocation_id,
        event_type="run_checkpoint",
        content={
            "status": "checkpointed",
            "run_id": str(run_id),
            "checkpoint_id": str(checkpoint_id),
            "framework": str(framework),
            "is_resumable": is_resumable,
            "is_terminal": is_terminal,
            "resume_status": resume_status,
            "resume_disabled_reason": event_metadata["resume_disabled_reason"],
            "next_node": event_metadata["next_node"],
            "backend": backend,
            "scope": scope,
            "durable": durable,
            **({"phase": str(phase)} if phase else {}),
        },
        metadata=event_metadata,
        session_service_provider=lambda: service,
    )


async def append_run_resume_event(
    *,
    session_id: str,
    author: str,
    run_id: str,
    checkpoint_id: str,
    resume_attempt_id: str,
    framework: str,
    framework_ref: Mapping[str, Any],
    invocation_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    session_service_provider: Callable[[], Any] | None = None,
) -> SessionEvent:
    event_metadata = dict(metadata or {})
    event_metadata.update(
        {
            "run_id": str(run_id),
            "checkpoint_id": str(checkpoint_id),
            "resume_attempt_id": str(resume_attempt_id),
            "framework": str(framework),
            "framework_ref": dict(framework_ref),
        }
    )
    return await append_conversation_event(
        session_id=session_id,
        author=author,
        role="model",
        text="checkpoint resume requested",
        invocation_id=invocation_id,
        event_type="run_resume",
        content={
            "status": "resuming",
            "run_id": str(run_id),
            "checkpoint_id": str(checkpoint_id),
            "resume_attempt_id": str(resume_attempt_id),
            "framework": str(framework),
        },
        metadata=event_metadata,
        session_service_provider=session_service_provider,
    )


async def append_reasoning_event(
    *,
    session_id: str,
    author: str,
    text: str,
    invocation_id: Optional[str] = None,
    session_service_provider: Callable[[], Any] | None = None,
) -> SessionEvent | None:
    """Persist assistant reasoning so hosted UI refresh can replay thinking state."""
    reasoning_text = str(text or "")
    if not reasoning_text:
        return None
    return await append_conversation_event(
        session_id=session_id,
        author=author,
        role="model",
        text=reasoning_text,
        invocation_id=invocation_id,
        event_type="reasoning",
        metadata={"reasoning": reasoning_text},
        session_service_provider=session_service_provider,
    )


async def append_context_checkpoint_event(
    *,
    session_id: str,
    author: str,
    compacted_until_seq_id: int,
    summary_text: str = "",
    trigger: str = "auto",
    invocation_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    session_service_provider: Callable[[], Any] | None = None,
) -> SessionEvent:
    """追加 compaction boundary + checkpoint summary。

    这里遵循 Claude Code 的大方向：边界事件和摘要事件都保留在 transcript
    里，而不是把旧 history 原地覆盖掉。
    """
    event_metadata = dict(metadata or {})
    event_metadata["compacted_until_seq_id"] = compacted_until_seq_id
    event_metadata["trigger"] = trigger
    await append_conversation_event(
        session_id=session_id,
        author=author,
        role="model",
        text="",
        invocation_id=invocation_id,
        event_type="compaction_boundary",
        content={"status": "compacted", "compacted_until_seq_id": compacted_until_seq_id},
        metadata=event_metadata,
        session_service_provider=session_service_provider,
    )
    return await append_conversation_event(
        session_id=session_id,
        author=author,
        role="model",
        text=summary_text,
        invocation_id=invocation_id,
        event_type="context_checkpoint",
        metadata=event_metadata,
        session_service_provider=session_service_provider,
    )


async def compact_conversation_history(
    *,
    session_id: str,
    author: str,
    invocation_id: Optional[str] = None,
    model: Optional[str] = None,
    model_metadata: Mapping[str, Any] | None = None,
    force: bool = False,
    trigger: str = "auto",
    keep_tail_groups: Optional[int] = None,
    session_service_provider: Callable[[], Any] | None = None,
) -> SessionEvent | None:
    """把旧轮次折叠为 checkpoint。

    这是本地版的 compaction：先按 API round 分组，再保留尾部若干轮，把更早
    的部分压成 append-only summary 事件。force=True 时用于 PTL 恢复。
    """
    provider = session_service_provider or resolve_session_service
    service = provider()
    events = await service.get_events(session_id)
    plan = _plan_compaction(
        events,
        model=model,
        model_metadata=model_metadata,
        force=force,
        keep_tail_groups=keep_tail_groups,
    )
    if not plan.should_compact:
        return None

    previous_summary = ""
    latest_checkpoint = next(
        (
            event
            for event in reversed(events)
            if canonical_event_type(event.event_type) == "context_checkpoint"
        ),
        None,
    )
    if latest_checkpoint:
        previous_summary = extract_event_text(latest_checkpoint)

    compacted_until_seq_id_value = int(plan.compacted_until_seq_id or 0)
    resolved_model_metadata = _resolve_model_metadata(model, model_metadata=model_metadata)
    threshold_tokens = get_auto_compact_threshold_tokens(resolved_model_metadata)

    # P0.5 分层 compaction pipeline:L2 Snip + L3 Microcompact(零 LLM 成本确定性裁剪),
    # 只作用于送 summarizer 的 candidate 投影,绝不删 append-only transcript。
    # 详见 ksadk/conversations/compaction_pipeline.py。
    pipeline_result = run_pipeline(
        plan.groups_to_compact,
        threshold_tokens=threshold_tokens,
        pinned_state=plan.pinned_state,
        previous_summary=previous_summary,
    )
    candidate_groups = pipeline_result["candidate_groups"]

    summary_result = await summarize_compaction(
        groups_to_compact=candidate_groups,
        previous_summary=previous_summary,
        pinned_state=plan.pinned_state,
        model_metadata=resolved_model_metadata,
        model=model,
    )

    # L5 working set 恢复(保守版):只记 metadata,不读文件内容。
    working_set = build_working_set_metadata(pinned_state=plan.pinned_state)

    return await append_context_checkpoint_event(
        session_id=session_id,
        author=author,
        compacted_until_seq_id=compacted_until_seq_id_value,
        summary_text=summary_result.summary_text,
        trigger=trigger,
        invocation_id=invocation_id,
        metadata={
            "head_seq_id": plan.groups_to_compact[0][0].seq_id,
            "tail_seq_id": plan.groups_to_compact[-1][-1].seq_id,
            "invocation_ids": [
                event.invocation_id
                for group in plan.groups_to_compact
                for event in group
                if event.invocation_id
            ],
            "summary_strategy": summary_result.summary_strategy,
            "summary_version": summary_result.summary_version,
            "summary_model": summary_result.summary_model,
            "summary_usage": summary_result.summary_usage,
            "fallback_reason": summary_result.fallback_reason,
            # P0.5 pipeline 审计字段(原始 transcript 未改,这里只记投影统计)。
            "pipeline_stages": pipeline_result["pipeline_stages"],
            "tokens_before": pipeline_result["tokens_before"],
            "tokens_after": pipeline_result["tokens_after"],
            "snip_stats": {
                "removed_redundant_tool_results": pipeline_result[
                    "snip_stats"
                ].removed_redundant_tool_results,
                "snip_released_tokens": pipeline_result["snip_released_tokens"],
                "covered_seq_range": list(pipeline_result["snip_stats"].covered_seq_range or []),
            },
            "microcompact_stats": (
                {
                    "compacted_groups": pipeline_result["microcompact_stats"].compacted_groups,
                    "tokens_before": pipeline_result["microcompact_stats"].tokens_before,
                    "tokens_after": pipeline_result["microcompact_stats"].tokens_after,
                    "preserved_receipts": pipeline_result["microcompact_stats"].preserved_receipts,
                }
                if pipeline_result["microcompact_stats"] is not None
                else None
            ),
            "working_set": working_set,
        },
        session_service_provider=provider,
    )


async def build_run_input(
    *,
    agent_id: str,
    user_id: str,
    session_id: Optional[str],
    messages: Sequence[Dict[str, Any]],
    model: Optional[str] = None,
    model_metadata: Mapping[str, Any] | None = None,
    model_options: Mapping[str, Any] | None = None,
    state_delta: Optional[dict[str, Any]] = None,
    instructions: Optional[str] = None,
    request_metadata: Mapping[str, Any] | None = None,
    resume_input: Mapping[str, Any] | None = None,
    invocation_id: Optional[str] = None,
    governance_state: RuntimeGovernanceState | None = None,
    session_service_provider: Callable[[], Any] | None = None,
    run_mode: str = RUN_MODE_FOREGROUND,
) -> PreparedConversationTurn:
    """构建一次 turn 的标准运行输入，并在进入模型前做上下文投影/压缩。

    run_mode 由 caller 按 endpoint 语义传入（Background:true→background，普通→foreground）；
    run_trigger 由 resume_input 推导（new_run/checkpoint_resume/approval_resume）。
    """
    caller_run_mode = validate_run_mode(run_mode)
    caller_run_trigger = trigger_from_resume_input(resume_input)
    provider = session_service_provider or resolve_session_service
    service = provider()
    resolved_user_id = user_id
    if session_id:
        existing_session = await service.get_session(session_id)
        if existing_session and existing_session.user_id:
            resolved_user_id = existing_session.user_id

    session = await ensure_conversation_session(
        agent_id=agent_id,
        user_id=resolved_user_id,
        session_id=session_id,
        session_service_provider=provider,
    )
    resolved_session_id = session.id
    resolved_invocation_id = str(invocation_id or uuid.uuid4())
    resolved_model_metadata = await _resolve_runtime_model_metadata(
        model,
        model_metadata=model_metadata,
    )
    normalized_request_metadata = dict(request_metadata or {})
    policy_model = model or os.getenv("OPENAI_MODEL_NAME") or os.getenv("MODEL_NAME")
    normalized_model_options = {
        **normalize_model_options(model_options),
        **model_policy_options_for_model(policy_model),
    }
    normalized_instructions = str(instructions or "").strip()

    if resume_input is not None:
        if not session_id:
            raise ValueError("Responses resume input requires session_id")
        existing_events = await service.get_events(resolved_session_id)
        normalized_resume_input = dict(resume_input)
        if _is_checkpoint_resume_input(normalized_resume_input):
            normalized_resume_input = _normalize_checkpoint_resume_input(normalized_resume_input)
            await append_run_resume_event(
                session_id=resolved_session_id,
                author=agent_id,
                run_id=str(normalized_resume_input["run_id"]),
                checkpoint_id=str(normalized_resume_input["checkpoint_id"]),
                resume_attempt_id=str(normalized_resume_input["resume_attempt_id"]),
                framework=str(normalized_resume_input["framework"]),
                framework_ref=normalized_resume_input["framework_ref"],
                invocation_id=resolved_invocation_id,
                session_service_provider=provider,
            )
            # 补写 run_status(resuming)：让 ActiveRunStatus 在 resume 期间正确反映"恢复中"。
            # append_run_resume_event 写的是 run_resume 事件（status=resuming），而
            # _latest_session_run_status 只扫 run_status 事件 → 不补写则
            # resuming 不进 ActiveRunStatus。
            await append_run_status_event(
                session_id=resolved_session_id,
                author=agent_id,
                status="resuming",
                invocation_id=resolved_invocation_id,
                detail="checkpoint_resume",
                session_service_provider=provider,
                run_mode=caller_run_mode,
                run_trigger=RUN_TRIGGER_CHECKPOINT_RESUME,
            )
            history = build_history_from_events(await service.get_events(resolved_session_id))
            return PreparedConversationTurn(
                session_id=resolved_session_id,
                invocation_id=resolved_invocation_id,
                user_input="",
                user_display_input="",
                history=history,
                input_content=[],
                input_messages=[],
                user_parts=[],
                attachments=[],
                attachment_results=[],
                current_attachments=[],
                current_attachment_results=[],
                has_current_files=False,
                model_metadata=resolved_model_metadata,
                model_options=normalized_model_options,
                instructions=normalized_instructions,
                request_metadata={
                    **normalized_request_metadata,
                    **_agentengine_resume_metadata(normalized_resume_input),
                },
                resume_input=normalized_resume_input,
                run_mode=caller_run_mode,
                run_trigger=RUN_TRIGGER_CHECKPOINT_RESUME,
            )

        is_approval_resume = _is_approval_resume_input(normalized_resume_input)
        existing_tool_receipt_event = None
        if is_approval_resume and not _has_pending_approval(existing_events):
            replay_candidate = _normalize_approval_resume_input(
                normalized_resume_input,
                existing_events,
                include_resolved=True,
            )
            receipt_key = _tool_receipt_idempotency_key_for_resume(
                session_id=resolved_session_id,
                resume_input=replay_candidate,
            )
            if receipt_key:
                existing_tool_receipt_event = _find_tool_receipt_event_by_key(
                    existing_events,
                    receipt_key,
                )
            if existing_tool_receipt_event is not None:
                normalized_resume_input = replay_candidate
            else:
                raise ValueError("Responses resume input requires a pending approval_request")
        if is_approval_resume:
            normalized_resume_input = _normalize_approval_resume_input(
                normalized_resume_input,
                existing_events,
            )
            caller_run_mode = _approval_resume_run_mode(
                existing_events,
                normalized_resume_input,
                fallback=caller_run_mode,
            )
            if governance_state is not None:
                governance_state.consecutive_approval_denials = (
                    _consecutive_approval_denials_from_events(existing_events)
                )

        resume_text = _format_resume_response_text(normalized_resume_input)
        await append_conversation_event(
            session_id=resolved_session_id,
            author="user",
            role="user",
            text=resume_text,
            invocation_id=resolved_invocation_id,
            event_type="approval_response" if is_approval_resume else "tool_result",
            session_service_provider=provider,
            metadata={"resume_input": normalized_resume_input},
        )
        if is_approval_resume and governance_state is not None:
            decision = normalized_resume_input.get("approval")
            if not isinstance(decision, Mapping):
                decision = _approval_decision_from_resume(normalized_resume_input)
            _governance_record_approval_response(governance_state, decision)
        tool_resume_input = None
        if is_approval_resume:
            tool_resume_input = await _execute_approved_builtin_tool_resume(
                session_id=resolved_session_id,
                invocation_id=resolved_invocation_id,
                resume_input=normalized_resume_input,
                session_service_provider=provider,
            )
        effective_resume_input = tool_resume_input or normalized_resume_input
        history = build_history_from_events(await service.get_events(resolved_session_id))
        return PreparedConversationTurn(
            session_id=resolved_session_id,
            invocation_id=resolved_invocation_id,
            user_input=resume_text,
            user_display_input=resume_text,
            history=history,
            input_content=[],
            input_messages=[],
            user_parts=[],
            attachments=[],
            attachment_results=[],
            current_attachments=[],
            current_attachment_results=[],
            has_current_files=False,
            model_metadata=resolved_model_metadata,
            model_options=normalized_model_options,
            instructions=normalized_instructions,
            request_metadata=normalized_request_metadata,
            resume_input=effective_resume_input,
            run_mode=caller_run_mode,
            run_trigger=RUN_TRIGGER_APPROVAL_RESUME,
        )

    normalized_messages = _normalized_conversation_messages(messages)
    (
        user_input,
        user_display_input,
        input_content,
        user_parts,
        attachments,
        attachment_results,
    ) = _latest_user_turn(normalized_messages)
    input_messages = _canonical_input_messages(normalized_messages)
    effective_attachments, effective_attachment_results = _resolve_effective_attachment_context(
        normalized_messages=normalized_messages,
        session=session,
    )
    effective_state_delta = _build_attachment_context_state_delta(
        base_state_delta=state_delta,
        attachments=attachments,
        attachment_results=attachment_results,
    )
    event_metadata = {
        "agent_input": user_input,
        "attachments": [compact_attachment_for_session(item) for item in attachments if item],
        "attachment_results": [
            compact_attachment_result_for_session(item) for item in attachment_results if item
        ],
    }

    if normalized_instructions:
        event_metadata["instructions"] = normalized_instructions
    deferred_tool_names = _latest_deferred_tool_names(await service.get_events(resolved_session_id))
    if deferred_tool_names and "deferred_tool_names" not in normalized_request_metadata:
        normalized_request_metadata["deferred_tool_names"] = deferred_tool_names
    if normalized_request_metadata:
        event_metadata["request_metadata"] = normalized_request_metadata

    await append_conversation_event(
        session_id=resolved_session_id,
        author="user",
        role="user",
        text=user_display_input or user_input,
        invocation_id=resolved_invocation_id,
        event_type="user_message",
        state_delta=effective_state_delta,
        content=_user_event_content(
            user_input=user_input,
            user_display_input=user_display_input,
            input_content=input_content,
            user_parts=user_parts,
        ),
        session_service_provider=provider,
        metadata=event_metadata,
    )
    await _update_session_metadata_after_user_turn(
        service=service,
        session=session,
        user_input=user_input or user_display_input,
    )

    checkpoint = await _compact_conversation_history_with_governance(
        governance_state,
        session_id=resolved_session_id,
        author=agent_id,
        invocation_id=resolved_invocation_id,
        model=model,
        model_metadata=resolved_model_metadata,
        session_service_provider=provider,
    )
    history = build_history_from_events(await service.get_events(resolved_session_id))
    request_history = build_request_history(normalized_messages[:-1])
    # Gateway / Responses callers may send full prompt context while the
    # runtime-local session is empty or stale (for example after pod
    # replacement). Preserve that request context, but do not duplicate it when
    # local session events already contain the same prefix.
    history = _merge_request_history_with_session_history(request_history, history)

    return PreparedConversationTurn(
        session_id=resolved_session_id,
        invocation_id=resolved_invocation_id,
        user_input=user_input,
        user_display_input=user_display_input or user_input,
        history=history,
        input_content=input_content,
        input_messages=input_messages,
        user_parts=user_parts,
        attachments=effective_attachments,
        attachment_results=effective_attachment_results,
        current_attachments=attachments,
        current_attachment_results=attachment_results,
        has_current_files=bool(attachments or _parts_include_file(user_parts)),
        model_metadata=resolved_model_metadata,
        model_options=normalized_model_options,
        instructions=normalized_instructions,
        request_metadata=normalized_request_metadata,
        compaction_triggered=checkpoint is not None,
        compaction_trigger=(
            str((checkpoint.metadata or {}).get("trigger") or "auto") if checkpoint else None
        ),
        compacted_until_seq_id=(
            int((checkpoint.metadata or {}).get("compacted_until_seq_id") or 0)
            if checkpoint
            else None
        ),
        run_mode=caller_run_mode,
        run_trigger=caller_run_trigger,
    )


async def _refresh_history(
    prepared: PreparedConversationTurn, *, session_service_provider: Callable[[], Any] | None = None
) -> PreparedConversationTurn:
    """在 compaction 后刷新 prepared turn 的 history 视图。"""
    provider = session_service_provider or resolve_session_service
    service = provider()
    prepared.history = build_history_from_events(await service.get_events(prepared.session_id))
    return prepared


async def invoke_conversation_once(
    *,
    runner: Any,
    agent_id: str,
    user_id: str,
    session_id: Optional[str],
    messages: Sequence[Dict[str, Any]],
    model: Optional[str],
    prepare_runner: Callable[[Any, Optional[str]], None],
    model_metadata: Mapping[str, Any] | None = None,
    model_options: Mapping[str, Any] | None = None,
    state_delta: Optional[dict[str, Any]] = None,
    instructions: Optional[str] = None,
    request_metadata: Mapping[str, Any] | None = None,
    resume_input: Mapping[str, Any] | None = None,
    response_id: str | None = None,
    account_id: str | None = None,
    invocation_id: Optional[str] = None,
    session_service_provider: Callable[[], Any] | None = None,
    run_mode: str = RUN_MODE_FOREGROUND,
) -> tuple[str, dict[str, Any]]:
    """非流式 turn 编排入口。

    顺序固定为：写用户事件 -> 需要时 compact -> 写 run_status(in_progress)
    -> 调 runner -> PTL 时 compact/retry -> 写 assistant 结果 -> 写 completed。
    """
    provider = session_service_provider or resolve_session_service
    prepare_runner(runner, model)
    governance = _runtime_governance_from_env()
    _governance_record_turn_start(governance)
    # 入口算 run_trigger（不依赖 prepared，build_run_input 失败时也能用）
    entry_run_mode = validate_run_mode(run_mode)
    entry_run_trigger = trigger_from_resume_input(resume_input)
    try:
        prepared = await build_run_input(
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            messages=messages,
            model=model,
            model_metadata=model_metadata,
            model_options=model_options,
            state_delta=state_delta,
            instructions=instructions,
            request_metadata=request_metadata,
            resume_input=resume_input,
            invocation_id=invocation_id,
            governance_state=governance,
            session_service_provider=provider,
            run_mode=entry_run_mode,
        )
        # prepared 之后的 run_status 写入复用 prepared 的 mode/trigger
        run_mode = prepared.run_mode
        run_trigger = prepared.run_trigger
    except RuntimeCircuitOpen as exc:
        if session_id:
            await append_run_status_event(
                session_id=session_id,
                author=_runner_name(runner),
                status=_failed_status_for_resume(resume_input),
                invocation_id=invocation_id,
                detail=str(exc),
                metadata={"governance": exc.metadata},
                session_service_provider=provider,
                run_mode=entry_run_mode,
                run_trigger=entry_run_trigger,
            )
        raise
    _inject_runner_deferred_tools_for_request(runner, prepared)
    ambient_contexts = _build_runner_ambient_contexts(
        runner=runner,
        user_id=user_id,
        user_input=prepared.user_input,
    )
    runtime_context = PlatformInvocationContext(
        agent_id=agent_id,
        user_id=user_id,
        account_id=str(account_id or ""),
        session_id=prepared.session_id,
        history=list(prepared.history),
        input_content=list(prepared.input_content),
        input_messages=list(prepared.input_messages),
        input_parts=list(prepared.user_parts),
        attachments=list(prepared.attachments),
        attachment_results=list(prepared.attachment_results),
        current_attachments=list(prepared.current_attachments),
        current_attachment_results=list(prepared.current_attachment_results),
        has_current_files=prepared.has_current_files,
        runner_type=_runner_type_name(runner),
        model=model,
        model_options=prepared.model_options,
        kb_context=ambient_contexts.get("kb_context"),
        memory_context=ambient_contexts.get("memory_context"),
    )
    runner_name = _runner_name(runner)
    async with _conversation_span_scope(runner_name) as span:
        _set_conversation_span_attributes(
            span,
            agent_id=agent_id,
            user_id=user_id,
            session_id=prepared.session_id,
            invocation_id=prepared.invocation_id,
            runner_name=runner_name,
            model=model,
            response_id=response_id,
        )
        _set_conversation_input_attributes(span, prepared.user_input or prepared.user_display_input)
        trace_metadata = _span_feedback_metadata(span)
        await append_run_status_event(
            session_id=prepared.session_id,
            author=runner_name,
            status="in_progress",
            invocation_id=prepared.invocation_id,
            session_service_provider=provider,
            run_mode=run_mode,
            run_trigger=run_trigger,
        )

        result: dict[str, Any] | None = None
        last_invoke_error: Exception | None = None
        for attempt in range(2):
            try:
                last_invoke_error = None
                runtime_context.history = list(prepared.history)
                with platform_invocation_scope(runtime_context):
                    with tool_execution_scope(
                        session_id=prepared.session_id,
                        run_id=prepared.invocation_id,
                        invocation_id=prepared.invocation_id,
                    ):
                        result = await runner.invoke(
                            _build_runner_request_payload(
                                prepared=prepared,
                                model=model,
                                runtime_context=runtime_context,
                            )
                        )
                break
            except asyncio.CancelledError:
                await append_run_status_event(
                    session_id=prepared.session_id,
                    author=runner_name,
                    status="cancelled",
                    invocation_id=prepared.invocation_id,
                    detail="cancel_requested",
                    session_service_provider=provider,
                    run_mode=run_mode,
                    run_trigger=run_trigger,
                )
                raise
            except Exception as exc:
                if attempt == 0 and _is_prompt_too_long_error(exc):
                    try:
                        checkpoint = await _compact_conversation_history_with_governance(
                            governance,
                            session_id=prepared.session_id,
                            author=runner_name,
                            invocation_id=prepared.invocation_id,
                            model=model,
                            model_metadata=prepared.model_metadata,
                            force=True,
                            trigger="prompt_too_long",
                            keep_tail_groups=PTL_RETRY_KEEP_TAIL_GROUPS,
                            session_service_provider=provider,
                        )
                    except RuntimeCircuitOpen as circuit_exc:
                        await append_run_status_event(
                            session_id=prepared.session_id,
                            author=runner_name,
                            status=_failed_status_for_resume(resume_input),
                            invocation_id=prepared.invocation_id,
                            detail=str(circuit_exc),
                            metadata={"governance": circuit_exc.metadata},
                            session_service_provider=provider,
                            run_mode=run_mode,
                            run_trigger=run_trigger,
                        )
                        raise circuit_exc
                    if checkpoint:
                        prepared = await _refresh_history(
                            prepared, session_service_provider=provider
                        )
                        runtime_context.history = list(prepared.history)
                        continue
                fallback_model = fallback_model_for_exception(exc, current_model=model)
                if attempt == 0 and fallback_model:
                    model = fallback_model
                    runtime_context.model = fallback_model
                    runtime_context.model_options = {
                        **prepared.model_options,
                        **model_policy_options_for_model(fallback_model),
                    }
                    prepare_runner(runner, fallback_model)
                    await append_run_status_event(
                        session_id=prepared.session_id,
                        author=runner_name,
                        status="in_progress",
                        invocation_id=prepared.invocation_id,
                        detail=f"fallback_model:{fallback_model}",
                        session_service_provider=provider,
                        run_mode=run_mode,
                        run_trigger=run_trigger,
                    )
                    continue
                last_invoke_error = exc
                await append_run_status_event(
                    session_id=prepared.session_id,
                    author=runner_name,
                    status=_failed_status_for_resume(resume_input),
                    invocation_id=prepared.invocation_id,
                    detail=str(exc),
                    session_service_provider=provider,
                    run_mode=run_mode,
                    run_trigger=run_trigger,
                )
                break

        if last_invoke_error is not None:
            raise last_invoke_error
        result = result or {}
        output_text = strip_reasoning_markup(str(result.get("output", "")))
        result_usage = _normalize_usage_payload(result.get("usage"))
        result_last_usage = _normalize_usage_payload(
            (result.get("metadata") or {}).get("last_usage")
        ) or (result_usage if result_usage else {})
        _set_conversation_output_attributes(span, output_text)
        _set_conversation_usage_attributes(span, result_usage)
        result_agentengine_metadata = _extract_agentengine_metadata(result)
        assistant_metadata: dict[str, Any] = {
            **trace_metadata,
            **_merge_agentengine_metadata(prepared.request_metadata, result_agentengine_metadata),
        }
        if prepared.request_metadata:
            request_metadata_without_agentengine = {
                key: value
                for key, value in prepared.request_metadata.items()
                if key != "agentengine"
            }
            if request_metadata_without_agentengine:
                assistant_metadata["request_metadata"] = request_metadata_without_agentengine
        if result_usage:
            assistant_metadata["usage"] = result_usage
        if result_last_usage:
            assistant_metadata["last_usage"] = result_last_usage
        if response_id:
            assistant_metadata["response_id"] = response_id
        checkpoint_args = _checkpoint_event_args_from_agentengine_metadata(
            assistant_metadata.get("agentengine")
            if isinstance(assistant_metadata, Mapping)
            else None,
            fallback_run_id=prepared.invocation_id,
        )
        if checkpoint_args:
            await append_run_checkpoint_event(
                session_id=prepared.session_id,
                author=runner_name,
                run_id=checkpoint_args["run_id"],
                checkpoint_id=checkpoint_args["checkpoint_id"],
                framework=checkpoint_args["framework"],
                framework_ref=checkpoint_args["framework_ref"],
                phase=checkpoint_args.get("phase") or "completed",
                invocation_id=prepared.invocation_id,
                metadata=checkpoint_args.get("metadata"),
                session_service_provider=provider,
            )
        await append_conversation_event(
            session_id=prepared.session_id,
            author=runner_name,
            role="model",
            text=output_text,
            invocation_id=prepared.invocation_id,
            event_type="assistant_message",
            metadata=assistant_metadata or None,
            session_service_provider=provider,
        )
        await _update_session_metadata_after_assistant_turn(
            service=provider(),
            session_id=prepared.session_id,
            assistant_text=output_text,
            model=model,
        )
        await _auto_save_ltm_turn(
            agent_id=agent_id,
            user_id=user_id,
            prepared=prepared,
            output_text=output_text,
            runner_type=runtime_context.runner_type,
            model=model,
        )
        await append_run_status_event(
            session_id=prepared.session_id,
            author=runner_name,
            status="completed",
            invocation_id=prepared.invocation_id,
            session_service_provider=provider,
            run_mode=run_mode,
            run_trigger=run_trigger,
        )
        result_payload = {
            "output_text": output_text,
            "model": model,
            "metadata": {
                **trace_metadata,
                **{
                    key: value
                    for key, value in prepared.request_metadata.items()
                    if key != "agentengine"
                },
                **_merge_agentengine_metadata(
                    prepared.request_metadata, result_agentengine_metadata
                ),
            },
        }
        if result_usage:
            result_payload["usage"] = result_usage
            result_payload["metadata"]["usage"] = result_usage
        if result_last_usage:
            result_payload["metadata"]["last_usage"] = result_last_usage
        if response_id:
            result_payload["response_id"] = response_id
        return prepared.session_id, result_payload


def _response_sse(event: str, data: Mapping[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(dict(data), ensure_ascii=False)}\n\n"


async def _iter_conversation_turn_events(
    *,
    runner: Any,
    agent_id: str,
    user_id: str,
    session_id: Optional[str],
    messages: Sequence[Dict[str, Any]],
    model: Optional[str],
    prepare_runner: Callable[[Any, Optional[str]], None],
    model_metadata: Mapping[str, Any] | None = None,
    model_options: Mapping[str, Any] | None = None,
    state_delta: Optional[dict[str, Any]] = None,
    instructions: Optional[str] = None,
    request_metadata: Mapping[str, Any] | None = None,
    resume_input: Mapping[str, Any] | None = None,
    response_id: str | None = None,
    account_id: str | None = None,
    invocation_id: Optional[str] = None,
    session_service_provider: Callable[[], Any] | None = None,
    run_mode: str = RUN_MODE_FOREGROUND,
) -> AsyncIterator[dict[str, Any]]:
    """Internal semantic event stream shared by protocol serializers."""
    provider = session_service_provider or resolve_session_service
    prepare_runner(runner, model)
    governance = _runtime_governance_from_env()
    _governance_record_turn_start(governance)
    entry_run_mode = validate_run_mode(run_mode)
    entry_run_trigger = trigger_from_resume_input(resume_input)
    if resume_input is None:
        compaction_preview = await preview_auto_compaction(
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            messages=messages,
            model=model,
            model_metadata=model_metadata,
            session_service_provider=provider,
        )
    else:
        compaction_preview = CompactionPlan(
            should_compact=False,
            groups_to_compact=[],
            total_chars=0,
            total_estimated_tokens=0,
            group_count=0,
            tail_groups=0,
        )
    if compaction_preview.should_compact:
        yield {
            "type": "compaction",
            "phase": "start",
            "trigger": "auto",
            "total_chars": compaction_preview.total_chars,
            "total_estimated_tokens": compaction_preview.total_estimated_tokens,
            "group_count": compaction_preview.group_count,
            "threshold_percentage": compaction_preview.auto_compact_threshold_percentage,
        }
    try:
        prepared = await build_run_input(
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            messages=messages,
            model=model,
            model_metadata=model_metadata,
            model_options=model_options,
            state_delta=state_delta,
            instructions=instructions,
            request_metadata=request_metadata,
            resume_input=resume_input,
            invocation_id=invocation_id,
            governance_state=governance,
            session_service_provider=provider,
            run_mode=entry_run_mode,
        )
        # prepared 之后的 run_status 写入复用 prepared 的 mode/trigger
        run_mode = prepared.run_mode
        run_trigger = prepared.run_trigger
    except RuntimeCircuitOpen as exc:
        if session_id:
            await append_run_status_event(
                session_id=session_id,
                author=_runner_name(runner),
                status=_failed_status_for_resume(resume_input),
                invocation_id=invocation_id,
                detail=str(exc),
                metadata={"governance": exc.metadata},
                session_service_provider=provider,
                run_mode=entry_run_mode,
                run_trigger=entry_run_trigger,
            )
        yield {"type": "error", "message": str(exc) or "Agent 运行失败"}
        return
    _inject_runner_deferred_tools_for_request(runner, prepared)
    ambient_contexts = _build_runner_ambient_contexts(
        runner=runner,
        user_id=user_id,
        user_input=prepared.user_input,
    )
    runtime_context = PlatformInvocationContext(
        agent_id=agent_id,
        user_id=user_id,
        account_id=str(account_id or ""),
        session_id=prepared.session_id,
        history=list(prepared.history),
        input_content=list(prepared.input_content),
        input_messages=list(prepared.input_messages),
        input_parts=list(prepared.user_parts),
        attachments=list(prepared.attachments),
        attachment_results=list(prepared.attachment_results),
        current_attachments=list(prepared.current_attachments),
        current_attachment_results=list(prepared.current_attachment_results),
        has_current_files=prepared.has_current_files,
        runner_type=_runner_type_name(runner),
        model=model,
        model_options=prepared.model_options,
        kb_context=ambient_contexts.get("kb_context"),
        memory_context=ambient_contexts.get("memory_context"),
    )
    if prepared.compaction_triggered:
        yield {
            "type": "compaction",
            "phase": "done",
            "trigger": str(prepared.compaction_trigger or "auto"),
            "compacted_until_seq_id": prepared.compacted_until_seq_id,
            "total_chars": (
                compaction_preview.total_chars if compaction_preview.should_compact else None
            ),
            "total_estimated_tokens": (
                compaction_preview.total_estimated_tokens
                if compaction_preview.should_compact
                else None
            ),
            "group_count": (
                compaction_preview.group_count if compaction_preview.should_compact else None
            ),
            "threshold_percentage": (
                compaction_preview.auto_compact_threshold_percentage
                if compaction_preview.should_compact
                else None
            ),
        }
    runner_name = _runner_name(runner)
    tracer = _get_conversation_tracer()
    span = tracer.start_span(runner_name) if tracer else None
    span_ended = False

    def _finish_span() -> None:
        nonlocal span_ended
        if span is None or span_ended:
            return
        span_ended = True
        try:
            span.end()
        except Exception:
            pass

    try:
        _set_conversation_span_attributes(
            span,
            agent_id=agent_id,
            user_id=user_id,
            session_id=prepared.session_id,
            invocation_id=prepared.invocation_id,
            runner_name=runner_name,
            model=model,
            response_id=response_id,
        )
        _set_conversation_input_attributes(span, prepared.user_input or prepared.user_display_input)
        trace_metadata = _span_feedback_metadata(span)
        yield {
            "type": "started",
            "session_id": prepared.session_id,
            "metadata": {**trace_metadata, **dict(request_metadata or {})},
        }
        await append_run_status_event(
            session_id=prepared.session_id,
            author=runner_name,
            status="in_progress",
            invocation_id=prepared.invocation_id,
            session_service_provider=provider,
            run_mode=run_mode,
            run_trigger=run_trigger,
        )

        accumulated_text = ""
        accumulated_reasoning_parts: list[str] = []
        emitted_anything = False
        emitted_response_artifacts = False
        saw_final_chunk = False
        responses_output: list[Any] = []
        responses_response_id: str | None = response_id
        runner_agentengine_metadata: dict[str, Any] = {}
        stream_usage: dict[str, Any] = {}
        stream_last_usage: dict[str, Any] = {}
        reasoning_disabled = _model_options_disable_reasoning(prepared.model_options)

        async def _persist_accumulated_reasoning() -> None:
            if not accumulated_reasoning_parts:
                return
            reasoning = "".join(accumulated_reasoning_parts)
            accumulated_reasoning_parts.clear()
            await append_reasoning_event(
                session_id=prepared.session_id,
                author=runner_name,
                text=reasoning,
                invocation_id=prepared.invocation_id,
                session_service_provider=provider,
            )

        for attempt in range(2):
            try:
                runtime_context.history = list(prepared.history)
                with platform_invocation_scope(runtime_context):
                    with tool_execution_scope(
                        session_id=prepared.session_id,
                        run_id=prepared.invocation_id,
                        invocation_id=prepared.invocation_id,
                    ):
                        stream = runner.stream(
                            _build_runner_request_payload(
                                prepared=prepared,
                                model=model,
                                runtime_context=runtime_context,
                            )
                        )
                        while True:
                            try:
                                with _span_current_context(span):
                                    chunk = await anext(stream)
                            except StopAsyncIteration:
                                break
                            chunk_type = chunk.get("type")
                            if chunk_type == "checkpoint":
                                emitted_anything = True
                                chunk_agentengine_metadata = _extract_agentengine_metadata(chunk)
                                if chunk_agentengine_metadata:
                                    runner_agentengine_metadata.update(chunk_agentengine_metadata)
                                    resume_run_id = ""
                                    if prepared.resume_input and _is_checkpoint_resume_input(
                                        prepared.resume_input
                                    ):
                                        resume_run_id = str(
                                            prepared.resume_input.get("run_id") or ""
                                        ).strip()
                                    checkpoint_args = (
                                        _checkpoint_event_args_from_agentengine_metadata(
                                            runner_agentengine_metadata.get("agentengine"),
                                            fallback_run_id=resume_run_id or prepared.invocation_id,
                                        )
                                    )
                                    if checkpoint_args:
                                        await append_run_checkpoint_event(
                                            session_id=prepared.session_id,
                                            author=runner_name,
                                            run_id=checkpoint_args["run_id"],
                                            checkpoint_id=checkpoint_args["checkpoint_id"],
                                            framework=checkpoint_args["framework"],
                                            framework_ref=checkpoint_args["framework_ref"],
                                            phase=checkpoint_args.get("phase") or "stream",
                                            invocation_id=prepared.invocation_id,
                                            metadata=checkpoint_args.get("metadata"),
                                            session_service_provider=provider,
                                        )
                                continue
                            if chunk_type == "responses_output":
                                if isinstance(chunk.get("usage"), Mapping):
                                    stream_usage = _normalize_usage_payload(chunk.get("usage"))
                                chunk_last = (chunk.get("metadata") or {}).get("last_usage")
                                if isinstance(chunk_last, Mapping):
                                    stream_last_usage = _normalize_usage_payload(chunk_last) or stream_usage
                                raw_output = chunk.get("output")
                                responses_output = (
                                    raw_output if isinstance(raw_output, list) else []
                                )
                                if reasoning_disabled:
                                    responses_output = _filter_responses_reasoning_output(
                                        responses_output
                                    )
                                raw_response_id = chunk.get("response_id")
                                responses_response_id = (
                                    str(raw_response_id)
                                    if raw_response_id
                                    else responses_response_id
                                )
                                if responses_output and not emitted_response_artifacts:
                                    for semantic_event in _semantic_events_from_responses_output(
                                        responses_output
                                    ):
                                        if semantic_event.get("type") == "thinking":
                                            accumulated_reasoning_parts.append(
                                                str(semantic_event.get("delta") or "")
                                            )
                                        emitted_anything = True
                                        yield semantic_event
                                continue
                            if chunk_type == "thinking":
                                if reasoning_disabled:
                                    continue
                                delta = str(chunk.get("delta", ""))
                                if delta:
                                    accumulated_reasoning_parts.append(delta)
                                    emitted_anything = True
                                    emitted_response_artifacts = True
                                    yield {"type": "thinking", "delta": delta}
                                continue
                            if chunk_type == "text":
                                delta = str(chunk.get("delta", ""))
                                if delta:
                                    accumulated_text += delta
                                    emitted_anything = True
                                    yield {"type": "text", "delta": delta}
                                continue
                            if chunk_type == "tool_call":
                                _governance_record_tool_call(governance)
                                emitted_response_artifacts = True
                                tool_args = chunk.get("tool_args", {})
                                if not isinstance(tool_args, Mapping):
                                    tool_args = {}
                                await append_conversation_event(
                                    session_id=prepared.session_id,
                                    author=runner_name,
                                    role="model",
                                    text=str(chunk.get("tool_name") or "tool"),
                                    invocation_id=prepared.invocation_id,
                                    event_type="tool_call",
                                    metadata={
                                        "tool_name": chunk.get("tool_name"),
                                        "tool_args": dict(tool_args),
                                        "run_id": chunk.get("run_id"),
                                        "stage": chunk.get("stage") or tool_args.get("stage"),
                                        "event_kind": chunk.get("event_kind"),
                                        "display_title": chunk.get("display_title"),
                                        "display_summary": chunk.get("display_summary"),
                                    },
                                    session_service_provider=provider,
                                )
                                emitted_anything = True
                                await _persist_accumulated_reasoning()
                                yield {
                                    "type": "tool_call",
                                    "name": chunk.get("tool_name"),
                                    "args": dict(tool_args),
                                    "run_id": chunk.get("run_id"),
                                    "stage": chunk.get("stage") or tool_args.get("stage"),
                                    "event_kind": chunk.get("event_kind"),
                                    "display_title": chunk.get("display_title"),
                                    "display_summary": chunk.get("display_summary"),
                                }
                                continue
                            if chunk_type in {"stage_tool_call", "stage_tool_result"}:
                                emitted_response_artifacts = True
                                tool_name = str(
                                    chunk.get("tool_name") or chunk.get("name") or "tool"
                                )
                                tool_args = chunk.get("tool_args", chunk.get("args", {}))
                                if not isinstance(tool_args, Mapping):
                                    tool_args = {}
                                tool_output = chunk.get("tool_output", chunk.get("output", ""))
                                event_kind = str(chunk.get("event_kind") or "")
                                display_title = str(chunk.get("display_title") or tool_name)
                                display_summary = str(
                                    chunk.get("display_summary") or chunk.get("text") or ""
                                )
                                await append_conversation_event(
                                    session_id=prepared.session_id,
                                    author=runner_name,
                                    role="model" if chunk_type == "stage_tool_call" else "user",
                                    text=display_summary or tool_name,
                                    invocation_id=prepared.invocation_id,
                                    event_type=chunk_type,
                                    metadata={
                                        "tool_name": tool_name,
                                        "tool_args": dict(tool_args),
                                        "tool_output": tool_output,
                                        "run_id": chunk.get("run_id"),
                                        "stage": chunk.get("stage") or tool_args.get("stage"),
                                        "event_kind": event_kind,
                                        "display_title": display_title,
                                        "display_summary": display_summary,
                                    },
                                    session_service_provider=provider,
                                )
                                emitted_anything = True
                                yield {
                                    "type": chunk_type,
                                    "name": tool_name,
                                    "args": dict(tool_args),
                                    "output": tool_output,
                                    "run_id": chunk.get("run_id"),
                                    "stage": chunk.get("stage") or tool_args.get("stage"),
                                    "event_kind": event_kind,
                                    "display_title": display_title,
                                    "display_summary": display_summary,
                                }
                                continue
                            if chunk_type == "tool_result":
                                emitted_response_artifacts = True
                                tool_name = str(chunk.get("tool_name") or "tool")
                                tool_args = chunk.get("tool_args", {})
                                if not isinstance(tool_args, Mapping):
                                    tool_args = {}
                                tool_run_id = str(chunk.get("run_id") or prepared.invocation_id)
                                checkpoint_metadata = _latest_checkpoint_metadata_for_run(
                                    await provider().get_events(prepared.session_id),
                                    tool_run_id,
                                )
                                approval_interrupt_info = approval_interrupt_info_from_result(
                                    chunk.get("tool_output", ""),
                                    fallback_tool_name=tool_name,
                                    tool_args=tool_args,
                                    run_id=tool_run_id,
                                )
                                if approval_interrupt_info:
                                    await append_conversation_event(
                                        session_id=prepared.session_id,
                                        author=runner_name,
                                        role="model",
                                        text="approval requested",
                                        invocation_id=prepared.invocation_id,
                                        event_type="approval_request",
                                        metadata={"interrupt_info": approval_interrupt_info},
                                        session_service_provider=provider,
                                    )
                                    await append_run_status_event(
                                        session_id=prepared.session_id,
                                        author=runner_name,
                                        status="interrupted",
                                        invocation_id=prepared.invocation_id,
                                        detail="approval_required",
                                        session_service_provider=provider,
                                        run_mode=run_mode,
                                        run_trigger=run_trigger,
                                    )
                                    emitted_anything = True
                                    await _persist_accumulated_reasoning()
                                    yield {
                                        "type": "interrupt",
                                        "interrupt_info": approval_interrupt_info,
                                        "session_id": prepared.session_id,
                                        "metadata": {**trace_metadata, **prepared.request_metadata},
                                    }
                                    return
                                await append_conversation_event(
                                    session_id=prepared.session_id,
                                    author=runner_name,
                                    role="user",
                                    text=str(chunk.get("tool_output", "")),
                                    invocation_id=prepared.invocation_id,
                                    event_type="tool_result",
                                    metadata={
                                        "tool_name": tool_name,
                                        "tool_output": chunk.get("tool_output", ""),
                                        "run_id": tool_run_id,
                                        "observability": _tool_observability_metadata(
                                            tool_name, chunk.get("tool_output", "")
                                        ),
                                        "tool_receipt": _tool_receipt_metadata(
                                            session_id=prepared.session_id,
                                            run_id=tool_run_id,
                                            tool_call_id=tool_run_id,
                                            tool_name=tool_name,
                                            tool_args=tool_args,
                                            checkpoint_id=checkpoint_metadata.get("checkpoint_id"),
                                            framework=checkpoint_metadata.get("framework"),
                                            framework_ref=checkpoint_metadata.get("framework_ref"),
                                            status=(
                                                "failed"
                                                if isinstance(chunk.get("tool_output"), Mapping)
                                                and chunk.get("tool_output", {}).get("ok") is False
                                                else "completed"
                                            ),
                                        ),
                                    },
                                    session_service_provider=provider,
                                )
                                deferred_tool_names = _extract_deferred_tool_names(
                                    chunk.get("tool_output", "")
                                )
                                if tool_name == "tool_search" and deferred_tool_names:
                                    await append_deferred_tools_event(
                                        session_id=prepared.session_id,
                                        author=runner_name,
                                        deferred_tool_names=deferred_tool_names,
                                        invocation_id=prepared.invocation_id,
                                        session_service_provider=provider,
                                        run_mode=run_mode,
                                        run_trigger=run_trigger,
                                    )
                                _governance_record_tool_result(
                                    governance, chunk.get("tool_output", "")
                                )
                                emitted_anything = True
                                await _persist_accumulated_reasoning()
                                yield {
                                    "type": "tool_result",
                                    "name": chunk.get("tool_name"),
                                    "output": chunk.get("tool_output", ""),
                                    "run_id": chunk.get("run_id"),
                                }
                                continue
                            if chunk_type == "interrupt":
                                interrupt_info = chunk.get("interrupt_info")
                                await append_conversation_event(
                                    session_id=prepared.session_id,
                                    author=runner_name,
                                    role="model",
                                    text="approval requested",
                                    invocation_id=prepared.invocation_id,
                                    event_type="approval_request",
                                    metadata={"interrupt_info": interrupt_info},
                                    session_service_provider=provider,
                                )
                                await append_run_status_event(
                                    session_id=prepared.session_id,
                                    author=runner_name,
                                    status="interrupted",
                                    invocation_id=prepared.invocation_id,
                                    detail="approval_required",
                                    session_service_provider=provider,
                                    run_mode=run_mode,
                                    run_trigger=run_trigger,
                                )
                                emitted_anything = True
                                yield {
                                    "type": "interrupt",
                                    "interrupt_info": interrupt_info,
                                    "session_id": prepared.session_id,
                                    "metadata": {**trace_metadata, **prepared.request_metadata},
                                }
                                return
                            if chunk_type == "final":
                                saw_final_chunk = True
                                final_text = str(chunk.get("output", ""))
                                if final_text:
                                    accumulated_text = final_text
                                if isinstance(chunk.get("usage"), Mapping):
                                    stream_usage = _normalize_usage_payload(chunk.get("usage"))
                                chunk_last = (chunk.get("metadata") or {}).get("last_usage")
                                if isinstance(chunk_last, Mapping):
                                    stream_last_usage = _normalize_usage_payload(chunk_last) or stream_usage
                break
            except asyncio.CancelledError:
                await _persist_accumulated_reasoning()
                await append_run_status_event(
                    session_id=prepared.session_id,
                    author=runner_name,
                    status="cancelled",
                    invocation_id=prepared.invocation_id,
                    detail="cancel_requested",
                    session_service_provider=provider,
                    run_mode=run_mode,
                    run_trigger=run_trigger,
                )
                yield {
                    "type": "cancelled",
                    "session_id": prepared.session_id,
                    "metadata": {**trace_metadata, **prepared.request_metadata},
                }
                return
            except Exception as exc:
                if attempt == 0 and not emitted_anything and _is_prompt_too_long_error(exc):
                    yield {"type": "compaction", "phase": "start", "trigger": "prompt_too_long"}
                    try:
                        checkpoint = await _compact_conversation_history_with_governance(
                            governance,
                            session_id=prepared.session_id,
                            author=runner_name,
                            invocation_id=prepared.invocation_id,
                            model=model,
                            model_metadata=prepared.model_metadata,
                            force=True,
                            trigger="prompt_too_long",
                            keep_tail_groups=PTL_RETRY_KEEP_TAIL_GROUPS,
                            session_service_provider=provider,
                        )
                    except RuntimeCircuitOpen as circuit_exc:
                        await append_run_status_event(
                            session_id=prepared.session_id,
                            author=runner_name,
                            status=_failed_status_for_resume(resume_input),
                            invocation_id=prepared.invocation_id,
                            detail=str(circuit_exc),
                            metadata={"governance": circuit_exc.metadata},
                            session_service_provider=provider,
                            run_mode=run_mode,
                            run_trigger=run_trigger,
                        )
                        await _persist_accumulated_reasoning()
                        yield {"type": "error", "message": str(circuit_exc) or "Agent 运行失败"}
                        return
                    if checkpoint:
                        yield {
                            "type": "compaction",
                            "phase": "done",
                            "trigger": "prompt_too_long",
                            "compacted_until_seq_id": int(
                                (checkpoint.metadata or {}).get("compacted_until_seq_id") or 0
                            )
                            or None,
                        }
                        prepared = await _refresh_history(
                            prepared, session_service_provider=provider
                        )
                        runtime_context.history = list(prepared.history)
                        continue
                if attempt == 0 and not emitted_anything:
                    fallback_model = fallback_model_for_exception(exc, current_model=model)
                    if fallback_model:
                        model = fallback_model
                        prepare_runner(runner, fallback_model)
                        prepared.model_options = {
                            **prepared.model_options,
                            **model_policy_options_for_model(fallback_model),
                        }
                        runtime_context.model = fallback_model
                        runtime_context.model_options = prepared.model_options
                        _set_span_attribute(span, "ksadk.model.fallback", fallback_model)
                        await append_run_status_event(
                            session_id=prepared.session_id,
                            author=runner_name,
                            status="in_progress",
                            invocation_id=prepared.invocation_id,
                            detail=f"fallback_model:{fallback_model}",
                            session_service_provider=provider,
                            run_mode=run_mode,
                            run_trigger=run_trigger,
                        )
                        continue
                await append_run_status_event(
                    session_id=prepared.session_id,
                    author=runner_name,
                    status=_failed_status_for_resume(resume_input),
                    invocation_id=prepared.invocation_id,
                    detail=str(exc),
                    metadata={"governance": exc.metadata}
                    if isinstance(exc, RuntimeCircuitOpen)
                    else None,
                    session_service_provider=provider,
                    run_mode=run_mode,
                    run_trigger=run_trigger,
                )
                await _persist_accumulated_reasoning()
                yield {"type": "error", "message": str(exc) or "Agent 运行失败"}
                return

        request_metadata_without_agentengine = {
            key: value
            for key, value in dict(request_metadata or {}).items()
            if key != "agentengine"
        }
        assistant_metadata = {
            **trace_metadata,
            **request_metadata_without_agentengine,
            **_merge_agentengine_metadata(request_metadata, runner_agentengine_metadata),
        }
        if responses_output:
            assistant_metadata["responses_output"] = responses_output
        if stream_usage:
            assistant_metadata["usage"] = stream_usage
        if stream_last_usage:
            assistant_metadata["last_usage"] = stream_last_usage
        elif stream_usage:
            assistant_metadata["last_usage"] = stream_usage
        if responses_response_id:
            assistant_metadata["response_id"] = responses_response_id
        if emitted_anything and not saw_final_chunk and not accumulated_text:
            await append_run_status_event(
                session_id=prepared.session_id,
                author=runner_name,
                status=_failed_status_for_resume(resume_input),
                invocation_id=prepared.invocation_id,
                detail="runner_stream_ended_without_final_output",
                session_service_provider=provider,
                run_mode=run_mode,
                run_trigger=run_trigger,
            )
            await _persist_accumulated_reasoning()
            _finish_span()
            yield {
                "type": "error",
                "message": "Agent 运行流已结束，但没有返回最终输出。",
                "session_id": prepared.session_id,
                "metadata": {
                    **assistant_metadata,
                    "detail": "runner_stream_ended_without_final_output",
                },
            }
            return
        _set_conversation_output_attributes(span, accumulated_text)

        await _persist_accumulated_reasoning()
        await append_conversation_event(
            session_id=prepared.session_id,
            author=runner_name,
            role="model",
            text=accumulated_text,
            invocation_id=prepared.invocation_id,
            event_type="assistant_message",
            metadata=assistant_metadata or None,
            session_service_provider=provider,
        )
        await _update_session_metadata_after_assistant_turn(
            service=provider(),
            session_id=prepared.session_id,
            assistant_text=accumulated_text,
            model=model,
        )
        await _auto_save_ltm_turn(
            agent_id=agent_id,
            user_id=user_id,
            prepared=prepared,
            output_text=accumulated_text,
            runner_type=runtime_context.runner_type,
            model=model,
        )
        await append_run_status_event(
            session_id=prepared.session_id,
            author=runner_name,
            status="completed",
            invocation_id=prepared.invocation_id,
            session_service_provider=provider,
            run_mode=run_mode,
            run_trigger=run_trigger,
        )
        _set_conversation_usage_attributes(span, assistant_metadata.get("usage"))
        _finish_span()
        yield {
            "type": "completed",
            "output_text": accumulated_text,
            "model": model,
            "session_id": prepared.session_id,
            "metadata": assistant_metadata,
            "responses_output": responses_output,
            "response_id": responses_response_id,
            "usage": assistant_metadata.get("usage"),
        }
    finally:
        _finish_span()


async def stream_conversation_turn(
    *,
    runner: Any,
    agent_id: str,
    user_id: str,
    session_id: Optional[str],
    messages: Sequence[Dict[str, Any]],
    model: Optional[str],
    prepare_runner: Callable[[Any, Optional[str]], None],
    model_metadata: Mapping[str, Any] | None = None,
    model_options: Mapping[str, Any] | None = None,
    state_delta: Optional[dict[str, Any]] = None,
    instructions: Optional[str] = None,
    request_metadata: Mapping[str, Any] | None = None,
    resume_input: Mapping[str, Any] | None = None,
    account_id: str | None = None,
    invocation_id: Optional[str] = None,
    session_service_provider: Callable[[], Any] | None = None,
    run_mode: str = RUN_MODE_FOREGROUND,
) -> AsyncIterator[str]:
    """Legacy ksadk response SSE stream used by hosted chat and chat-completions."""
    async for event in _iter_conversation_turn_events(
        runner=runner,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        messages=messages,
        model=model,
        prepare_runner=prepare_runner,
        model_metadata=model_metadata,
        model_options=model_options,
        state_delta=state_delta,
        instructions=instructions,
        request_metadata=request_metadata,
        resume_input=resume_input,
        account_id=account_id,
        invocation_id=invocation_id,
        session_service_provider=session_service_provider,
        run_mode=run_mode,
    ):
        event_type = event.get("type")
        if event_type == "compaction":
            yield build_compaction_sse_event(
                phase=str(event.get("phase") or "start"),
                trigger=str(event.get("trigger") or "auto"),
                compacted_until_seq_id=event.get("compacted_until_seq_id"),
                total_chars=event.get("total_chars"),
                total_estimated_tokens=event.get("total_estimated_tokens"),
                group_count=event.get("group_count"),
                threshold_percentage=event.get("threshold_percentage"),
            )
        elif event_type == "thinking":
            yield _response_sse("response.reasoning.delta", {"delta": event.get("delta", "")})
        elif event_type == "text":
            yield _response_sse("response.output_text.delta", {"delta": event.get("delta", "")})
        elif event_type == "tool_call":
            yield _response_sse(
                "response.tool_call",
                {
                    "name": event.get("name"),
                    "args": event.get("args", {}),
                    "run_id": event.get("run_id"),
                    "stage": event.get("stage"),
                    "event_kind": event.get("event_kind"),
                    "display_title": event.get("display_title"),
                    "display_summary": event.get("display_summary"),
                },
            )
        elif event_type == "tool_result":
            yield _response_sse(
                "response.tool_result",
                {
                    "name": event.get("name"),
                    "output": event.get("output", ""),
                    "run_id": event.get("run_id"),
                },
            )
        elif event_type in {"stage_tool_call", "stage_tool_result"}:
            yield _response_sse(
                f"response.ksadk.{event_type}",
                {
                    "type": event_type,
                    "name": event.get("name"),
                    "args": event.get("args", {}),
                    "output": event.get("output", ""),
                    "run_id": event.get("run_id"),
                    "stage": event.get("stage"),
                    "event_kind": event.get("event_kind"),
                    "display_title": event.get("display_title"),
                    "display_summary": event.get("display_summary"),
                },
            )
        elif event_type == "interrupt":
            yield _response_sse(
                "response.approval_request", {"interrupt_info": event.get("interrupt_info")}
            )
        elif event_type == "error":
            yield _response_sse(
                "response.error", {"message": event.get("message") or "Agent 运行失败"}
            )
        elif event_type == "cancelled":
            yield _response_sse("response.cancelled", {"status": "cancelled"})
        elif event_type == "completed":
            final_payload = build_responses_payload(
                output_text=str(event.get("output_text") or ""),
                model=event.get("model") or model,
                session_id=str(event.get("session_id") or session_id or ""),
                metadata=(
                    event.get("metadata") if isinstance(event.get("metadata"), Mapping) else None
                ),
            )
            yield _response_sse("response.completed", final_payload)


async def stream_responses_conversation_turn(
    *,
    runner: Any,
    agent_id: str,
    user_id: str,
    session_id: Optional[str],
    messages: Sequence[Dict[str, Any]],
    model: Optional[str],
    prepare_runner: Callable[[Any, Optional[str]], None],
    model_metadata: Mapping[str, Any] | None = None,
    model_options: Mapping[str, Any] | None = None,
    state_delta: Optional[dict[str, Any]] = None,
    instructions: Optional[str] = None,
    request_metadata: Mapping[str, Any] | None = None,
    resume_input: Mapping[str, Any] | None = None,
    account_id: str | None = None,
    invocation_id: Optional[str] = None,
    session_service_provider: Callable[[], Any] | None = None,
    run_mode: str = RUN_MODE_FOREGROUND,
) -> AsyncIterator[str]:
    """OpenAI Responses-style SSE stream."""
    response_id = f"resp_{uuid.uuid4().hex}"
    created_at = int(time.time())
    response_metadata = dict(request_metadata or {})

    message_item_id = f"msg_{uuid.uuid4().hex[:12]}"
    reasoning_item_id = f"rs_{uuid.uuid4().hex[:12]}"
    next_output_index = 0
    text_output_index: int | None = None
    reasoning_output_index: int | None = None
    message_started = False
    content_started = False
    reasoning_started = False
    completed_text = ""
    lifecycle_started = False

    def _start_response_lifecycle(current_session_id: str | None = None) -> list[str]:
        nonlocal lifecycle_started, response_metadata
        if lifecycle_started:
            return []
        lifecycle_started = True
        initial_payload = build_responses_payload(
            output_text="",
            model=model,
            session_id=current_session_id or session_id or "",
            response_id=response_id,
            created_at=created_at,
            status="in_progress",
            metadata=response_metadata,
        )
        return [
            _response_sse("response.created", initial_payload),
            _response_sse("response.in_progress", initial_payload),
        ]

    def _message_item(status: str, text: str = "") -> dict[str, Any]:
        content = [{"type": "output_text", "text": text}] if text or status == "completed" else []
        return {
            "id": message_item_id,
            "type": "message",
            "status": status,
            "role": "assistant",
            "content": content,
        }

    def _reasoning_item(status: str) -> dict[str, Any]:
        return {
            "id": reasoning_item_id,
            "type": "reasoning",
            "status": status,
            "summary": [],
        }

    def _next_output_index() -> int:
        nonlocal next_output_index
        output_index = next_output_index
        next_output_index += 1
        return output_index

    async for event in _iter_conversation_turn_events(
        runner=runner,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        messages=messages,
        model=model,
        prepare_runner=prepare_runner,
        model_metadata=model_metadata,
        model_options=model_options,
        state_delta=state_delta,
        instructions=instructions,
        request_metadata=request_metadata,
        resume_input=resume_input,
        response_id=response_id,
        account_id=account_id,
        invocation_id=invocation_id,
        session_service_provider=session_service_provider,
        run_mode=run_mode,
    ):
        event_metadata = event.get("metadata")
        if isinstance(event_metadata, Mapping):
            response_metadata.update(dict(event_metadata))
        if not lifecycle_started:
            for lifecycle_chunk in _start_response_lifecycle(str(event.get("session_id") or "")):
                yield lifecycle_chunk
        event_type = event.get("type")
        if event_type == "compaction":
            yield build_compaction_sse_event(
                phase=str(event.get("phase") or "start"),
                trigger=str(event.get("trigger") or "auto"),
                compacted_until_seq_id=event.get("compacted_until_seq_id"),
                total_chars=event.get("total_chars"),
                total_estimated_tokens=event.get("total_estimated_tokens"),
                group_count=event.get("group_count"),
                threshold_percentage=event.get("threshold_percentage"),
            )
            continue

        if event_type == "thinking":
            if not reasoning_started:
                reasoning_started = True
                reasoning_output_index = _next_output_index()
                yield _response_sse(
                    "response.output_item.added",
                    {
                        "output_index": reasoning_output_index,
                        "item": _reasoning_item("in_progress"),
                    },
                )
            yield _response_sse(
                "response.reasoning.delta",
                {
                    "item_id": reasoning_item_id,
                    "output_index": reasoning_output_index,
                    "delta": event.get("delta", ""),
                },
            )
            continue

        if event_type == "text":
            if not message_started:
                message_started = True
                text_output_index = _next_output_index()
                yield _response_sse(
                    "response.output_item.added",
                    {"output_index": text_output_index, "item": _message_item("in_progress")},
                )
            if not content_started:
                content_started = True
                yield _response_sse(
                    "response.content_part.added",
                    {
                        "item_id": message_item_id,
                        "output_index": text_output_index,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": ""},
                    },
                )
            delta = str(event.get("delta") or "")
            completed_text += delta
            yield _response_sse(
                "response.output_text.delta",
                {
                    "item_id": message_item_id,
                    "output_index": text_output_index,
                    "content_index": 0,
                    "delta": delta,
                },
            )
            continue

        if event_type == "tool_call":
            args_json = json.dumps(event.get("args", {}) or {}, ensure_ascii=False)
            call_id = str(event.get("run_id") or f"call_{uuid.uuid4().hex[:12]}")
            item_id = f"fc_{uuid.uuid4().hex[:12]}"
            call_output_index = _next_output_index()
            item = {
                "id": item_id,
                "type": "function_call",
                "status": "in_progress",
                "call_id": call_id,
                "name": event.get("name") or "unknown",
                "arguments": "",
            }
            yield _response_sse(
                "response.output_item.added", {"output_index": call_output_index, "item": item}
            )
            yield _response_sse(
                "response.function_call_arguments.delta",
                {"item_id": item_id, "output_index": call_output_index, "delta": args_json},
            )
            item["arguments"] = args_json
            item["status"] = "completed"
            yield _response_sse(
                "response.function_call_arguments.done",
                {"item_id": item_id, "output_index": call_output_index, "arguments": args_json},
            )
            yield _response_sse(
                "response.output_item.done", {"output_index": call_output_index, "item": item}
            )
            if event.get("display_title") or event.get("display_summary"):
                yield _response_sse(
                    "response.ksadk.tool_call",
                    {
                        "type": "tool_call",
                        "name": event.get("name"),
                        "args": event.get("args", {}),
                        "run_id": event.get("run_id"),
                        "stage": event.get("stage"),
                        "event_kind": event.get("event_kind"),
                        "display_title": event.get("display_title"),
                        "display_summary": event.get("display_summary"),
                    },
                )
            continue

        if event_type == "tool_result":
            yield _response_sse(
                "response.ksadk.tool_result",
                {
                    "name": event.get("name"),
                    "output": event.get("output", ""),
                    "run_id": event.get("run_id"),
                },
            )
            continue

        if event_type in {"stage_tool_call", "stage_tool_result"}:
            yield _response_sse(
                f"response.ksadk.{event_type}",
                {
                    "type": event_type,
                    "name": event.get("name"),
                    "args": event.get("args", {}),
                    "output": event.get("output", ""),
                    "run_id": event.get("run_id"),
                    "stage": event.get("stage"),
                    "event_kind": event.get("event_kind"),
                    "display_title": event.get("display_title"),
                    "display_summary": event.get("display_summary"),
                },
            )
            continue

        if event_type == "interrupt":
            interrupt_info = event.get("interrupt_info")
            if isinstance(interrupt_info, Mapping) and interrupt_info.get("tool_name"):
                raw_arguments = (
                    interrupt_info.get("arguments")
                    or interrupt_info.get("tool_args")
                    or interrupt_info.get("args")
                    or {}
                )
                arguments = (
                    raw_arguments
                    if isinstance(raw_arguments, str)
                    else json.dumps(raw_arguments, ensure_ascii=False)
                )
                approval_item = {
                    "id": str(
                        interrupt_info.get("approval_request_id")
                        or interrupt_info.get("id")
                        or f"appr_{uuid.uuid4().hex[:12]}"
                    ),
                    "type": "mcp_approval_request",
                    "name": str(interrupt_info.get("tool_name")),
                    "arguments": arguments,
                    "server_label": str(interrupt_info.get("server_label") or "ksadk"),
                }
                approval_output_index = _next_output_index()
                yield _response_sse(
                    "response.output_item.added",
                    {"output_index": approval_output_index, "item": approval_item},
                )
                yield _response_sse(
                    "response.output_item.done",
                    {"output_index": approval_output_index, "item": approval_item},
                )
            else:
                yield _response_sse(
                    "response.ksadk.approval_request", {"interrupt_info": interrupt_info}
                )
            incomplete_payload = build_responses_payload(
                output_text=completed_text,
                model=model,
                session_id=str(event.get("session_id") or session_id or ""),
                response_id=response_id,
                created_at=created_at,
                status="incomplete",
                metadata=response_metadata,
                incomplete_details={
                    "reason": "approval_required",
                    "ksadk_interrupt": interrupt_info,
                },
            )
            yield _response_sse("response.incomplete", incomplete_payload)
            return

        if event_type == "error":
            failed_payload = build_responses_payload(
                output_text=completed_text,
                model=model,
                session_id=session_id or "",
                response_id=response_id,
                created_at=created_at,
                status="failed",
                metadata=response_metadata,
                error={"message": event.get("message") or "Agent 运行失败"},
            )
            yield _response_sse("response.failed", failed_payload)
            return

        if event_type == "cancelled":
            cancelled_payload = build_responses_payload(
                output_text=completed_text,
                model=model,
                session_id=str(event.get("session_id") or session_id or ""),
                response_id=response_id,
                created_at=created_at,
                status="cancelled",
                metadata=(
                    event.get("metadata")
                    if isinstance(event.get("metadata"), Mapping)
                    else response_metadata
                ),
            )
            yield _response_sse("response.cancelled", cancelled_payload)
            return

        if event_type == "completed":
            completed_text = str(event.get("output_text") or completed_text)
            if completed_text and not message_started:
                message_started = True
                text_output_index = _next_output_index()
                yield _response_sse(
                    "response.output_item.added",
                    {"output_index": text_output_index, "item": _message_item("in_progress")},
                )
                content_started = True
                yield _response_sse(
                    "response.content_part.added",
                    {
                        "item_id": message_item_id,
                        "output_index": text_output_index,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": ""},
                    },
                )
            if message_started:
                yield _response_sse(
                    "response.output_text.done",
                    {
                        "item_id": message_item_id,
                        "output_index": text_output_index,
                        "content_index": 0,
                        "text": completed_text,
                    },
                )
                yield _response_sse(
                    "response.content_part.done",
                    {
                        "item_id": message_item_id,
                        "output_index": text_output_index,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": completed_text},
                    },
                )
                yield _response_sse(
                    "response.output_item.done",
                    {
                        "output_index": text_output_index,
                        "item": _message_item("completed", completed_text),
                    },
                )
            if reasoning_started:
                yield _response_sse(
                    "response.output_item.done",
                    {"output_index": reasoning_output_index, "item": _reasoning_item("completed")},
                )
            final_payload = build_responses_payload(
                output_text=completed_text,
                model=event.get("model") or model,
                session_id=str(event.get("session_id") or session_id or ""),
                response_id=str(event.get("response_id") or response_id),
                created_at=created_at,
                status="completed",
                metadata=(
                    event.get("metadata")
                    if isinstance(event.get("metadata"), Mapping)
                    else response_metadata
                ),
                output_items=(
                    event.get("responses_output")
                    if isinstance(event.get("responses_output"), Sequence)
                    else None
                ),
            )
            yield _response_sse("response.completed", final_payload)
            return

    if not lifecycle_started:
        for lifecycle_chunk in _start_response_lifecycle():
            yield lifecycle_chunk
