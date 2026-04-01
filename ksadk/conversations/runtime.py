from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, Optional, Sequence

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
from ksadk.conversations.normalize import compact_attachment_for_session, normalize_kop_messages
from ksadk.conversations.semantic_summary import (
    extract_pinned_state,
    find_pinned_group_indexes,
    summarize_compaction,
)
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
    compaction_triggered: bool = False
    compaction_trigger: str | None = None
    compacted_until_seq_id: int | None = None


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


def build_responses_payload(*, output_text: str, model: Optional[str], session_id: str) -> dict[str, Any]:
    response_id = f"resp_{uuid.uuid4().hex}"
    created_at = int(time.time())
    message_id = f"msg_{uuid.uuid4().hex[:12]}"
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": "completed",
        "model": model or "agent",
        "output": [
            {
                "id": message_id,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": output_text}],
            }
        ],
        "output_text": output_text,
        "session_id": session_id,
    }


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


def _resolve_model_metadata(model: Optional[str]) -> dict[str, Any]:
    """统一收口模型上下文配置。

    当前阶段还没把远端 /v1/models 的完整 metadata 缓存接进 runtime，
    所以这里只用默认值 + model id。后续模型目录接口上线 richer metadata
    后，只需要把这层改成真正的 resolver，compaction 逻辑本身不用再动。
    """

    return normalize_model_metadata({"id": model or "agent"})


def _normalized_conversation_messages(messages: Sequence[Dict[str, Any]]) -> list[dict[str, Any]]:
    """把不同入口的 message 形态收敛成统一内部格式。"""

    normalized_messages: list[dict[str, Any]] = []
    for message in list(messages or []):
        if isinstance(message, dict) and any(
            key in message for key in ("display_content", "attachments", "parts")
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
                }
            )
            continue
        normalized_messages.extend(normalize_kop_messages([message]))
    return normalized_messages


def _latest_user_turn(
    normalized_messages: Sequence[Dict[str, Any]],
) -> tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]:
    latest_user_message = next(
        (message for message in reversed(normalized_messages) if message.get("role") == "user"),
        {},
    )
    user_input = str(latest_user_message.get("content") or "")
    user_display_input = str(latest_user_message.get("display_content") or user_input)
    user_parts = list(latest_user_message.get("parts") or [])
    attachments = list(latest_user_message.get("attachments") or [])
    return user_input, user_display_input, user_parts, attachments


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
            },
            "stateDelta": {},
        },
        session_id=session_id,
    )


def _plan_compaction(
    events: Sequence[SessionEvent],
    *,
    model: Optional[str] = None,
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
    model_metadata = _resolve_model_metadata(model)
    auto_compact_threshold_tokens = get_auto_compact_threshold_tokens(model_metadata)
    auto_compact_threshold_percentage = get_auto_compact_threshold_percentage(model_metadata)
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
    user_input, user_display_input, _, attachments = _latest_user_turn(normalized_messages)
    pending_event = _build_pending_user_event(
        session_id=session_id,
        invocation_id=f"preview-{uuid.uuid4()}",
        user_input=user_input,
        user_display_input=user_display_input or user_input,
        attachments=attachments,
    )
    events = await service.get_events(session_id)
    return _plan_compaction(events, model=model, pending_events=[pending_event])


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
    model_metadata = _resolve_model_metadata(model)
    summary_result = await summarize_compaction(
        groups_to_compact=plan.groups_to_compact,
        previous_summary=previous_summary,
        pinned_state=plan.pinned_state,
        model_metadata=model_metadata,
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
    state_delta: Optional[dict[str, Any]] = None,
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

    normalized_messages = _normalized_conversation_messages(messages)
    user_input, user_display_input, user_parts, attachments = _latest_user_turn(normalized_messages)

    await append_conversation_event(
        session_id=resolved_session_id,
        author="user",
        role="user",
        text=user_display_input or user_input,
        invocation_id=resolved_invocation_id,
        event_type="user_message",
        state_delta=state_delta or {},
        session_service_provider=provider,
        metadata={
            "agent_input": user_input,
            "attachments": [compact_attachment_for_session(item) for item in attachments if item],
        },
    )

    checkpoint = await compact_conversation_history(
        session_id=resolved_session_id,
        author=agent_id,
        invocation_id=resolved_invocation_id,
        model=model,
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
        attachments=attachments,
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
    state_delta: Optional[dict[str, Any]] = None,
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
        state_delta=state_delta,
        session_service_provider=provider,
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
            result = await runner.invoke(
                {
                    "input": prepared.user_input,
                    "history": prepared.history,
                    "input_parts": prepared.user_parts,
                    "attachments": prepared.attachments,
                    "model": model,
                }
            )
            break
        except Exception as exc:
            if attempt == 0 and _is_prompt_too_long_error(exc):
                checkpoint = await compact_conversation_history(
                    session_id=prepared.session_id,
                    author=runner_name,
                    invocation_id=prepared.invocation_id,
                    model=model,
                    force=True,
                    trigger="prompt_too_long",
                    keep_tail_groups=PTL_RETRY_KEEP_TAIL_GROUPS,
                    session_service_provider=provider,
                )
                if checkpoint:
                    prepared = await _refresh_history(prepared, session_service_provider=provider)
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
        session_service_provider=provider,
    )
    await append_run_status_event(
        session_id=prepared.session_id,
        author=runner_name,
        status="completed",
        invocation_id=prepared.invocation_id,
        session_service_provider=provider,
    )
    return prepared.session_id, {"output_text": output_text, "model": model}


async def stream_conversation_turn(
    *,
    runner: Any,
    agent_id: str,
    user_id: str,
    session_id: Optional[str],
    messages: Sequence[Dict[str, Any]],
    model: Optional[str],
    prepare_runner: Callable[[Any, Optional[str]], None],
    state_delta: Optional[dict[str, Any]] = None,
    session_service_provider: Callable[[], Any] | None = None,
) -> AsyncIterator[str]:
    """流式 turn 编排入口。

    这里既负责对外输出 SSE，也负责把 tool/approval/assistant 最终结果回写到
    transcript，保证本地 `/v1/responses`、`/v1/chat/completions` 和 KOP
    RunAgent 用的是同一条 conversation path。
    """
    provider = session_service_provider or resolve_session_service
    prepare_runner(runner, model)
    compaction_preview = await preview_auto_compaction(
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        messages=messages,
        model=model,
        session_service_provider=provider,
    )
    if compaction_preview.should_compact:
        yield build_compaction_sse_event(
            phase="start",
            trigger="auto",
            total_chars=compaction_preview.total_chars,
            total_estimated_tokens=compaction_preview.total_estimated_tokens,
            group_count=compaction_preview.group_count,
            threshold_percentage=compaction_preview.auto_compact_threshold_percentage,
        )
    prepared = await build_run_input(
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        messages=messages,
        model=model,
        state_delta=state_delta,
        session_service_provider=provider,
    )
    if prepared.compaction_triggered:
        yield build_compaction_sse_event(
            phase="done",
            trigger=str(prepared.compaction_trigger or "auto"),
            compacted_until_seq_id=prepared.compacted_until_seq_id,
            total_chars=compaction_preview.total_chars if compaction_preview.should_compact else None,
            total_estimated_tokens=compaction_preview.total_estimated_tokens
            if compaction_preview.should_compact
            else None,
            group_count=compaction_preview.group_count if compaction_preview.should_compact else None,
            threshold_percentage=compaction_preview.auto_compact_threshold_percentage
            if compaction_preview.should_compact
            else None,
        )
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
    for attempt in range(2):
        try:
            async for chunk in runner.stream(
                {
                    "input": prepared.user_input,
                    "history": prepared.history,
                    "input_parts": prepared.user_parts,
                    "attachments": prepared.attachments,
                    "model": model,
                }
            ):
                chunk_type = chunk.get("type")
                if chunk_type == "thinking":
                    delta = str(chunk.get("delta", ""))
                    if delta:
                        emitted_anything = True
                        yield f"event: response.reasoning.delta\ndata: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"
                    continue
                if chunk_type == "text":
                    delta = str(chunk.get("delta", ""))
                    if delta:
                        accumulated_text += delta
                        emitted_anything = True
                        yield f"event: response.output_text.delta\ndata: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"
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
                    yield (
                        "event: response.tool_call\n"
                        f"data: {json.dumps({'name': chunk.get('tool_name'), 'args': chunk.get('tool_args', {}), 'run_id': chunk.get('run_id')}, ensure_ascii=False)}\n\n"
                    )
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
                    yield (
                        "event: response.tool_result\n"
                        f"data: {json.dumps({'name': chunk.get('tool_name'), 'output': chunk.get('tool_output', ''), 'run_id': chunk.get('run_id')}, ensure_ascii=False)}\n\n"
                    )
                    continue
                if chunk_type == "interrupt":
                    await append_conversation_event(
                        session_id=prepared.session_id,
                        author=runner_name,
                        role="model",
                        text="approval requested",
                        invocation_id=prepared.invocation_id,
                        event_type="approval_request",
                        metadata={"interrupt_info": chunk.get("interrupt_info")},
                        session_service_provider=provider,
                    )
                    emitted_anything = True
                    yield (
                        "event: response.approval_request\n"
                        f"data: {json.dumps({'interrupt_info': chunk.get('interrupt_info')}, ensure_ascii=False)}\n\n"
                    )
                    continue
                if chunk_type == "final":
                    final_text = str(chunk.get("output", ""))
                    if final_text:
                        accumulated_text = final_text
            break
        except Exception as exc:
            if attempt == 0 and not emitted_anything and _is_prompt_too_long_error(exc):
                yield build_compaction_sse_event(
                    phase="start",
                    trigger="prompt_too_long",
                )
                checkpoint = await compact_conversation_history(
                    session_id=prepared.session_id,
                    author=runner_name,
                    invocation_id=prepared.invocation_id,
                    model=model,
                    force=True,
                    trigger="prompt_too_long",
                    keep_tail_groups=PTL_RETRY_KEEP_TAIL_GROUPS,
                    session_service_provider=provider,
                )
                if checkpoint:
                    yield build_compaction_sse_event(
                        phase="done",
                        trigger="prompt_too_long",
                        compacted_until_seq_id=int(
                            (checkpoint.metadata or {}).get("compacted_until_seq_id") or 0
                        )
                        or None,
                    )
                    prepared = await _refresh_history(prepared, session_service_provider=provider)
                    continue
            await append_run_status_event(
                session_id=prepared.session_id,
                author=runner_name,
                status="failed",
                invocation_id=prepared.invocation_id,
                detail=str(exc),
                session_service_provider=provider,
            )
            yield (
                "event: response.error\n"
                f"data: {json.dumps({'message': str(exc) or 'Agent 运行失败'}, ensure_ascii=False)}\n\n"
            )
            return

    await append_conversation_event(
        session_id=prepared.session_id,
        author=runner_name,
        role="model",
        text=accumulated_text,
        invocation_id=prepared.invocation_id,
        event_type="assistant_message",
        session_service_provider=provider,
    )
    await append_run_status_event(
        session_id=prepared.session_id,
        author=runner_name,
        status="completed",
        invocation_id=prepared.invocation_id,
        session_service_provider=provider,
    )
    final_payload = build_responses_payload(
        output_text=accumulated_text,
        model=model,
        session_id=prepared.session_id,
    )
    yield f"event: response.completed\ndata: {json.dumps(final_payload, ensure_ascii=False)}\n\n"
