from __future__ import annotations

import time
import uuid
from typing import Any, Callable, Mapping, Optional, Sequence, cast

from fastapi import HTTPException

from ksadk.conversations.context import (
    canonical_event_type,
)
from ksadk.conversations.run_kinds import (
    RUN_MODE_UNKNOWN,
    RUN_TRIGGER_UNKNOWN,
    validate_run_mode,
    validate_run_trigger,
)
from ksadk.conversations.runtime_constants import (
    EVENT_SCAN_PAGE_SIZE,
)
from ksadk.conversations.runtime_observability import _extract_deferred_tool_names
from ksadk.sessions import Session, SessionEvent, resolve_session_service


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


async def _find_latest_session_event(
    service: Any,
    session_id: str,
    predicate: Callable[[SessionEvent], bool],
) -> SessionEvent | None:
    total = await service.count_events(session_id)
    offset = 0
    while offset < total:
        page = await service.get_events(
            session_id,
            offset=offset,
            limit=min(EVENT_SCAN_PAGE_SIZE, total - offset),
        )
        if not page:
            return None
        for event in reversed(page):
            if predicate(event):
                return cast(SessionEvent, event)
        offset += len(page)
    return None


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
            existing = await _find_latest_session_event(
                service,
                session_id,
                lambda event: (
                    event.event_type == "run_status"
                    and event.invocation_id == invocation_id
                    and str(
                        (event.metadata or {}).get("status")
                        or (event.content or {}).get("status")
                        or ""
                    )
                    == status
                ),
            )
            if existing is not None:
                return existing
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
    existing = await _find_latest_session_event(
        service,
        session_id,
        lambda event: (
            event.event_type == "run_checkpoint"
            and str((event.metadata or {}).get("run_id") or "") == str(run_id)
            and str((event.metadata or {}).get("checkpoint_id") or "") == str(checkpoint_id)
            and str((event.metadata or {}).get("framework") or "") == str(framework)
        ),
    )
    if existing is not None:
        return existing

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
    metadata: Optional[Mapping[str, Any]] = None,
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
        metadata={"reasoning": reasoning_text, **dict(metadata or {})},
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
