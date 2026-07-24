from __future__ import annotations

import json
import os
from typing import Any, Mapping, Sequence

from ksadk.conversations.reasoning_markup import strip_reasoning_markup
from ksadk.conversations.runtime_constants import (
    PROMPT_TOO_LONG_MARKERS,
    logger,
)
from ksadk.conversations.runtime_observability import _extract_deferred_tool_names
from ksadk.conversations.runtime_payloads import PreparedConversationTurn
from ksadk.conversations.runtime_resume import _is_checkpoint_resume_input
from ksadk.knowledge_base.service import KnowledgeBaseService
from ksadk.memory.service import LongTermMemoryService
from ksadk.runtime_context import (
    PlatformInvocationContext,
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
    return str(runner.__class__.__name__).lower()


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
    runner: Any | None = None,
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
    if getattr(runner, "api_format", "") == "responses" and prepared.responses_history:
        payload["responses_history"] = list(prepared.responses_history)
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


def _merge_responses_history_with_session_history(
    request_history: Sequence[dict[str, Any]],
    session_history: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge request-provided Responses items with the session projection."""
    if not request_history:
        return [dict(item) for item in session_history]
    if not session_history:
        return [dict(item) for item in request_history]

    prefix_len = min(len(request_history), len(session_history))
    if list(request_history[:prefix_len]) == list(session_history[:prefix_len]):
        return [
            *[dict(item) for item in request_history],
            *[dict(item) for item in session_history[prefix_len:]],
        ]
    return [
        *[dict(item) for item in request_history],
        *[dict(item) for item in session_history],
    ]
