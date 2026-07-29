from __future__ import annotations

import uuid
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from ksadk.conversations.compaction_pipeline import build_working_set_metadata, run_pipeline
from ksadk.conversations.context import (
    TRANSCRIPT_EVENT_TYPES,
    canonical_event_type,
    compacted_until_seq_id,
    extract_event_text,
    group_events_by_api_round,
)
from ksadk.conversations.model_context import (
    estimate_text_tokens,
    get_auto_compact_threshold_percentage,
    get_auto_compact_threshold_tokens,
)
from ksadk.conversations.runtime_constants import (
    AUTOCOMPACT_KEEP_TAIL_GROUPS,
    PTL_RETRY_KEEP_TAIL_GROUPS,
)
from ksadk.conversations.runtime_metadata import (
    _build_pending_user_event,
    _latest_user_turn,
    _normalized_conversation_messages,
    _resolve_effective_attachment_context,
    _resolve_model_metadata,
    _resolve_runtime_model_metadata,
    _transcript_event_type,
)
from ksadk.conversations.runtime_payloads import CompactionPlan
from ksadk.conversations.runtime_persistence import append_context_checkpoint_event
from ksadk.conversations.semantic_summary import (
    extract_pinned_state,
    find_pinned_group_indexes,
    summarize_compaction,
)
from ksadk.sessions import SessionEvent, resolve_session_service


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
