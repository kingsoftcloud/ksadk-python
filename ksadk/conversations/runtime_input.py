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


def _prompt_compiler_enabled() -> bool:
    """PR B：全局 kill switch。默认关——关闭时 Runner 输入与旧逻辑字节级一致。

    接管由三重门控共同决定：本 flag × per-Agent ``prompt_integration_mode``
    （由 ``prompt_ownership=ksadk`` 标记的 per-Build）× runner 类型限定 LangGraph。
    任一不满足 → ``_should_project_compiled_prompt`` 返回 False → 走旧 ``instructions`` 分支。
    """
    return _env_flag("KSADK_PROMPT_COMPILER_ENABLED", False)


def _should_project_compiled_prompt(
    *, prepared: PreparedConversationTurn, runner: Any | None
) -> bool:
    """PR B：判断本 turn 是否用 ``compiled_prompt`` 接管 ``payload["instructions"]``。

    满足全部条件才接管：
    1. 全局 flag 开（``KSADK_PROMPT_COMPILER_ENABLED``）；
    2. per-Build 接管标记 ``prompt_integration_mode=="ksadk_hosted"``
       （仅 ``prompt_ownership=ksadk``）；
    3. 已编译出真实 CompiledPrompt 且含非空 ``prompt_content``（agent_system/agent_task 非空，
       非 resume 旁路）；
    4. runner 类型为 langgraph（ADK/Codex 接管错位，排除）。

    任一不满足 → 返回 False → 调用方走 ``elif`` 分支 == 旧 ``if``，字节级一致。
    """
    if not _prompt_compiler_enabled():
        return False
    if prepared.prompt_integration_mode != "ksadk_hosted":
        return False
    compiled = prepared.compiled_prompt
    if not isinstance(compiled, Mapping):
        return False
    content = compiled.get("prompt_content")
    if not isinstance(content, str) or not content.strip():
        return False
    return _runner_type_name(runner) == "langgraph"


def _should_use_hosted_assembly(*, prepared: PreparedConversationTurn, runner: Any | None) -> bool:
    """PR E：判断本 turn 是否用 hosted pipeline 的 assembled_input 接管 payload。

    条件：``assembled_input`` 已生成（build_run_input 在 V2 门控下产出）且 runner 为
    langgraph 系（prompt_owner=ksadk）。该分支优先于 PR B/D2；满足时直接 return，不双重注入。
    native_runtime（codex）的 ``assembled_input`` 恒为 None（build_run_input 不为它生成），
    故 Managed Codex 不受影响（方案 §6.2 / PCM-RUNNER-003）。
    """
    if not isinstance(prepared.assembled_input, Mapping):
        return False
    if not str(prepared.assembled_input.get("system") or "").strip():
        return False
    return _runner_type_name(runner) == "langgraph"


def _assembled_input(prepared: PreparedConversationTurn) -> Any:
    """把 prepared.assembled_input 的 plain dict 还原成 assembler 能消费的形式。"""
    from ksadk.context_engine.assembler import AssembledInput

    d = prepared.assembled_input
    return AssembledInput(
        format=d.get("format", "chat"),
        system=str(d.get("system") or ""),
        messages=list(d.get("messages") or []),
        responses_items=[],
        estimated_tokens=int(d.get("estimated_tokens") or 0),
        warnings=tuple(d.get("warnings") or ()),
    )


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

    # 显式 error 字段（PR：Memory Recall 失败语义）：build_context 失败时把原因放
    # 独立 ``error`` 字段、``formatted_text`` 置空，错误不进模型上下文。
    if str(context.get("error") or "").strip():
        return True

    formatted_text = str(context.get("formatted_text") or "").strip()
    # 真无记忆不是可注入的上下文。它不是 provider failure，但对投影层而言
    # 同样应被丢弃，避免 UI 和审计把空召回误报成“已使用长期记忆”。
    if not formatted_text:
        return True

    # 纵深防御：``search_text``（工具路径）仍会把错误塞进正文，这里按前缀兜底，
    # 防止任何直接调 ``search_text`` 拼上下文的路径把错误字符串注入。
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
        "memory_recall_events": [],
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
                contexts.setdefault("memory_recall_events", []).append(
                    {"type": "memory.recall.completed", "count": 1}
                )
            else:
                contexts.setdefault("memory_recall_events", []).append(
                    {"type": "memory.recall.empty"}
                )
        except Exception as exc:
            logger.warning("Failed to build ambient memory context: %s", exc)
            contexts.setdefault("memory_recall_events", []).append(
                {"type": "memory.recall.failed", "error": str(exc)[:200]}
            )

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
    if prepared.request_metadata:
        # Keep endpoint-level controls available to an application entrypoint
        # (for example, its conversation approval profile) without leaking
        # caller public metadata into the agent payload.
        payload["request_metadata"] = dict(prepared.request_metadata)
    # PR E：hosted pipeline 接管（最高优先级）。当 build_run_input 产出 assembled_input 时，
    # 用组装好的 system/input/history 直接覆盖 payload——它已含 compiled_prompt + working_state
    # + planner 决策后的有序 messages。此分支满足后不再走 PR B/D2（避免双重注入）。
    if _should_use_hosted_assembly(prepared=prepared, runner=runner):
        from ksadk.context_engine.hosted_pipeline import assembled_to_payload

        override = assembled_to_payload(_assembled_input(prepared))
        if override["instructions"]:
            payload["instructions"] = override["instructions"]
        if override["input"]:
            payload["input"] = override["input"]
        # An empty assembled history is authoritative: on the first turn the
        # just-persisted user event must not survive from ``prepared.history``
        # and be injected alongside the canonical current input.
        payload["history"] = override["history"]
        payload["context_plan_id"] = (
            prepared.context_plan.get("plan_id") if prepared.context_plan else None
        )
        return payload
    # PR B：LangGraph CompiledPrompt→instructions 接管。三重门控满足时，把
    # payload["instructions"] 替换为 CompiledPrompt.content（XML），使 agent_system/
    # agent_task 首次进模型输入。任一门控不满足 → elif == 旧逻辑（字节级一致）。
    if _should_project_compiled_prompt(prepared=prepared, runner=runner):
        payload["instructions"] = prepared.compiled_prompt["prompt_content"]
    elif prepared.instructions:
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
            resume_input = dict(prepared.resume_input)
            gateway_approval_resume = bool(
                resume_input.pop("_ksadk_gateway_approval_resume", False)
            )
            if gateway_approval_resume and bool(
                getattr(runner, "supports_gateway_approval_semantic_resume", False)
            ):
                resume_input["_ksadk_gateway_approval_resume"] = True
            payload["input"] = resume_input
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
    # PR D2：WorkingState 门控重注入。仅 ksadk_hosted + 有 working_state 时，把结构化工作面
    # 渲染成 XML 段追加进 instructions（与 CompiledPrompt.content 风格一致，LangGraph _to_state
    # 能消费 instructions 字符串）。非门控或有 CompiledPrompt 接管时仍由前者决定 instructions。
    _maybe_inject_working_state(payload, prepared)
    return payload


def _maybe_inject_working_state(
    payload: dict[str, Any], prepared: PreparedConversationTurn
) -> None:
    """PR D2：把 working_state 渲染成 <working_state> XML 段追加进 payload instructions。

    门控：仅 ``prompt_integration_mode=="ksadk_hosted"`` 且 ``working_state`` 非空时注入。
    与 PR B 的 CompiledPrompt 接管叠加：若 instructions 已被 CompiledPrompt 接管（XML），
    WorkingState 段追加在其后；否则追加在 request instructions 后。非门控零注入。
    Prompt 明文不进 Trace（working_state 不进 shadow plan/trace）。
    """
    if prepared.prompt_integration_mode != "ksadk_hosted":
        return
    ws = prepared.working_state
    if not isinstance(ws, Mapping) or not ws:
        return
    xml = _render_working_state_xml(ws)
    if not xml:
        return
    existing = str(payload.get("instructions") or "").strip()
    if existing:
        payload["instructions"] = f"{existing}\n\n{xml}"
    else:
        payload["instructions"] = xml


def _render_working_state_xml(ws: Mapping[str, Any]) -> str:
    """把 working_state 审计 dict 渲染成 <working_state> XML 段（供模型理解当前工作面）。"""
    current_goal = str(ws.get("current_goal") or "").strip()
    next_action = str(ws.get("next_action") or "").strip()
    active_files = ws.get("active_files") or []
    pending_tools = ws.get("pending_tools") or []
    pending_approvals = ws.get("pending_approvals") or []
    lines: list[str] = []
    if current_goal:
        lines.append(f"当前目标：{current_goal}")
    if next_action:
        lines.append(f"下一步：{next_action}")
    if isinstance(active_files, list) and active_files:
        files = ", ".join(
            str((f.get("path") if isinstance(f, Mapping) else "") or "") for f in active_files
        ).strip(", ")
        if files:
            lines.append(f"活跃文件：{files}")
    if isinstance(pending_tools, list) and pending_tools:
        tools = "; ".join(
            str((t.get("text") if isinstance(t, Mapping) else "") or "") for t in pending_tools
        ).strip("; ")
        if tools:
            lines.append(f"未完成工具：{tools}")
    if isinstance(pending_approvals, list) and pending_approvals:
        approvals = "; ".join(
            str((a.get("text") if isinstance(a, Mapping) else "") or "") for a in pending_approvals
        ).strip("; ")
        if approvals:
            lines.append(f"待审批：{approvals}")
    if not lines:
        return ""
    body = "\n".join(lines)
    return f"<working_state>\n{body}\n</working_state>"


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
    if prepared.resume_input is not None:
        return
    memory_rollout = str(prepared.memory_write_rollout or "").strip().lower()
    if memory_rollout in {"off", "shadow"}:
        return
    if not memory_rollout and not _ltm_auto_save_enabled():
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
