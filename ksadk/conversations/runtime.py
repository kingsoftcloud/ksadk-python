from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, Mapping, Optional, Sequence

import httpx
from fastapi import HTTPException

from ksadk.conversations.context import (
    TRANSCRIPT_EVENT_TYPES,
    build_history_from_events,
    build_request_history,
    canonical_event_type,
    compacted_until_seq_id,
    extract_event_text,
    group_events_by_api_round,
    summarize_event_groups,
)
from ksadk.conversations.model_context import (
    estimate_text_tokens,
    get_auto_compact_threshold_percentage,
    get_auto_compact_threshold_tokens,
    normalize_model_metadata,
)
from ksadk.conversations.attachments import compact_attachment_result_for_session
from ksadk.conversations.normalize import compact_attachment_for_session, normalize_kop_messages
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
from ksadk.runtime_context import PlatformInvocationContext, platform_invocation_scope
from ksadk.sessions import Session, SessionEvent, resolve_session_service

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
    user_parts: list[dict[str, Any]]
    attachments: list[dict[str, Any]]
    attachment_results: list[dict[str, Any]]
    model_metadata: dict[str, Any] = field(default_factory=dict)
    instructions: str = ""
    request_metadata: dict[str, Any] = field(default_factory=dict)
    compaction_triggered: bool = False
    compaction_trigger: str | None = None
    compacted_until_seq_id: int | None = None
    resume_input: dict[str, Any] | None = None


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
) -> dict[str, Any]:
    response_id = response_id or f"resp_{uuid.uuid4().hex}"
    created_at = created_at or int(time.time())
    message_id = f"msg_{uuid.uuid4().hex[:12]}"
    output_item_status = "completed" if status == "completed" else status
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "error": dict(error) if isinstance(error, Mapping) else None,
        "incomplete_details": dict(incomplete_details) if isinstance(incomplete_details, Mapping) else None,
        "instructions": None,
        "metadata": dict(metadata or {}),
        "model": model or "agent",
        "parallel_tool_calls": True,
        "temperature": None,
        "top_p": None,
        "tools": [],
        "output": [
            {
                "id": message_id,
                "type": "message",
                "status": output_item_status,
                "role": "assistant",
                "content": [{"type": "output_text", "text": output_text}],
            }
        ],
        "output_text": output_text,
        "usage": {
            "input_tokens": 0,
            "output_tokens": len(output_text),
            "total_tokens": len(output_text),
        },
        "session_id": session_id,
    }


def extract_responses_resume_input(input_payload: Any) -> dict[str, Any] | None:
    """Extract OpenAI Responses approval resume input without exposing runner details."""
    if isinstance(input_payload, Mapping):
        candidates = [input_payload]
    elif isinstance(input_payload, Sequence) and not isinstance(input_payload, (str, bytes, bytearray)):
        candidates = [item for item in input_payload if isinstance(item, Mapping)]
    else:
        return None

    for item in candidates:
        item_type = str(item.get("type") or "").strip()
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

        if item_type in {"ksadk_resume", "ksadk.approval_response"}:
            resume_input = {"type": "ksadk_resume"}
            interrupt_id = item.get("interrupt_id") or item.get("approval_request_id") or item.get("id")
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


def build_chat_completions_payload(*, output_text: str, model: Optional[str], session_id: str) -> dict[str, Any]:
    return {
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
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": len(output_text),
            "total_tokens": len(output_text),
        },
        "session_id": session_id,
    }


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
    return f"event: response.compaction.{phase}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


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
        "input_parts": prepared.user_parts,
        "attachments": prepared.attachments,
        "attachment_results": prepared.attachment_results,
        "model": model,
        "model_metadata": prepared.model_metadata,
        "platform_context": runtime_context.to_payload(),
        "kb_context": runtime_context.kb_context,
        "memory_context": runtime_context.memory_context,
    }
    if prepared.instructions:
        payload["instructions"] = prepared.instructions
    if prepared.resume_input is not None:
        payload["input"] = prepared.resume_input
        payload["resume"] = True
    return payload


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

    return f"{item_type} {json.dumps(dict(resume_input), ensure_ascii=False, sort_keys=True)}"


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


async def _update_session_metadata_after_assistant_turn(
    *,
    service: Any,
    session_id: str,
    assistant_text: str,
    model: str | None,
) -> None:
    summary = _truncate_text(assistant_text, SESSION_SUMMARY_MAX_CHARS)
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
    title_client = resolve_session_title_client()
    title_model = resolve_session_title_model(model)
    if title_client.is_available and title_model:
        try:
            title, _usage = await title_client.generate_title(
                model=title_model,
                messages=build_session_title_messages(
                    first_prompt=first_prompt,
                    assistant_text=summary,
                ),
                timeout_ms=DEFAULT_SESSION_TITLE_TIMEOUT_MS,
            )
        except Exception:
            title = ""

        if title and not is_low_quality_title(title, first_prompt=first_prompt):
            next_title = title
            next_title_source = "ai"

    if not next_title or next_title == (session.title or "").strip():
        return
    await service.update_session_metadata(
        session_id,
        title=next_title,
        title_source=next_title_source,
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
            key in message for key in ("display_content", "attachments", "attachment_results", "parts")
        ):
            normalized_messages.append(
                {
                    "role": str(message.get("role") or "user"),
                    "content": str(message.get("content") or ""),
                    "display_content": str(
                        message.get("display_content") or message.get("content") or ""
                    ),
                    "parts": list(message.get("parts") or []),
                    "attachments": list(message.get("attachments") or []),
                    "attachment_results": list(message.get("attachment_results") or []),
                }
            )
            continue
        normalized_messages.extend(normalize_kop_messages([message]))
    return normalized_messages


def _latest_user_turn(
    normalized_messages: Sequence[Dict[str, Any]],
) -> tuple[str, str, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    latest_user_message = next(
        (message for message in reversed(normalized_messages) if message.get("role") == "user"),
        {},
    )
    user_input = str(latest_user_message.get("content") or "")
    user_display_input = str(latest_user_message.get("display_content") or user_input)
    user_parts = list(latest_user_message.get("parts") or [])
    attachments = list(latest_user_message.get("attachments") or [])
    attachment_results = list(latest_user_message.get("attachment_results") or [])
    return user_input, user_display_input, user_parts, attachments, attachment_results


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
        list(payload.get("attachments") or []),
        list(payload.get("attachment_results") or []),
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
            "attachments": [dict(item) for item in attachments if isinstance(item, dict)],
            "attachment_results": [dict(item) for item in attachment_results if isinstance(item, dict)],
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
                "attachments": [compact_attachment_for_session(item) for item in attachments if item],
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
    tail_groups = keep_tail_groups if keep_tail_groups is not None else (
        PTL_RETRY_KEEP_TAIL_GROUPS if force else AUTOCOMPACT_KEEP_TAIL_GROUPS
    )
    resolved_model_metadata = _resolve_model_metadata(model, model_metadata=model_metadata)
    auto_compact_threshold_tokens = get_auto_compact_threshold_tokens(resolved_model_metadata)
    auto_compact_threshold_percentage = get_auto_compact_threshold_percentage(resolved_model_metadata)
    total_chars = sum(len(extract_event_text(event)) for event in combined_events)
    total_estimated_tokens = sum(estimate_text_tokens(extract_event_text(event)) for event in combined_events)
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

    compactable_indexes = [index for index in range(len(groups)) if index not in pinned_group_indexes]
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
    user_input, user_display_input, _, attachments, attachment_results = _latest_user_turn(normalized_messages)
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
    session_service_provider: Callable[[], Any] | None = None,
) -> SessionEvent:
    """记录运行态事件，供 UI/恢复逻辑区分 turn 生命周期。"""
    content = {"status": status}
    if detail:
        content["detail"] = detail
    return await append_conversation_event(
        session_id=session_id,
        author=author,
        role="model",
        text="",
        invocation_id=invocation_id,
        event_type="run_status",
        content=content,
        metadata={"status": status, **({"detail": detail} if detail else {})},
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
    summary_result = await summarize_compaction(
        groups_to_compact=plan.groups_to_compact,
        previous_summary=previous_summary,
        pinned_state=plan.pinned_state,
        model_metadata=resolved_model_metadata,
        model=model,
    )
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
    state_delta: Optional[dict[str, Any]] = None,
    instructions: Optional[str] = None,
    request_metadata: Mapping[str, Any] | None = None,
    resume_input: Mapping[str, Any] | None = None,
    invocation_id: Optional[str] = None,
    session_service_provider: Callable[[], Any] | None = None,
) -> PreparedConversationTurn:
    """构建一次 turn 的标准运行输入，并在进入模型前做上下文投影/压缩。"""
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
    normalized_instructions = str(instructions or "").strip()

    if resume_input is not None:
        if not session_id:
            raise ValueError("Responses resume input requires session_id")
        existing_events = await service.get_events(resolved_session_id)
        if not _has_pending_approval(existing_events):
            raise ValueError("Responses resume input requires a pending approval_request")

        normalized_resume_input = dict(resume_input)
        resume_text = _format_resume_response_text(normalized_resume_input)
        await append_conversation_event(
            session_id=resolved_session_id,
            author="user",
            role="user",
            text=resume_text,
            invocation_id=resolved_invocation_id,
            event_type="approval_response",
            session_service_provider=provider,
            metadata={"resume_input": normalized_resume_input},
        )
        history = build_history_from_events(await service.get_events(resolved_session_id))
        return PreparedConversationTurn(
            session_id=resolved_session_id,
            invocation_id=resolved_invocation_id,
            user_input=resume_text,
            user_display_input=resume_text,
            history=history,
            user_parts=[],
            attachments=[],
            attachment_results=[],
            model_metadata=resolved_model_metadata,
            instructions=normalized_instructions,
            request_metadata=normalized_request_metadata,
            resume_input=normalized_resume_input,
        )

    normalized_messages = _normalized_conversation_messages(messages)
    user_input, user_display_input, user_parts, attachments, attachment_results = _latest_user_turn(
        normalized_messages
    )
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
            compact_attachment_result_for_session(item)
            for item in attachment_results
            if item
        ],
    }

    if normalized_instructions:
        event_metadata["instructions"] = normalized_instructions
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
        session_service_provider=provider,
        metadata=event_metadata,
    )
    await _update_session_metadata_after_user_turn(
        service=service,
        session=session,
        user_input=user_input or user_display_input,
    )

    checkpoint = await compact_conversation_history(
        session_id=resolved_session_id,
        author=agent_id,
        invocation_id=resolved_invocation_id,
        model=model,
        model_metadata=resolved_model_metadata,
        session_service_provider=provider,
    )
    history = build_history_from_events(await service.get_events(resolved_session_id))
    if not history:
        history = build_request_history(normalized_messages[:-1])

    return PreparedConversationTurn(
        session_id=resolved_session_id,
        invocation_id=resolved_invocation_id,
        user_input=user_input,
        user_display_input=user_display_input or user_input,
        history=history,
        user_parts=user_parts,
        attachments=effective_attachments,
        attachment_results=effective_attachment_results,
        model_metadata=resolved_model_metadata,
        instructions=normalized_instructions,
        request_metadata=normalized_request_metadata,
        compaction_triggered=checkpoint is not None,
        compaction_trigger=str((checkpoint.metadata or {}).get("trigger") or "auto")
        if checkpoint
        else None,
        compacted_until_seq_id=int((checkpoint.metadata or {}).get("compacted_until_seq_id") or 0)
        if checkpoint
        else None,
    )


async def _refresh_history(prepared: PreparedConversationTurn, *, session_service_provider: Callable[[], Any] | None = None) -> PreparedConversationTurn:
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
    state_delta: Optional[dict[str, Any]] = None,
    instructions: Optional[str] = None,
    request_metadata: Mapping[str, Any] | None = None,
    resume_input: Mapping[str, Any] | None = None,
    session_service_provider: Callable[[], Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """非流式 turn 编排入口。

    顺序固定为：写用户事件 -> 需要时 compact -> 写 run_status(in_progress)
    -> 调 runner -> PTL 时 compact/retry -> 写 assistant 结果 -> 写 completed。
    """
    provider = session_service_provider or resolve_session_service
    prepare_runner(runner, model)
    prepared = await build_run_input(
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        messages=messages,
        model=model,
        model_metadata=model_metadata,
        state_delta=state_delta,
        instructions=instructions,
        request_metadata=request_metadata,
        resume_input=resume_input,
        session_service_provider=provider,
    )
    ambient_contexts = _build_runner_ambient_contexts(
        runner=runner,
        user_id=user_id,
        user_input=prepared.user_input,
    )
    runtime_context = PlatformInvocationContext(
        agent_id=agent_id,
        user_id=user_id,
        session_id=prepared.session_id,
        history=list(prepared.history),
        input_parts=list(prepared.user_parts),
        attachments=list(prepared.attachments),
        attachment_results=list(prepared.attachment_results),
        runner_type=_runner_type_name(runner),
        model=model,
        kb_context=ambient_contexts.get("kb_context"),
        memory_context=ambient_contexts.get("memory_context"),
    )
    runner_name = _runner_name(runner)
    await append_run_status_event(
        session_id=prepared.session_id,
        author=runner_name,
        status="in_progress",
        invocation_id=prepared.invocation_id,
        session_service_provider=provider,
    )

    result: dict[str, Any] | None = None
    for attempt in range(2):
        try:
            runtime_context.history = list(prepared.history)
            with platform_invocation_scope(runtime_context):
                result = await runner.invoke(
                    _build_runner_request_payload(
                        prepared=prepared,
                        model=model,
                        runtime_context=runtime_context,
                    )
                )
            break
        except Exception as exc:
            if attempt == 0 and _is_prompt_too_long_error(exc):
                checkpoint = await compact_conversation_history(
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
                if checkpoint:
                    prepared = await _refresh_history(prepared, session_service_provider=provider)
                    runtime_context.history = list(prepared.history)
                    continue
            await append_run_status_event(
                session_id=prepared.session_id,
                author=runner_name,
                status="failed",
                invocation_id=prepared.invocation_id,
                detail=str(exc),
                session_service_provider=provider,
            )
            raise

    result = result or {}
    output_text = str(result.get("output", ""))
    await append_conversation_event(
        session_id=prepared.session_id,
        author=runner_name,
        role="model",
        text=output_text,
        invocation_id=prepared.invocation_id,
        event_type="assistant_message",
        metadata={"request_metadata": prepared.request_metadata} if prepared.request_metadata else None,
        session_service_provider=provider,
    )
    await _update_session_metadata_after_assistant_turn(
        service=provider(),
        session_id=prepared.session_id,
        assistant_text=output_text,
        model=model,
    )
    await append_run_status_event(
        session_id=prepared.session_id,
        author=runner_name,
        status="completed",
        invocation_id=prepared.invocation_id,
        session_service_provider=provider,
    )
    return prepared.session_id, {
        "output_text": output_text,
        "model": model,
        "metadata": prepared.request_metadata,
    }


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
    state_delta: Optional[dict[str, Any]] = None,
    instructions: Optional[str] = None,
    request_metadata: Mapping[str, Any] | None = None,
    resume_input: Mapping[str, Any] | None = None,
    session_service_provider: Callable[[], Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Internal semantic event stream shared by protocol serializers."""
    provider = session_service_provider or resolve_session_service
    prepare_runner(runner, model)
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
    prepared = await build_run_input(
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        messages=messages,
        model=model,
        model_metadata=model_metadata,
        state_delta=state_delta,
        instructions=instructions,
        request_metadata=request_metadata,
        resume_input=resume_input,
        session_service_provider=provider,
    )
    ambient_contexts = _build_runner_ambient_contexts(
        runner=runner,
        user_id=user_id,
        user_input=prepared.user_input,
    )
    runtime_context = PlatformInvocationContext(
        agent_id=agent_id,
        user_id=user_id,
        session_id=prepared.session_id,
        history=list(prepared.history),
        input_parts=list(prepared.user_parts),
        attachments=list(prepared.attachments),
        attachment_results=list(prepared.attachment_results),
        runner_type=_runner_type_name(runner),
        model=model,
        kb_context=ambient_contexts.get("kb_context"),
        memory_context=ambient_contexts.get("memory_context"),
    )
    if prepared.compaction_triggered:
        yield {
            "type": "compaction",
            "phase": "done",
            "trigger": str(prepared.compaction_trigger or "auto"),
            "compacted_until_seq_id": prepared.compacted_until_seq_id,
            "total_chars": compaction_preview.total_chars if compaction_preview.should_compact else None,
            "total_estimated_tokens": compaction_preview.total_estimated_tokens
            if compaction_preview.should_compact
            else None,
            "group_count": compaction_preview.group_count if compaction_preview.should_compact else None,
            "threshold_percentage": compaction_preview.auto_compact_threshold_percentage
            if compaction_preview.should_compact
            else None,
        }
    runner_name = _runner_name(runner)
    await append_run_status_event(
        session_id=prepared.session_id,
        author=runner_name,
        status="in_progress",
        invocation_id=prepared.invocation_id,
        session_service_provider=provider,
    )

    accumulated_text = ""
    emitted_anything = False
    responses_output: list[Any] = []
    responses_response_id: str | None = None
    for attempt in range(2):
        try:
            runtime_context.history = list(prepared.history)
            with platform_invocation_scope(runtime_context):
                async for chunk in runner.stream(
                    _build_runner_request_payload(
                        prepared=prepared,
                        model=model,
                        runtime_context=runtime_context,
                    )
                ):
                    chunk_type = chunk.get("type")
                    if chunk_type == "responses_output":
                        raw_output = chunk.get("output")
                        responses_output = raw_output if isinstance(raw_output, list) else []
                        raw_response_id = chunk.get("response_id")
                        responses_response_id = str(raw_response_id) if raw_response_id else responses_response_id
                        continue
                    if chunk_type == "thinking":
                        delta = str(chunk.get("delta", ""))
                        if delta:
                            emitted_anything = True
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
                        await append_conversation_event(
                            session_id=prepared.session_id,
                            author=runner_name,
                            role="model",
                            text=str(chunk.get("tool_name") or "tool"),
                            invocation_id=prepared.invocation_id,
                            event_type="tool_call",
                            metadata={
                                "tool_name": chunk.get("tool_name"),
                                "tool_args": chunk.get("tool_args", {}),
                                "run_id": chunk.get("run_id"),
                            },
                            session_service_provider=provider,
                        )
                        emitted_anything = True
                        yield {
                            "type": "tool_call",
                            "name": chunk.get("tool_name"),
                            "args": chunk.get("tool_args", {}),
                            "run_id": chunk.get("run_id"),
                        }
                        continue
                    if chunk_type == "tool_result":
                        await append_conversation_event(
                            session_id=prepared.session_id,
                            author=runner_name,
                            role="user",
                            text=str(chunk.get("tool_output", "")),
                            invocation_id=prepared.invocation_id,
                            event_type="tool_result",
                            metadata={
                                "tool_name": chunk.get("tool_name"),
                                "tool_output": chunk.get("tool_output", ""),
                                "run_id": chunk.get("run_id"),
                            },
                            session_service_provider=provider,
                        )
                        emitted_anything = True
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
                        )
                        emitted_anything = True
                        yield {
                            "type": "interrupt",
                            "interrupt_info": interrupt_info,
                            "session_id": prepared.session_id,
                            "metadata": prepared.request_metadata,
                        }
                        return
                    if chunk_type == "final":
                        final_text = str(chunk.get("output", ""))
                        if final_text:
                            accumulated_text = final_text
            break
        except Exception as exc:
            if attempt == 0 and not emitted_anything and _is_prompt_too_long_error(exc):
                yield {"type": "compaction", "phase": "start", "trigger": "prompt_too_long"}
                checkpoint = await compact_conversation_history(
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
                    prepared = await _refresh_history(prepared, session_service_provider=provider)
                    runtime_context.history = list(prepared.history)
                    continue
            await append_run_status_event(
                session_id=prepared.session_id,
                author=runner_name,
                status="failed",
                invocation_id=prepared.invocation_id,
                detail=str(exc),
                session_service_provider=provider,
            )
            yield {"type": "error", "message": str(exc) or "Agent 运行失败"}
            return

    assistant_metadata = dict(request_metadata or {})
    if responses_output:
        assistant_metadata["responses_output"] = responses_output
    if responses_response_id:
        assistant_metadata["response_id"] = responses_response_id

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
    await append_run_status_event(
        session_id=prepared.session_id,
        author=runner_name,
        status="completed",
        invocation_id=prepared.invocation_id,
        session_service_provider=provider,
    )
    yield {
        "type": "completed",
        "output_text": accumulated_text,
        "model": model,
        "session_id": prepared.session_id,
        "metadata": assistant_metadata,
    }


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
    state_delta: Optional[dict[str, Any]] = None,
    instructions: Optional[str] = None,
    request_metadata: Mapping[str, Any] | None = None,
    resume_input: Mapping[str, Any] | None = None,
    session_service_provider: Callable[[], Any] | None = None,
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
        state_delta=state_delta,
        instructions=instructions,
        request_metadata=request_metadata,
        resume_input=resume_input,
        session_service_provider=session_service_provider,
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
                {"name": event.get("name"), "args": event.get("args", {}), "run_id": event.get("run_id")},
            )
        elif event_type == "tool_result":
            yield _response_sse(
                "response.tool_result",
                {"name": event.get("name"), "output": event.get("output", ""), "run_id": event.get("run_id")},
            )
        elif event_type == "interrupt":
            yield _response_sse("response.approval_request", {"interrupt_info": event.get("interrupt_info")})
        elif event_type == "error":
            yield _response_sse("response.error", {"message": event.get("message") or "Agent 运行失败"})
        elif event_type == "completed":
            final_payload = build_responses_payload(
                output_text=str(event.get("output_text") or ""),
                model=event.get("model") or model,
                session_id=str(event.get("session_id") or session_id or ""),
                metadata=event.get("metadata") if isinstance(event.get("metadata"), Mapping) else None,
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
    state_delta: Optional[dict[str, Any]] = None,
    instructions: Optional[str] = None,
    request_metadata: Mapping[str, Any] | None = None,
    resume_input: Mapping[str, Any] | None = None,
    session_service_provider: Callable[[], Any] | None = None,
) -> AsyncIterator[str]:
    """OpenAI Responses-style SSE stream."""
    response_id = f"resp_{uuid.uuid4().hex}"
    created_at = int(time.time())
    response_metadata = dict(request_metadata or {})
    initial_payload = build_responses_payload(
        output_text="",
        model=model,
        session_id=session_id or "",
        response_id=response_id,
        created_at=created_at,
        status="in_progress",
        metadata=response_metadata,
    )
    yield _response_sse("response.created", initial_payload)
    yield _response_sse("response.in_progress", initial_payload)

    message_item_id = f"msg_{uuid.uuid4().hex[:12]}"
    reasoning_item_id = f"rs_{uuid.uuid4().hex[:12]}"
    next_output_index = 0
    text_output_index: int | None = None
    reasoning_output_index: int | None = None
    message_started = False
    content_started = False
    reasoning_started = False
    completed_text = ""

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
        state_delta=state_delta,
        instructions=instructions,
        request_metadata=request_metadata,
        resume_input=resume_input,
        session_service_provider=session_service_provider,
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
            yield _response_sse("response.output_item.added", {"output_index": call_output_index, "item": item})
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
            yield _response_sse("response.output_item.done", {"output_index": call_output_index, "item": item})
            continue

        if event_type == "tool_result":
            yield _response_sse(
                "response.ksadk.tool_result",
                {"name": event.get("name"), "output": event.get("output", ""), "run_id": event.get("run_id")},
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
                yield _response_sse("response.ksadk.approval_request", {"interrupt_info": interrupt_info})
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
                    {"output_index": text_output_index, "item": _message_item("completed", completed_text)},
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
                response_id=response_id,
                created_at=created_at,
                status="completed",
                metadata=event.get("metadata") if isinstance(event.get("metadata"), Mapping) else response_metadata,
            )
            yield _response_sse("response.completed", final_payload)
            return
