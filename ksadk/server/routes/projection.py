"""Session event projection, pagination, and checkpoint policy helpers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Mapping
from datetime import datetime, timezone
from typing import Any, Optional, cast

from fastapi import HTTPException

from ksadk.conversations.session_title import (
    HEURISTIC_SESSION_TITLE_SOURCE,
    build_fallback_title,
    build_heuristic_title,
)
from ksadk.events.canonical import (
    ContinuationCreated,
    UnknownCanonicalEvent,
)
from ksadk.server.factory import get_runtime_execution, get_state
from ksadk.sessions import Session, SessionEvent
from ksadk.sessions.agent_identity import session_agent_ids
from ksadk.sessions.errors import HistoryProjectionBudgetExceeded, HistoryReloadRequired
from ksadk.sessions.history_identity import history_identity as _history_identity  # noqa: F401

from . import dependencies as deps
from .checkpoint_audit import (  # noqa: F401
    _parse_canonical_payload,
    _record_resume_audit,
    _resume_audit_by_checkpoint,
)
from .common import _sanitize_session_state_for_action
from .models import (
    _EVENT_SCAN_PAGE_SIZE,
    _latest_session_run_metadata,
    _parse_iso_datetime,
    _session_topic_from_events,
    _session_user_prompt_from_event,
    _truncate_session_text,
)

from ksadk.server.routes.display_projection import _first_text_part, _first_tool_name, _first_tool_result, _derive_display_text

logger = logging.getLogger(__name__)
_HISTORY_MAX_EVENTS = 8192
_HISTORY_MAX_BYTES = 16 * 1024 * 1024
# A smaller initial page can reduce transfer latency for large tool outputs
# over a remote database. Invocation completion and total budgets are unchanged.
try:
    _HISTORY_INITIAL_EVENTS = max(32, min(256, int(os.getenv("KSADK_HISTORY_INITIAL_EVENTS", "256"))))
except ValueError:
    _HISTORY_INITIAL_EVENTS = 256
_HISTORY_BOUNDARY_EVENTS = 64
# Completing an invocation uses the same transfer ceiling as its first
# window; only an indivisible first event may exceed it. Short byte-limited
# pages still require continuation until the invocation boundary is found.
_HISTORY_BOUNDARY_BYTES = 64 * 1024


class _HistoryProjectionBudget:
    def __init__(self) -> None:
        self.events = 0
        self.bytes = 0
        self._enriched_ids: set[tuple[str, int]] = set()

    def account(self, events: list[SessionEvent]) -> None:
        self.events += len(events)
        self.bytes += sum(len(json.dumps(event.to_dict(), ensure_ascii=False).encode())
                          for event in events)
        if self.events > _HISTORY_MAX_EVENTS or self.bytes > _HISTORY_MAX_BYTES:
            raise HistoryProjectionBudgetExceeded(
                "History invocation exceeds 8192 events or 16 MiB; reload with a narrower cursor"
            )

    def account_enrichment(
        self, originals: list[dict[str, Any]], enriched: list[dict[str, Any]],
    ) -> None:
        for original, projected in zip(originals, enriched):
            if projected is original:
                continue
            key = (str(original.get("EventId") or ""), int(original.get("SeqId") or 0))
            if key in self._enriched_ids:
                continue
            self._enriched_ids.add(key)
            self.bytes += len(json.dumps(projected, ensure_ascii=False).encode())
        if self.bytes > _HISTORY_MAX_BYTES:
            raise HistoryProjectionBudgetExceeded(
                "History projection exceeds 16 MiB after Kernel input enrichment"
            )


def _validate_history_cursor(
    *, epoch: str, is_live: bool, requested_epoch: str | None,
    cursor_source: str | None, has_cursor: bool,
) -> None:
    if cursor_source == "control" or (requested_epoch is not None and requested_epoch != epoch):
        raise HistoryReloadRequired("History source changed; reload the first page")
    if has_cursor and is_live and requested_epoch is None:
        raise HistoryReloadRequired("Live history cursor has no provable source epoch")


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
        or (agent_id is not None and session.agent_id not in session_agent_ids(agent_id))
        or (user_id is not None and session.user_id != user_id)
    ):
        logger.warning("Session %s not found, userId %s", session_id, user_id)
        raise HTTPException(status_code=404, detail="Session not found")
    return cast(Session, session)


async def _session_to_action_payload(session: Session, service: Any) -> dict[str, Any]:
    # Metadata actions need only prompt endpoints and lifecycle facts. Keep
    # their existing active-run/title semantics without loading full history.
    summary = getattr(service, "get_session_summary_events", None)
    events: list[SessionEvent] = (
        await summary(session.id) if callable(summary)
        else await service.get_events(session.id, limit=256)
    )
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


def _derive_checkpoint_id(event: SessionEvent, content: Mapping[str, Any]) -> str:
    """Surface checkpoint_id to Content top level for checkpoint-bearing events.

    ``run_checkpoint`` events already carry ``checkpoint_id`` at Content top
    level.  ``continuation.created`` events with ``continuation_kind=
    graph_checkpoint`` store it deeper as ``Content.runtime_event.
    continuation_id``; this helper extracts it so frontends reading
    ``Content.checkpoint_id`` work uniformly across both event shapes.
    Returns an empty string for non-checkpoint events.
    """
    if content.get("checkpoint_id") is not None:
        return str(content.get("checkpoint_id") or "")
    if event.event_type == "continuation.created":
        runtime_event = content.get("runtime_event")
        if isinstance(runtime_event, Mapping):
            if str(runtime_event.get("continuation_kind") or "") == "graph_checkpoint":
                return str(runtime_event.get("continuation_id") or "")
    return ""


def _event_to_action_payload(event: SessionEvent) -> dict[str, Any]:
    """Serialize a stored SessionEvent for the REST action wire.

    公开承诺字段（契约声明见 ``ksadk/events/projections.py``）：
    ``EventId``/``SessionId``/``Author``/``EventType``/``Content``/``Timestamp``/
    ``SeqId``（有值时附 ``InvocationId``）。这是存储事件形态本身的透传，
    ``Content``/``Metadata`` 的内部结构不在承诺范围。

    对于 canonical runtime 事件（family=runtime/v2），``Content`` 没有
    顶层 ``parts``。此处根据事件类型从 ``runtime_event`` payload 提取一段
    人类可读文本注入 ``Content.parts[0].text``，兼容前端读取旧路径。
    同理，``continuation.created`` (graph_checkpoint) 的 checkpoint id 存在
    ``Content.runtime_event.continuation_id`` 深处；此处将其提升到
    ``Content.checkpoint_id``，与 ``run_checkpoint`` 事件的外层结构对齐，
    内层 ``runtime_event`` 原始结构保持不动。
    """
    from ksadk.kernel.legacy_history import project_legacy_history_event

    legacy_event = project_legacy_history_event(event)
    if legacy_event is not None:
        return legacy_event

    content = dict(event.content or {})
    if not isinstance(content.get("parts"), list):
        display_text = _derive_display_text(content)
        if display_text:
            content["parts"] = [{"text": display_text}]
    checkpoint_id = _derive_checkpoint_id(event, content)
    if checkpoint_id and "checkpoint_id" not in content:
        content["checkpoint_id"] = checkpoint_id
    payload = {
        "EventId": event.id,
        "SessionId": event.session_id,
        "Author": event.author,
        "EventType": event.event_type,
        "Content": content,
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
    """Project a checkpoint-bearing event into the REST checkpoint action payload.

    公开承诺字段（契约声明见 ``ksadk/events/projections.py``）：``EventId``/
    ``SessionId``/``InvocationId``/``SeqId``/``Timestamp``/``RunId``/
    ``CheckpointId``/``Framework``/``FrameworkRef``/``IsResumable``/
    ``ResumeStatus``/``IsTerminal``/``NextNode``。来源为显式 ``run_checkpoint``
    事件元数据，或 ``continuation.created`` canonical 事件的投影。
    非 checkpoint 事件（或不可识别的 continuation_kind）返回 ``None``。
    内部不保证字段：其余元数据透传键（``Metadata`` 内容随存储演进可变）。
    """
    if event.event_type == "run_checkpoint":
        metadata = event.metadata or {}
    else:
        payload = (event.content or {}).get("runtime_event")
        if not isinstance(payload, dict):
            envelope = (event.content or {}).get("session_event")
            payload = envelope.get("payload") if isinstance(envelope, dict) else None
        if not isinstance(payload, dict):
            return None
        canonical = _parse_canonical_payload(event, payload)
        if isinstance(canonical, UnknownCanonicalEvent):
            return None
        if not isinstance(canonical, ContinuationCreated):
            return None
        if canonical.continuation_kind != "graph_checkpoint":
            return None
        framework = canonical.source.framework
        source_metadata = canonical.source.metadata
        framework_ref = {framework: dict(canonical.ref)}
        capability = source_metadata.get("capability")
        capability = capability if isinstance(capability, Mapping) else {}
        # Fallback: when the canonical event lacks capability fields, try
        # the raw event metadata (legacy run_checkpoint stored backend/scope
        # directly in metadata, and some adapters may not enrich source).
        event_meta = event.metadata or {}
        def _capability_str(key: str, default: str = "unknown") -> str:
            value = capability.get(key)
            if value is not None:
                return str(value)
            value = event_meta.get(key)
            if value is not None:
                return str(value)
            value = source_metadata.get(key)
            if value is not None:
                return str(value)
            return default
        durable_raw = capability.get("durable")
        if durable_raw is None:
            durable_raw = event_meta.get("durable", source_metadata.get("durable", False))
        def _cap_val(key: str, default: Any = None) -> Any:
            value = capability.get(key)
            if value is not None:
                return value
            return event_meta.get(key, default)
        metadata = {
            **dict(event.metadata or {}),
            **dict(canonical.source.metadata),
            "continuation_kind": canonical.continuation_kind,
            "run_id": canonical.run_id,
            "checkpoint_id": canonical.continuation_id,
            "framework": framework,
            "framework_ref": dict(framework_ref),
            "backend": _capability_str("backend"),
            "scope": _capability_str("scope"),
            "durable": bool(durable_raw),
            "is_resumable": canonical.resumable,
            "is_terminal": bool(
                capability.get("is_terminal", event_meta.get("is_terminal", False))
            ),
            "next_node": _capability_str("next_node", ""),
            "resume_status": _capability_str("resume_status", ""),
            "resume_disabled_reason": _capability_str("resume_disabled_reason", ""),
            "phase": _capability_str("phase", ""),
            "stage": _capability_str("stage", ""),
            "stage_name": _capability_str("stage_name", ""),
            "stage_key": _capability_str("stage_key", ""),
            "summary": _capability_str("summary", ""),
            "next_action": _capability_str("next_action", ""),
            "status": _capability_str("status", ""),
            "tool_name": _capability_str("tool_name", ""),
            "receipt_key": _capability_str("receipt_key", ""),
            "artifact_path": _capability_str("artifact_path", ""),
            "stage_index": _cap_val("stage_index"),
            "total_stages": _cap_val("total_stages"),
            "artifact_preview": _cap_val("artifact_preview", {}),
        }
    run_id = str(metadata.get("run_id") or "").strip()
    checkpoint_id = str(metadata.get("checkpoint_id") or "").strip()
    framework = str(metadata.get("framework") or "").strip()
    framework_ref = metadata.get("framework_ref")
    if not run_id or not checkpoint_id or not framework or not isinstance(framework_ref, Mapping):
        return None
    next_node = str(metadata.get("next_node") or "").strip()
    if not next_node:
        langgraph_ref = framework_ref.get(framework)
        if isinstance(langgraph_ref, Mapping):
            next_node = str(langgraph_ref.get("next_node") or "").strip()
            # Fallback: stream_mapping wraps framework_ref one level deeper;
            # try ref[framework].framework_ref[framework].next_node.
            if not next_node:
                inner_ref = langgraph_ref.get("framework_ref")
                if isinstance(inner_ref, Mapping):
                    inner_framework_ref = inner_ref.get(framework)
                    if isinstance(inner_framework_ref, Mapping):
                        next_node = str(inner_framework_ref.get("next_node") or "").strip()
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


def _apply_checkpoint_resume_audit(
    checkpoint: dict[str, Any],
    audit_by_checkpoint: Mapping[tuple[str, ...], Mapping[str, Any]],
) -> dict[str, Any]:
    session_id = str(checkpoint.get("SessionId") or "")
    run_id = str(checkpoint.get("RunId") or "")
    checkpoint_id = str(checkpoint.get("CheckpointId") or "")
    audit = audit_by_checkpoint.get(
        (session_id, run_id, checkpoint_id),
        audit_by_checkpoint.get((run_id, checkpoint_id), {}),
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
        "Code": "checkpoint_not_resumable",
        "code": "checkpoint_not_resumable",
        "Reason": reason,
        "reason": reason,
        "CheckpointId": str(checkpoint.get("CheckpointId") or ""),
        "checkpoint_id": str(checkpoint.get("CheckpointId") or ""),
        "RunId": str(checkpoint.get("RunId") or ""),
        "run_id": str(checkpoint.get("RunId") or ""),
        "ResumeStatus": str(checkpoint.get("ResumeStatus") or "disabled"),
        "resume_status": str(checkpoint.get("ResumeStatus") or "disabled"),
        "IsTerminal": bool(checkpoint.get("IsTerminal")),
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
    budget: _HistoryProjectionBudget | None = None,
    boundary_hint: dict[str, bool] | None = None,
) -> list[SessionEvent]:
    if not events:
        return events
    invocation_id = _event_invocation_id(events[0])
    if not invocation_id:
        return events
    cursor = int(events[0].seq_id or 0)
    prefix: list[SessionEvent] = []
    while cursor > 0:
        from ksadk.sessions.history_budget import CURRENT_HISTORY_READ_BUDGET
        window = getattr(service, 'get_message_event_window', None)
        use_window = callable(window) and CURRENT_HISTORY_READ_BUDGET.get() is not None
        page = await (window if use_window else service.get_events)(
            session_id,
            limit=_HISTORY_BOUNDARY_EVENTS,
            after_seq_id=after_seq_id,
            before_seq_id=cursor,
            **({'byte_limit': _HISTORY_BOUNDARY_BYTES} if use_window else {}),
        )
        if not page:
            if boundary_hint is not None:
                boundary_hint['has_more'] = False
            break
        if budget is not None:
            budget.account(page)
        matching_suffix: list[SessionEvent] = []
        for event in reversed(page):
            if _event_invocation_id(event) != invocation_id:
                break
            matching_suffix.append(event)
        if not matching_suffix:
            if boundary_hint is not None:
                boundary_hint['has_more'] = True
            break
        matching_suffix.reverse()
        prefix[0:0] = matching_suffix
        cursor = int(matching_suffix[0].seq_id or 0)
        exhausted = getattr(page, 'source_exhausted', len(page) < _HISTORY_BOUNDARY_EVENTS)
        if len(matching_suffix) < len(page) or exhausted:
            if boundary_hint is not None:
                boundary_hint['has_more'] = len(matching_suffix) < len(page)
            break
    return [*prefix, *events]


async def _extend_invocation_after_window(
    service: Any,
    session_id: str,
    events: list[SessionEvent],
    *,
    before_seq_id: int | None,
    budget: _HistoryProjectionBudget | None = None,
) -> list[SessionEvent]:
    if not events:
        return events
    invocation_id = _event_invocation_id(events[-1])
    if not invocation_id:
        return events
    cursor = int(events[-1].seq_id or 0)
    suffix: list[SessionEvent] = []
    while True:
        from ksadk.sessions.base import SessionEventQuery
        from ksadk.sessions.history_budget import CURRENT_HISTORY_READ_BUDGET
        window = getattr(service, 'get_message_event_window', None)
        if callable(window) and CURRENT_HISTORY_READ_BUDGET.get() is not None:
            page = await window(
                session_id, limit=_HISTORY_BOUNDARY_EVENTS, from_start=True,
                after_seq_id=cursor, before_seq_id=before_seq_id,
                byte_limit=_HISTORY_BOUNDARY_BYTES,
            )
        else:
            page = await service.query_events(
                SessionEventQuery(
                    session_ids=[session_id], from_start=True, order_by_seq=True,
                    limit=_HISTORY_BOUNDARY_EVENTS, after_seq_id=cursor,
                    before_seq_id=before_seq_id,
                )
            )
        if not page:
            break
        if budget is not None:
            budget.account(page)
        matching_prefix: list[SessionEvent] = []
        for event in page:
            if _event_invocation_id(event) != invocation_id:
                break
            matching_prefix.append(event)
        if not matching_prefix:
            break
        suffix.extend(matching_prefix)
        cursor = int(matching_prefix[-1].seq_id or 0)
        exhausted = getattr(page, 'source_exhausted', len(page) < _HISTORY_BOUNDARY_EVENTS)
        if len(matching_prefix) < len(page) or exhausted:
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
    query = getattr(service, "query_events", None)
    if callable(query):
        from ksadk.sessions.base import SessionEventQuery
        return bool(await query(SessionEventQuery(
            session_ids=[session_id], invocation_id=invocation_id,
            limit=1, order_by_seq=True,
        )))
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
    query = getattr(service, "query_events", None)
    if callable(query):
        from ksadk.sessions.base import SessionEventQuery
        page = await query(SessionEventQuery(
            session_ids=[session_id], invocation_id=invocation_id,
            event_types=["run_status"], limit=1, order_by_seq=True,
        ))
        return str((page[-1].content or {}).get("status") or "").strip().lower() if page else ""
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
    invocation_id: str | None = None,
) -> list[SessionEvent]:
    query = getattr(service, "query_events", None)
    if callable(query):
        from ksadk.sessions.base import SessionEventQuery
        return list(await query(SessionEventQuery(
            session_ids=[session_id], invocation_id=invocation_id,
            after_seq_id=after_seq_id, from_start=True, order_by_seq=True,
            limit=32,
        )))
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
