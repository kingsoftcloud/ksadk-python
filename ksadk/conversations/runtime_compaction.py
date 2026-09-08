from __future__ import annotations

import asyncio
import logging
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
    get_auto_compact_hard_limit_tokens,
    get_auto_compact_soft_limit_tokens,
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
    extract_working_state,
    find_pinned_group_indexes,
    summarize_compaction,
)
from ksadk.sessions import SessionEvent, resolve_session_service

logger = logging.getLogger(__name__)


def _plan_compaction(
    events: Sequence[SessionEvent],
    *,
    model: Optional[str] = None,
    model_metadata: Mapping[str, Any] | None = None,
    pending_events: Sequence[SessionEvent] | None = None,
    force: bool = False,
    keep_tail_groups: int | None = None,
    prompt_integration_mode: str = "",
    compaction_owner: str = "",
) -> CompactionPlan:
    """根据当前 transcript 计算是否需要做 checkpoint compaction。

    ``compaction_owner=="ksadk"`` 时走双阈值（soft 50% / hard ~84%），命中且
    ``len(groups) > tail_groups`` 时触发 proactive compact（soft=整理，hard=强制止血）。
    未显式提供 owner 的旧调用继续以 ``ksadk_hosted`` 作为兼容判据。
    ``force=True``（PTL）始终绕过阈值，``trigger_band="emergency"``。

    compaction_owner 硬门控（方案 §6.2）：native/framework owner 不运行 KsADK 第二套
    压缩；framework-assisted Runner 可显式声明 owner=ksadk 使用平台压缩。
    """

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
    # 是否启用双阈值由 compaction ownership 决定，而不是由 Prompt 接管模式决定。
    # framework-assisted LangGraph 也可以把压缩明确交给 KsADK；native/framework
    # owner 则继续使用各自原生机制，避免双重压缩。
    is_ksadk_hosted = compaction_owner == "ksadk" or (
        not compaction_owner and prompt_integration_mode == "ksadk_hosted"
    )
    soft_limit_tokens = (
        get_auto_compact_soft_limit_tokens(resolved_model_metadata) if is_ksadk_hosted else None
    )
    hard_limit_tokens = (
        get_auto_compact_hard_limit_tokens(resolved_model_metadata) if is_ksadk_hosted else None
    )

    # 触发带判定。force（PTL）优先 → emergency，绕过阈值。
    groups_enough = len(groups) > tail_groups
    if force:
        trigger_band = "emergency"
        should_by_threshold = True
    elif is_ksadk_hosted and soft_limit_tokens is not None and hard_limit_tokens is not None:
        # 双阈值：hard 优先于 soft。二者都需 groups 充足，否则 none（避免每轮压缩）。
        if not groups_enough:
            trigger_band = "none"
            should_by_threshold = False
        elif total_estimated_tokens > hard_limit_tokens:
            trigger_band = "hard"
            should_by_threshold = True
        elif total_estimated_tokens > soft_limit_tokens:
            trigger_band = "soft"
            should_by_threshold = True
        else:
            trigger_band = "none"
            should_by_threshold = False
    else:
        # 非 ksadk_hosted → 旧单阈值。trigger_band="" 表示门控未启用（旧路径）。
        trigger_band = ""
        should_by_threshold = total_estimated_tokens > auto_compact_threshold_tokens

    # early-return：groups 不足或（非 force 且未超阈值）。保留 len(groups)<=tail_groups 早退，
    # 避免每轮压缩。force 仍绕过此早退的阈值部分，但 groups 不足时 force 也无可压缩（下方）。
    if not force and (len(groups) <= tail_groups or not should_by_threshold):
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
            soft_limit_tokens=soft_limit_tokens,
            hard_limit_tokens=hard_limit_tokens,
            trigger_band=trigger_band,
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
            soft_limit_tokens=soft_limit_tokens,
            hard_limit_tokens=hard_limit_tokens,
            trigger_band=trigger_band,
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
        soft_limit_tokens=soft_limit_tokens,
        hard_limit_tokens=hard_limit_tokens,
        trigger_band=trigger_band,
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
    prompt_integration_mode: str = "",
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
        prompt_integration_mode=prompt_integration_mode,
    )


def _working_state_from_checkpoint(checkpoint: Any) -> Any:
    """§8.1：从上一个 context_checkpoint 事件解析 WorkingState（用于缺失字段合并）。

    checkpoint metadata 由 compact_conversation_history 写入（仅 ksadk_hosted）。无 checkpoint
    或无 working_state 键时返回 None。重建的 WorkingState 仅用于回填 current_goal/constraints
    等关键字段，不恢复 pending_tools/approvals（那些以事实事件为准）。
    """
    if checkpoint is None:
        return None
    meta = getattr(checkpoint, "metadata", None) or {}
    ws_audit = meta.get("working_state")
    if not isinstance(ws_audit, dict):
        return None
    from ksadk.conversations.semantic_summary import WorkingState

    return WorkingState(
        current_goal=str(ws_audit.get("current_goal") or ""),
        next_action=ws_audit.get("next_action"),
        completed_steps=list(ws_audit.get("completed_steps") or []),
        constraints=list(ws_audit.get("constraints") or []),
        source_seq_range=tuple(ws_audit.get("source_seq_range") or (0, 0)),  # type: ignore[arg-type]
    )


async def _maybe_memory_flush(
    events: Sequence[SessionEvent],
    *,
    user_id: str = "",
    agent_id: str = "",
) -> dict[str, Any] | None:
    """压缩前 best-effort Memory Flush（方案 §9.2）。

    门控：``KSADK_MEMORY_FLUSH_ENABLED``（默认关，避免在无 Memory Provider 时改变行为）+
    ``KSADK_MEMORY_FLUSH_BEFORE_COMPACTION``（默认开，但需前者总开关）。提取候选交
    ``MemoryCoordinator.flush_candidates``，Policy 决定 commit/reject。失败返回 ``failed``，不抛。
    """
    import os as _os

    if _os.environ.get("KSADK_MEMORY_FLUSH_ENABLED", "").strip().lower() not in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return None
    try:
        from ksadk.context_engine.policies import ContextPolicy
        from ksadk.memory.coordinator import MemoryCoordinator
        from ksadk.memory.extraction import propose_memory_candidates
        from ksadk.memory.providers.local_sqlite import resolve_default_memory_provider

        policy = ContextPolicy.from_env()
        if not policy.compaction.flush_memory_before_compaction:
            return None
        # 持久化 Memory Provider（替换临时 :memory:，方案 §10/§12）。云端应经
        # LongTermMemoryService/HTTP/SDK Provider；本地默认 SQLite 文件库。
        provider = resolve_default_memory_provider()
        coordinator = MemoryCoordinator(provider)
        from ksadk.memory.coordinator import agent_user_scope_id

        _scope_id = agent_user_scope_id(agent_id=agent_id, user_id=str(user_id or ""))
        candidates = propose_memory_candidates(list(events), scope="user", scope_id=_scope_id)
        if not candidates:
            return {"status": "skipped", "proposed": 0, "committed": 0, "rejected": 0}
        result = coordinator.flush_candidates(candidates)
        return result.to_audit_dict()
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory flush failed: %s", exc)
        return {"status": "failed", "error": str(exc), "proposed": 0, "committed": 0, "rejected": 0}


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
    prompt_integration_mode: str = "",
    compaction_owner: str = "",
) -> SessionEvent | None:
    """把旧轮次折叠为 checkpoint。

    这是本地版的 compaction：先按 API round 分组，再保留尾部若干轮，把更早
    的部分压成 append-only summary 事件。force=True 时用于 PTL 恢复。

    compaction_owner 硬门控（方案 §6.2）：非 ksadk 时不走 KsADK 双阈值压缩。
    """
    provider = session_service_provider or resolve_session_service
    service = provider()
    events = await service.get_events(session_id)
    session = await service.get_session(session_id)
    memory_user_id = str(getattr(session, "user_id", "") or "")
    plan = _plan_compaction(
        events,
        model=model,
        model_metadata=model_metadata,
        force=force,
        keep_tail_groups=keep_tail_groups,
        prompt_integration_mode=prompt_integration_mode,
        compaction_owner=compaction_owner,
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

    # PR D2：Session Working State（仅 ksadk_hosted）。从事实事件确定性提取，
    # 写进 checkpoint metadata 供下一轮门控重注入。非门控不写（向后兼容）。
    working_state_audit: dict[str, Any] | None = None
    # PR D2.5：Memory Flush（方案 §9.2）。仅 ksadk_hosted + policy 开启时，压缩前 best-effort
    # 提取候选并提交；失败不阻止 compaction（§9.2 失败语义）。非门控不执行（向后兼容）。
    memory_flush_audit: dict[str, Any] | None = None
    if (
        compaction_owner == "ksadk"
        or (not compaction_owner and prompt_integration_mode == "ksadk_hosted")
    ) and plan.groups_to_compact:
        compacted_events = [event for group in plan.groups_to_compact for event in group]
        seq_range = (
            int(plan.groups_to_compact[0][0].seq_id or 0),
            int(plan.groups_to_compact[-1][-1].seq_id or 0),
        )
        working_state = extract_working_state(
            compacted_events,
            pinned_state=plan.pinned_state,
            summary_text=summary_result.summary_text,
            source_seq_range=seq_range,
        )
        # §8.1：关键字段缺失时用压缩前 checkpoint 的 WorkingState 合并，不接受空值覆盖。
        previous_ws = _working_state_from_checkpoint(latest_checkpoint)
        working_state.merge_missing_from(previous_ws)
        working_state_audit = working_state.to_audit_dict()
        memory_flush_audit = await _maybe_memory_flush(
            compacted_events, user_id=memory_user_id, agent_id=author
        )

    # PR D2.6：per-session compaction lock + stale guard（方案 §9.6）。同一 session 同时只
    # 允许一个 checkpoint/WorkingState 提交；拿不到锁则放弃本次提交避免并发覆盖。
    _checkpoint_kwargs = dict(
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
            # PR D1：双阈值带标记（""=非门控旧路径 / "soft" / "hard" / "emergency"=PTL）。
            # 仅审计用，不改变既有 trigger 字段；trigger 仍为调用方值。
            "trigger_band": plan.trigger_band,
            # PR D2：Session Working State（仅 ksadk_hosted）。结构化工作面，供下一轮门控重注入。
            # 含 content_hash/source_seq_range/status，无 prompt 明文。非门控不写该键（向后兼容）。
            **({"working_state": working_state_audit} if working_state_audit is not None else {}),
            # PR D2.5：Memory Flush 审计（方案 §9.2）。失败不阻止 compaction。
            **({"memory_flush": memory_flush_audit} if memory_flush_audit is not None else {}),
            # PR D2：tokens_by_kind before/after（从 pipeline stats 取，审计用）。
            "tokens_by_kind_before": {"transcript": pipeline_result["tokens_before"]},
            "tokens_by_kind_after": {"transcript": pipeline_result["tokens_after"]},
        },
        session_service_provider=provider,
    )
    try:
        from ksadk.conversations.session_lock import session_compaction_lock
    except Exception:  # noqa: BLE001
        session_compaction_lock = None  # type: ignore[assignment]
    if session_compaction_lock is None:
        return await append_context_checkpoint_event(**_checkpoint_kwargs)
    try:
        async with session_compaction_lock(session_id):
            return await append_context_checkpoint_event(**_checkpoint_kwargs)
    except asyncio.TimeoutError:
        logger.warning(
            "session compaction lock timeout for %s; skipping checkpoint commit",
            session_id,
        )
        return None
