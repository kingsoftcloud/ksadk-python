"""Session event projection, pagination, and checkpoint policy helpers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from datetime import datetime, timezone
from typing import Any, Optional, cast

from fastapi import HTTPException

from ksadk.conversations.session_title import (
    HEURISTIC_SESSION_TITLE_SOURCE,
    build_fallback_title,
    build_heuristic_title,
)
from ksadk.events.runtime_event import EventType
from ksadk.server.factory import get_runtime_execution, get_state
from ksadk.sessions import Session, SessionEvent

from . import dependencies as deps
from .common import _sanitize_session_state_for_action
from .models import (
    _EVENT_SCAN_PAGE_SIZE,
    _latest_session_run_metadata,
    _parse_iso_datetime,
    _session_topic_from_events,
    _session_user_prompt_from_event,
    _truncate_session_text,
)

logger = logging.getLogger(__name__)


async def _require_action_session(
    service,
    *,
    session_id: str,
    agent_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Session:
    """Resolve one runtime session while enforcing every supplied scope."""

    session = await service.get_session_metadata(str(session_id or "").strip())
    if (
        session is None
        or (agent_id is not None and session.agent_id != agent_id)
        or (user_id is not None and session.user_id != user_id)
    ):
        raise HTTPException(status_code=404, detail="Session not found")
    return cast(Session, session)


async def _session_to_action_payload(session: Session) -> dict[str, Any]:
    events = list(session.events or [])
    if not events:
        try:
            events = await deps.resolve_session_service().get_events(session.id)
        except Exception as exc:
            logger.debug("Failed to hydrate events for session %s: %s", session.id, exc)
            events = []
    event_prompts = [
        _session_user_prompt_from_event(event)
        for event in events
        if event.event_type == "user_message"
    ]
    event_prompts = [prompt for prompt in event_prompts if prompt]
    first_prompt = session.first_prompt or (event_prompts[0] if event_prompts else "")
    last_prompt = session.last_prompt or (event_prompts[-1] if event_prompts else "")
    (
        active_invocation_id,
        active_run_status,
        active_run_mode,
        active_run_trigger,
    ) = _latest_session_run_metadata(events)
    title = session.title
    title_source = session.title_source
    if not title:
        title_seed = _session_topic_from_events(events) or first_prompt
        if title_seed:
            title = build_fallback_title(title_seed)
            title_source = "fallback_first_prompt"
    if title_source == "fallback_first_prompt":
        heuristic = build_heuristic_title(
            first_prompt=first_prompt or title,
            assistant_text=session.summary or "",
        )
        if heuristic and heuristic != title:
            title = heuristic
            title_source = HEURISTIC_SESSION_TITLE_SOURCE
    payload = {
        "SessionId": session.id,
        "AgentId": session.agent_id,
        "UserId": session.user_id,
        "Title": title,
        "TitleSource": title_source,
        "Summary": session.summary,
        "FirstPrompt": _truncate_session_text(first_prompt),
        "LastPrompt": _truncate_session_text(last_prompt),
        "ActiveInvocationId": active_invocation_id,
        "ActiveRunStatus": active_run_status,
        "ActiveRunMode": active_run_mode,
        "ActiveRunTrigger": active_run_trigger,
        "State": _sanitize_session_state_for_action(session.state),
        "CreatedAt": session.created_at,
        "UpdatedAt": session.updated_at,
        "Version": session.version,
        "Continuity": _runtime_continuity_payload(),
    }
    return payload


def _runtime_continuity_payload() -> dict[str, Any]:
    """Describe continuity from the active RuntimeAdapter capability contract."""

    state = get_state()
    if state.executor is None or state.launch_context is None:
        # Session storage is independently useful in route-manifest and
        # control-plane-only apps. Do not make a metadata projection require a
        # live runtime execution binding.
        return {
            "Level": "semantic",
            "Path": "replay",
            "Runtime": "unbound",
            "Details": {
                "CheckpointSupported": False,
                "Reason": "RuntimeAdapter is not bound to this app",
            },
        }
    executor, launch_context = get_runtime_execution()
    capabilities = executor.native_capabilities(launch_context)
    continuity = capabilities.get("SessionContinuity")
    continuity = continuity if isinstance(continuity, Mapping) else {}
    checkpoint = capabilities.get("Checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, Mapping) else {}
    level = str(continuity.get("Level") or "semantic").strip().lower()
    if level not in {"ui_only", "semantic", "runtime", "exact"}:
        level = "semantic"
    return {
        "Level": level,
        "Path": "checkpoint" if checkpoint.get("Supported") else "replay",
        "Runtime": launch_context.runtime_type,
        "Details": {
            "CheckpointSupported": bool(checkpoint.get("Supported")),
            "Reason": str(continuity.get("Reason") or ""),
        },
    }


def _event_to_action_payload(event: SessionEvent) -> dict[str, Any]:
    payload = {
        "EventId": event.id,
        "SessionId": event.session_id,
        "Author": event.author,
        "EventType": event.event_type,
        "Content": event.content,
        "Timestamp": event.timestamp,
        "SeqId": event.seq_id,
        "Metadata": event.metadata,
    }
    if event.invocation_id:
        payload["InvocationId"] = event.invocation_id
    return payload


async def _iter_with_idle_heartbeat(source: AsyncIterator[Any]):
    """转发 source 的 chunk，空闲超时发 ``: ping`` 而不取消 source 迭代器。

    直接用 ``asyncio.wait_for(source.__anext__(), timeout=...)`` 会在超时时 cancel
    掉正在进行的 ``__anext__()``；若 source 是原生 async generator，cancel 会触发
    GeneratorExit 把它关闭，下一轮 ``__anext__`` 抛 StopAsyncIteration → 流提前断。
    用 pump task + queue 隔离：pump 独立消费 source，主循环只对 queue.get() 计时，
    超时时发心跳而不碰 source。
    """
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)

    async def pump() -> None:
        async for chunk in source:
            await queue.put(chunk)

    pump_task = asyncio.create_task(pump())
    get_task: asyncio.Task[Any] | None = None
    try:
        while True:
            # Drain queued data before observing producer completion so a final
            # chunk is never lost when the producer exits immediately after it.
            if not queue.empty():
                yield ("chunk", queue.get_nowait())
                continue
            if pump_task.done():
                pump_task.result()
                return

            get_task = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait(
                {get_task, pump_task},
                timeout=deps.heartbeat_interval(),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if get_task in done:
                chunk = get_task.result()
                get_task = None
                yield ("chunk", chunk)
                continue
            if pump_task in done:
                # The final queue.put() may have completed in the same loop turn
                # as the producer. Let the pending getter consume it first.
                if not queue.empty():
                    chunk = await get_task
                    get_task = None
                    yield ("chunk", chunk)
                    continue
                get_task.cancel()
                await asyncio.gather(get_task, return_exceptions=True)
                get_task = None
                pump_task.result()
                return

            get_task.cancel()
            await asyncio.gather(get_task, return_exceptions=True)
            get_task = None
            yield ("heartbeat", None)
    finally:
        if get_task is not None and not get_task.done():
            get_task.cancel()
            await asyncio.gather(get_task, return_exceptions=True)
        if not pump_task.done():
            pump_task.cancel()
        await asyncio.gather(pump_task, return_exceptions=True)


def _checkpoint_event_to_action_payload(event: SessionEvent) -> dict[str, Any] | None:
    if event.event_type == "run_checkpoint":
        metadata = event.metadata or {}
    elif event.event_type == EventType.CHECKPOINT_CREATED:
        content = event.content or {}
        payload = content.get("payload") if isinstance(content, Mapping) else {}
        if not isinstance(payload, Mapping):
            return None
        framework_ref = payload.get("framework_ref") or payload.get("resume_target") or {}
        if not isinstance(framework_ref, Mapping):
            framework_ref = {}
        framework = str(payload.get("framework") or "").strip()
        if not framework and len(framework_ref) == 1:
            framework = str(next(iter(framework_ref)))
        capability = payload.get("capability")
        capability = capability if isinstance(capability, Mapping) else {}
        metadata = {
            **dict(event.metadata or {}),
            "run_id": str(payload.get("run_id") or event.invocation_id or ""),
            "checkpoint_id": str(payload.get("checkpoint_id") or ""),
            "framework": framework,
            "framework_ref": dict(framework_ref),
            "backend": str(payload.get("backend") or capability.get("backend") or "unknown"),
            "scope": str(payload.get("scope") or capability.get("scope") or "unknown"),
            "durable": bool(payload.get("durable", capability.get("durable", False))),
            "is_resumable": bool(payload.get("is_resumable", True)),
        }
    else:
        return None
    run_id = str(metadata.get("run_id") or "").strip()
    checkpoint_id = str(metadata.get("checkpoint_id") or "").strip()
    framework = str(metadata.get("framework") or "").strip()
    framework_ref = metadata.get("framework_ref")
    if not run_id or not checkpoint_id or not framework or not isinstance(framework_ref, Mapping):
        return None
    next_node = str(metadata.get("next_node") or "").strip()
    if not next_node:
        langgraph_ref = framework_ref.get("langgraph")
        if isinstance(langgraph_ref, Mapping):
            next_node = str(langgraph_ref.get("next_node") or "").strip()
    is_terminal = bool(metadata.get("is_terminal", False))
    is_resumable_raw = metadata.get("is_resumable")
    is_resumable = is_resumable_raw if isinstance(is_resumable_raw, bool) else None
    backend = str(metadata.get("backend") or "unknown").strip() or "unknown"
    scope = str(metadata.get("scope") or "unknown").strip() or "unknown"
    durable = bool(metadata.get("durable", False))
    disabled_reason = str(metadata.get("resume_disabled_reason") or "").strip()
    if is_terminal:
        is_resumable = False
        disabled_reason = disabled_reason or "该 checkpoint 已是终态；可选择更早恢复点重跑"
    if backend == "memory" or scope == "process_local":
        is_resumable = False
        disabled_reason = disabled_reason or "进程内 checkpoint 不能跨实例恢复"
    resume_status = str(metadata.get("resume_status") or "").strip()
    if not resume_status:
        if is_resumable is True:
            resume_status = "resumable"
        elif is_resumable is False:
            resume_status = "disabled"
        else:
            resume_status = "unknown"
    if resume_status == "disabled" and not disabled_reason:
        disabled_reason = "该 checkpoint 不可恢复"
    artifact_preview = metadata.get("artifact_preview")
    if not isinstance(artifact_preview, Mapping):
        artifact_preview = {}
    resume_count_raw = metadata.get("resume_count")
    try:
        resume_count = int(resume_count_raw) if resume_count_raw is not None else 0
    except (TypeError, ValueError):
        resume_count = 0
    last_resumed_at = metadata.get("last_resumed_at")
    replay_allowed_raw = metadata.get("replay_allowed")
    replay_allowed = replay_allowed_raw if isinstance(replay_allowed_raw, bool) else True
    expires_at = metadata.get("expires_at")
    checkpoint_status = str(metadata.get("checkpoint_status") or "").strip()
    if not checkpoint_status:
        if is_terminal:
            checkpoint_status = "resumed" if resume_count else "terminal"
        elif is_resumable is False:
            checkpoint_status = "disabled"
        else:
            checkpoint_status = "active"
    payload = {
        "EventId": event.id,
        "SessionId": event.session_id,
        "InvocationId": event.invocation_id,
        "SeqId": event.seq_id,
        "Timestamp": event.timestamp,
        "RunId": run_id,
        "CheckpointId": checkpoint_id,
        "Framework": framework,
        "FrameworkRef": dict(framework_ref),
        "Phase": str(metadata.get("phase") or ""),
        "Metadata": metadata,
        "IsResumable": is_resumable,
        "ResumeStatus": resume_status,
        "IsTerminal": is_terminal,
        "ResumeDisabledReason": disabled_reason,
        "NextNode": next_node,
        "StageKey": str(metadata.get("stage_key") or ""),
        "StageName": str(
            metadata.get("stage_name") or metadata.get("stage") or metadata.get("title") or ""
        ),
        "StageIndex": metadata.get("stage_index"),
        "TotalStages": metadata.get("total_stages"),
        "Backend": backend,
        "Scope": scope,
        "Durable": durable,
        "CreatedAt": event.timestamp,
        "ArtifactPreview": dict(artifact_preview),
        "LastResumedAt": last_resumed_at,
        "ResumeCount": resume_count,
        "ReplayAllowed": replay_allowed,
        "ExpiresAt": expires_at,
        "CheckpointStatus": checkpoint_status,
    }
    stage = str(metadata.get("stage") or metadata.get("title") or "").strip()
    summary = str(metadata.get("summary") or metadata.get("description") or "").strip()
    next_action = str(metadata.get("next_action") or metadata.get("nextAction") or "").strip()
    status = str(metadata.get("status") or "").strip()
    if stage:
        payload["Stage"] = stage
    if summary:
        payload["Summary"] = summary
    if next_action:
        payload["NextAction"] = next_action
    if status:
        payload["Status"] = status
    return payload


def _resume_audit_by_checkpoint(
    events: list[SessionEvent],
) -> dict[tuple[str, str], dict[str, Any]]:
    audit: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        if event.event_type != "run_resume":
            continue
        metadata = event.metadata or {}
        run_id = str(metadata.get("run_id") or "").strip()
        checkpoint_id = str(metadata.get("checkpoint_id") or "").strip()
        if not run_id or not checkpoint_id:
            continue
        key = (run_id, checkpoint_id)
        item = audit.setdefault(key, {"resume_count": 0, "last_resumed_at": None})
        item["resume_count"] = int(item["resume_count"]) + 1
        item["last_resumed_at"] = event.timestamp
    return audit


def _apply_checkpoint_resume_audit(
    checkpoint: dict[str, Any],
    audit_by_checkpoint: Mapping[tuple[str, str], Mapping[str, Any]],
) -> dict[str, Any]:
    audit = audit_by_checkpoint.get(
        (
            str(checkpoint.get("RunId") or ""),
            str(checkpoint.get("CheckpointId") or ""),
        ),
        {},
    )
    metadata = dict(checkpoint.get("Metadata") or {})
    resume_count = int(audit.get("resume_count") or checkpoint.get("ResumeCount") or 0)
    last_resumed_at = audit.get("last_resumed_at") or checkpoint.get("LastResumedAt")
    checkpoint["ResumeCount"] = resume_count
    checkpoint["LastResumedAt"] = last_resumed_at
    metadata["resume_count"] = resume_count
    metadata["last_resumed_at"] = last_resumed_at
    if resume_count and checkpoint.get("CheckpointStatus") in {"", "active"}:
        checkpoint["CheckpointStatus"] = "resumed"
    expires_at = checkpoint.get("ExpiresAt")
    expires_at_dt = _parse_iso_datetime(expires_at)
    if expires_at_dt is not None and expires_at_dt <= datetime.now(timezone.utc):
        checkpoint["IsResumable"] = False
        checkpoint["ResumeStatus"] = "disabled"
        checkpoint["CheckpointStatus"] = "expired"
        checkpoint["ResumeDisabledReason"] = "该 checkpoint 已过期"
    elif resume_count and checkpoint.get("ReplayAllowed") is False:
        checkpoint["IsResumable"] = False
        checkpoint["ResumeStatus"] = "disabled"
        checkpoint["ResumeDisabledReason"] = "该 checkpoint 已恢复过，当前策略不允许重复恢复"
    metadata["checkpoint_status"] = checkpoint.get("CheckpointStatus")
    metadata["resume_status"] = checkpoint.get("ResumeStatus")
    metadata["resume_disabled_reason"] = checkpoint.get("ResumeDisabledReason")
    checkpoint["Metadata"] = metadata
    return checkpoint


def _apply_adk_only_latest_resumable(
    checkpoints: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """P1.4: For ADK invocation_id resume mode, only the latest checkpoint per
    RunId is independently resumable. Older checkpoints get IsResumable=False."""
    latest_by_run: dict[str, int] = {}
    for cp in checkpoints:
        metadata = cp.get("Metadata") or {}
        if not metadata.get("only_latest_resumable"):
            continue
        run_id = str(cp.get("RunId") or "")
        seq_id = int(cp.get("SeqId") or 0)
        if run_id not in latest_by_run or seq_id > latest_by_run[run_id]:
            latest_by_run[run_id] = seq_id

    for cp in checkpoints:
        metadata = cp.get("Metadata") or {}
        if not metadata.get("only_latest_resumable"):
            continue
        run_id = str(cp.get("RunId") or "")
        seq_id = int(cp.get("SeqId") or 0)
        if seq_id < latest_by_run.get(run_id, 0):
            if cp.get("IsResumable") is True:
                cp["IsResumable"] = False
                cp["ResumeStatus"] = "disabled"
                cp["ResumeDisabledReason"] = "新的恢复点已生成，此恢复点暂停恢复能力"
                metadata["resume_disabled_reason"] = "新的恢复点已生成，此恢复点暂停恢复能力"
                metadata["resume_status"] = "disabled"
                cp["Metadata"] = metadata

    return checkpoints


def _check_adk_latest_resumable(
    checkpoint: dict[str, Any],
    events: list,
) -> dict[str, Any]:
    """P1.4: For a single ADK only_latest_resumable checkpoint, verify it is
    the latest for its RunId. If not, mark IsResumable=False."""
    metadata = checkpoint.get("Metadata") or {}
    if not metadata.get("only_latest_resumable"):
        return checkpoint

    run_id = str(checkpoint.get("RunId") or "")
    my_seq_id = int(checkpoint.get("SeqId") or 0)

    max_seq_id = my_seq_id
    for event in events:
        if event.event_type != "run_checkpoint":
            continue
        ev_meta = event.metadata or {}
        if str(ev_meta.get("run_id") or "") != run_id:
            continue
        seq_id = int(event.seq_id or 0)
        if seq_id > max_seq_id:
            max_seq_id = seq_id

    if my_seq_id < max_seq_id and checkpoint.get("IsResumable") is True:
        checkpoint["IsResumable"] = False
        checkpoint["ResumeStatus"] = "disabled"
        checkpoint["ResumeDisabledReason"] = "新的恢复点已生成，此恢复点暂停恢复能力"
        metadata["resume_disabled_reason"] = "新的恢复点已生成，此恢复点暂停恢复能力"
        metadata["resume_status"] = "disabled"
        checkpoint["Metadata"] = metadata

    return checkpoint


_SIDE_EFFECT_TOOL_NAMES = {
    "write_workspace_file",
    "write_workspace_files",
    "delete_workspace_file",
    "execute_skills",
    "run_command",
    "run_code",
}


def _tool_receipt_event_to_action_payload(event: SessionEvent) -> dict[str, Any] | None:
    if event.event_type != "tool_result":
        return None
    metadata = event.metadata or {}
    receipt = metadata.get("tool_receipt")
    if not isinstance(receipt, Mapping):
        return None
    tool_name = str(receipt.get("tool_name") or metadata.get("tool_name") or "").strip()
    if not tool_name:
        return None
    return {
        "EventId": event.id,
        "SessionId": event.session_id,
        "InvocationId": event.invocation_id,
        "SeqId": event.seq_id,
        "Timestamp": event.timestamp,
        "ReceiptId": str(receipt.get("receipt_id") or ""),
        "IdempotencyKey": str(receipt.get("idempotency_key") or ""),
        "ToolName": tool_name,
        "ToolCallId": str(receipt.get("tool_call_id") or ""),
        "RunId": str(receipt.get("run_id") or metadata.get("run_id") or ""),
        "CheckpointId": str(receipt.get("checkpoint_id") or ""),
        "Status": str(receipt.get("status") or ""),
        "Replayed": bool(receipt.get("replayed") or metadata.get("replayed")),
        "Metadata": dict(metadata),
    }


def _build_checkpoint_resume_preview(
    *,
    checkpoint: Mapping[str, Any],
    receipts: list[dict[str, Any]],
    receipt_total: int,
    side_effect_receipt_count: int,
    failed_receipt_count: int,
) -> dict[str, Any]:
    run_id = str(checkpoint.get("RunId") or "")
    risk_level = "low"
    if side_effect_receipt_count:
        risk_level = "medium"
    if failed_receipt_count:
        risk_level = "high"

    return {
        "Checkpoint": dict(checkpoint),
        "Capabilities": {
            "Checkpoints": True,
            "CheckpointResume": checkpoint.get("IsResumable") is not False,
            "ToolReceipts": True,
            "IdempotentToolReplay": True,
        },
        "CanResume": checkpoint.get("IsResumable") is not False,
        "Reason": str(checkpoint.get("ResumeDisabledReason") or ""),
        "NextNode": str(checkpoint.get("NextNode") or ""),
        "ExpectedAction": (
            "resume_from_checkpoint"
            if checkpoint.get("IsResumable") is True
            else ("preview_required" if checkpoint.get("ResumeStatus") == "unknown" else "disabled")
        ),
        "ToolReceipts": receipts,
        "ToolReceiptsTruncated": receipt_total > len(receipts),
        "Risk": {
            "Level": risk_level,
            "DuplicateSideEffectRisk": side_effect_receipt_count > 0,
            "SideEffectReceiptCount": side_effect_receipt_count,
            "FailedReceiptCount": failed_receipt_count,
        },
        "Summary": {
            "RunId": run_id,
            "CheckpointId": str(checkpoint.get("CheckpointId") or ""),
            "Phase": str(checkpoint.get("Phase") or ""),
            "ToolReceiptCount": receipt_total,
        },
    }


def _checkpoint_resume_disabled_detail(checkpoint: Mapping[str, Any]) -> dict[str, Any] | None:
    if checkpoint.get("IsResumable") is not False:
        return None
    reason = (
        str(checkpoint.get("ResumeDisabledReason") or "").strip() or "Checkpoint is not resumable"
    )
    return {
        "code": "checkpoint_not_resumable",
        "reason": reason,
        "checkpoint_id": str(checkpoint.get("CheckpointId") or ""),
        "run_id": str(checkpoint.get("RunId") or ""),
        "resume_status": str(checkpoint.get("ResumeStatus") or "disabled"),
        "is_terminal": bool(checkpoint.get("IsTerminal")),
    }


async def _iter_session_event_pages(
    service: Any,
    session_id: str,
    *,
    after_seq_id: int | None = None,
    before_seq_id: int | None = None,
) -> AsyncIterator[list[SessionEvent]]:
    remaining = await service.count_events(
        session_id,
        after_seq_id=after_seq_id,
        before_seq_id=before_seq_id,
    )
    while remaining > 0:
        page_size = min(_EVENT_SCAN_PAGE_SIZE, remaining)
        page = await service.get_events(
            session_id,
            offset=remaining - page_size,
            limit=page_size,
            after_seq_id=after_seq_id,
            before_seq_id=before_seq_id,
        )
        if not page:
            return
        yield page
        remaining -= len(page)


def _event_invocation_id(event: SessionEvent) -> str:
    return str(event.invocation_id or "").strip()


async def _extend_invocation_before_window(
    service: Any,
    session_id: str,
    events: list[SessionEvent],
    *,
    after_seq_id: int | None,
) -> list[SessionEvent]:
    if not events:
        return events
    invocation_id = _event_invocation_id(events[0])
    if not invocation_id:
        return events
    cursor = int(events[0].seq_id or 0)
    prefix: list[SessionEvent] = []
    while cursor > 0:
        page = await service.get_events(
            session_id,
            limit=_EVENT_SCAN_PAGE_SIZE,
            after_seq_id=after_seq_id,
            before_seq_id=cursor,
        )
        if not page:
            break
        matching_suffix: list[SessionEvent] = []
        for event in reversed(page):
            if _event_invocation_id(event) != invocation_id:
                break
            matching_suffix.append(event)
        if not matching_suffix:
            break
        matching_suffix.reverse()
        prefix[0:0] = matching_suffix
        cursor = int(matching_suffix[0].seq_id or 0)
        if len(matching_suffix) < len(page):
            break
    return [*prefix, *events]


async def _extend_invocation_after_window(
    service: Any,
    session_id: str,
    events: list[SessionEvent],
    *,
    before_seq_id: int | None,
) -> list[SessionEvent]:
    if not events:
        return events
    invocation_id = _event_invocation_id(events[-1])
    if not invocation_id:
        return events
    cursor = int(events[-1].seq_id or 0)
    suffix: list[SessionEvent] = []
    while True:
        remaining = await service.count_events(
            session_id,
            after_seq_id=cursor,
            before_seq_id=before_seq_id,
        )
        if remaining <= 0:
            break
        page_size = min(_EVENT_SCAN_PAGE_SIZE, remaining)
        page = await service.get_events(
            session_id,
            offset=remaining - page_size,
            limit=page_size,
            after_seq_id=cursor,
            before_seq_id=before_seq_id,
        )
        if not page:
            break
        matching_prefix: list[SessionEvent] = []
        for event in page:
            if _event_invocation_id(event) != invocation_id:
                break
            matching_prefix.append(event)
        if not matching_prefix:
            break
        suffix.extend(matching_prefix)
        cursor = int(matching_prefix[-1].seq_id or 0)
        if len(matching_prefix) < len(page):
            break
    return [*events, *suffix]


async def _iter_agent_event_pages(
    service: Any,
    agent_id: str,
    *,
    user_id: str | None = None,
) -> AsyncIterator[list[SessionEvent]]:
    remaining = await service.count_events_for_agent(agent_id, user_id=user_id)
    while remaining > 0:
        page_size = min(_EVENT_SCAN_PAGE_SIZE, remaining)
        page = await service.get_events_for_agent(
            agent_id,
            user_id=user_id,
            offset=remaining - page_size,
            limit=page_size,
        )
        if not page:
            return
        yield page
        remaining -= len(page)


async def _iter_scoped_event_pages(
    service: Any,
    *,
    session_id: str | None,
    agent_id: str,
    user_id: str | None,
) -> AsyncIterator[list[SessionEvent]]:
    if session_id:
        async for page in _iter_session_event_pages(service, session_id):
            yield page
        return
    async for page in _iter_agent_event_pages(service, agent_id, user_id=user_id):
        yield page


async def _session_contains_invocation(
    service: Any,
    session_id: str,
    invocation_id: str,
) -> bool:
    async for page in _iter_session_event_pages(service, session_id):
        if any(event.invocation_id == invocation_id for event in page):
            return True
    return False


async def _agent_contains_invocation(
    service: Any,
    agent_id: str,
    invocation_id: str,
    *,
    user_id: str | None,
) -> bool:
    async for page in _iter_agent_event_pages(service, agent_id, user_id=user_id):
        if any(event.invocation_id == invocation_id for event in page):
            return True
    return False


async def _latest_invocation_status(
    service: Any,
    session_id: str,
    invocation_id: str,
) -> str:
    offset = 0
    total = await service.count_events(session_id)
    while offset < total:
        page = await service.get_events(
            session_id,
            offset=offset,
            limit=min(_EVENT_SCAN_PAGE_SIZE, total - offset),
        )
        if not page:
            break
        for event in reversed(page):
            if event.invocation_id != invocation_id or event.event_type != "run_status":
                continue
            return str((event.content or {}).get("status") or "").strip().lower()
        offset += len(page)
    return ""


async def _oldest_unconsumed_session_events(
    service: Any,
    session_id: str,
    *,
    after_seq_id: int,
) -> list[SessionEvent]:
    remaining = await service.count_events(session_id, after_seq_id=after_seq_id)
    if remaining <= 0:
        return []
    page_size = min(_EVENT_SCAN_PAGE_SIZE, remaining)
    events = await service.get_events(
        session_id,
        offset=remaining - page_size,
        limit=page_size,
        after_seq_id=after_seq_id,
    )
    return list(events)


def _record_resume_audit(
    audit_by_session: dict[str, dict[tuple[str, str], dict[str, Any]]],
    event: SessionEvent,
) -> None:
    if event.event_type != "run_resume":
        return
    metadata = event.metadata or {}
    run_id = str(metadata.get("run_id") or "").strip()
    checkpoint_id = str(metadata.get("checkpoint_id") or "").strip()
    if not run_id or not checkpoint_id:
        return
    session_audit = audit_by_session.setdefault(event.session_id, {})
    item = session_audit.setdefault(
        (run_id, checkpoint_id),
        {"resume_count": 0, "last_resumed_at": None},
    )
    item["resume_count"] = int(item["resume_count"]) + 1
    item["last_resumed_at"] = event.timestamp


def _apply_latest_checkpoint_policy(
    checkpoint: dict[str, Any],
    latest_by_session_run: Mapping[tuple[str, str], int],
    *,
    session_id: str,
) -> dict[str, Any]:
    metadata = checkpoint.get("Metadata") or {}
    if not metadata.get("only_latest_resumable"):
        return checkpoint
    run_id = str(checkpoint.get("RunId") or "")
    if int(checkpoint.get("SeqId") or 0) >= latest_by_session_run.get((session_id, run_id), 0):
        return checkpoint
    if checkpoint.get("IsResumable") is True:
        checkpoint["IsResumable"] = False
        checkpoint["ResumeStatus"] = "disabled"
        checkpoint["ResumeDisabledReason"] = "新的恢复点已生成，此恢复点暂停恢复能力"
        metadata["resume_disabled_reason"] = checkpoint["ResumeDisabledReason"]
        metadata["resume_status"] = "disabled"
        checkpoint["Metadata"] = metadata
    return checkpoint
