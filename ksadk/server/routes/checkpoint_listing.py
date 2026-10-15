"""Bounded checkpoint listing, filtering, audit aggregation, and pagination."""

from __future__ import annotations

import heapq
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException

from ksadk.runtime_context import PlatformIdentityContext
from ksadk.sessions.base import CheckpointEventQuery
from ksadk.sessions.errors import CheckpointScanRestartRequired, SessionBackendUnavailable

from . import dependencies as deps
from .models import ListSessionCheckpointsActionRequest
from .projection import (
    _apply_adk_only_latest_resumable,
    _apply_checkpoint_resume_audit,
    _apply_latest_checkpoint_policy,
    _checkpoint_event_to_action_payload,
    _resume_audit_by_checkpoint,
)


def _count_resumable_checkpoints(checkpoints: list[dict[str, Any]]) -> int:
    """统计可恢复 checkpoint 数量。

    规则：IsResumable=True AND ReplayAllowed!=False AND IsTerminal!=True
    AND CheckpointStatus not in {expired, disabled}。
    不排除 resumed（已恢复过的仍计入，符合存档点可反复读的回档语义）。
    """
    resumable = 0
    for cp in checkpoints:
        if cp.get("IsResumable") is not True:
            continue
        if cp.get("ReplayAllowed") is False:
            continue
        if cp.get("IsTerminal") is True:
            continue
        status = str(cp.get("CheckpointStatus") or "").strip().lower()
        if status in {"expired", "disabled"}:
            continue
        resumable += 1
    return resumable


def _normalize_action_id_list(value: list[str] | str | None) -> list[str]:
    values = [value] if isinstance(value, str) else list(value or [])
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def _is_checkpoint_resumable(checkpoint: Mapping[str, Any]) -> bool:
    if checkpoint.get("IsResumable") is not True or checkpoint.get("IsTerminal") is True:
        return False
    if checkpoint.get("ReplayAllowed") is False:
        return False
    checkpoint_status = str(checkpoint.get("CheckpointStatus") or "").strip().lower()
    resume_status = str(checkpoint.get("ResumeStatus") or "").strip().lower()
    return (
        checkpoint_status not in {"expired", "disabled", "terminal"} and resume_status != "disabled"
    )


async def _list_checkpoints_payload_legacy_filtered(
    request: ListSessionCheckpointsActionRequest,
    session_ids: list[str],
    checkpoint_ids: list[str],
) -> dict[str, Any]:
    if len(session_ids) != 1:
        raise HTTPException(
            status_code=501, detail="Backend does not support multi-session checkpoints"
        )
    service = deps.resolve_session_service()
    events = await service.get_events(session_ids[0])
    audit = _resume_audit_by_checkpoint(events)
    latest_by_session_run: dict[tuple[str, str], int] = {}
    for event in events:
        checkpoint = _checkpoint_event_to_action_payload(event)
        if checkpoint is not None and (checkpoint.get("Metadata") or {}).get(
            "only_latest_resumable"
        ):
            key = (event.session_id, str(checkpoint.get("RunId") or ""))
            latest_by_session_run[key] = max(
                latest_by_session_run.get(key, 0), int(checkpoint.get("SeqId") or 0)
            )
    checkpoints: list[dict[str, Any]] = []
    resume_status_filter = set(request.ResumeStatus)
    resume_type_filter = set(request.ResumeTypes)
    for event in events:
        checkpoint = _checkpoint_event_to_action_payload(event)
        if checkpoint is None:
            continue
        checkpoint = _apply_checkpoint_resume_audit(checkpoint, audit)
        checkpoint = _apply_latest_checkpoint_policy(
            checkpoint, latest_by_session_run, session_id=event.session_id
        )
        if request.RunId and checkpoint["RunId"] != str(request.RunId):
            continue
        if checkpoint_ids and checkpoint["CheckpointId"] not in checkpoint_ids:
            continue
        if (
            request.Framework
            and str(checkpoint["Framework"]).lower() != str(request.Framework).lower()
        ):
            continue
        if (
            resume_status_filter
            and str(checkpoint.get("ResumeStatus") or "").lower() not in resume_status_filter
        ):
            continue
        if (
            resume_type_filter
            and str(checkpoint.get("Scope") or "unknown").lower() not in resume_type_filter
        ):
            continue
        checkpoints.append(checkpoint)
    checkpoints = _apply_adk_only_latest_resumable(checkpoints)
    resumable_total = sum(_is_checkpoint_resumable(item) for item in checkpoints)
    if request.OnlyResumable:
        checkpoints = [item for item in checkpoints if _is_checkpoint_resumable(item)]
    offset = int(request.Offset or 0)
    checkpoints.sort(
        key=lambda item: (item["Timestamp"], item["SessionId"], item["SeqId"]),
        reverse=True,
    )
    checkpoints = checkpoints[offset : offset + request.Limit]
    return {
        "Checkpoints": checkpoints,
        "Total": len(checkpoints),
        "ResumableTotal": resumable_total,
        "HasResumableCheckpoint": resumable_total > 0,
        "SessionId": session_ids,
        "CheckpointId": checkpoint_ids,
        "ResumeStatus": request.ResumeStatus,
        "ResumeTypes": request.ResumeTypes,
        "Offset": offset,
        "Limit": request.Limit,
    }


async def _list_checkpoints_payload(
    request: ListSessionCheckpointsActionRequest,
    invocation_identity: PlatformIdentityContext,
    *,
    require_session,
    list_sessions,
) -> dict[str, Any]:
    service = deps.resolve_session_service()
    session_ids = _normalize_action_id_list(request.SessionId)
    checkpoint_ids = _normalize_action_id_list(request.CheckpointId)
    if session_ids:
        for session_id in session_ids:
            await require_session(
                service,
                session_id=session_id,
                agent_id=request.AgentId,
                user_id=request.UserId,
                invocation_identity=invocation_identity,
                history_only=True,
            )
    elif request.UserId is not None or not invocation_identity.is_empty:
        session_ids = [
            session.id
            for session in await list_sessions(
                service,
                agent_id=request.AgentId,
                user_id=request.UserId,
                invocation_identity=invocation_identity,
            )
        ]
    if not invocation_identity.is_empty and not session_ids:
        return {
            "Checkpoints": [],
            "Total": 0,
            "ResumableTotal": 0,
            "HasResumableCheckpoint": False,
            "SessionId": [],
            "CheckpointId": checkpoint_ids,
            "ResumeStatus": request.ResumeStatus,
            "ResumeTypes": request.ResumeTypes,
            "Offset": int(request.Offset or 0),
            "Limit": request.Limit,
        }
    query = CheckpointEventQuery(
        session_ids=session_ids or None,
        agent_id=request.AgentId,
        checkpoint_ids=checkpoint_ids or None,
        run_id=str(request.RunId or "").strip() or None,
        framework=str(request.Framework or "").strip().lower() or None,
        limit=50,
    )
    resume_status_filter = set(request.ResumeStatus)
    resume_type_filter = set(request.ResumeTypes)
    offset = int(request.Offset or 0)
    for attempt in range(2):
        total = 0
        resumable_total = 0
        top_checkpoints: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        page_capacity = offset + int(request.Limit)
        sequence = 0
        batches = service.iter_checkpoint_event_chunks(query)
        try:
            async for batch in batches:
                chunk = [
                    checkpoint
                    for event in batch
                    if (checkpoint := _checkpoint_event_to_action_payload(event)) is not None
                    and (
                        not checkpoint_ids
                        or str(checkpoint.get("CheckpointId") or "") in checkpoint_ids
                    )
                    and (query.run_id is None or str(checkpoint.get("RunId") or "") == query.run_id)
                    and (
                        query.framework is None
                        or str(checkpoint.get("Framework") or "").lower() == query.framework
                    )
                ]
                keys = [
                    (
                        str(checkpoint["SessionId"]),
                        str(checkpoint["RunId"]),
                        str(checkpoint["CheckpointId"]),
                    )
                    for checkpoint in chunk
                ]
                stats = await service.get_checkpoint_stats(keys)
                for checkpoint in chunk:
                    checkpoint = _apply_checkpoint_resume_audit(
                        checkpoint, stats.get("audits") or {}
                    )
                    checkpoint = _apply_latest_checkpoint_policy(
                        checkpoint,
                        stats.get("latest_seq_ids") or {},
                        session_id=str(checkpoint.get("SessionId") or ""),
                    )
                    if (
                        resume_status_filter
                        and str(checkpoint.get("ResumeStatus") or "").strip().lower()
                        not in resume_status_filter
                    ):
                        continue
                    if (
                        resume_type_filter
                        and str(checkpoint.get("Scope") or "unknown").strip().lower()
                        not in resume_type_filter
                    ):
                        continue
                    resumable_total += int(_is_checkpoint_resumable(checkpoint))
                    if request.OnlyResumable and not _is_checkpoint_resumable(checkpoint):
                        continue
                    if page_capacity:
                        sequence += 1
                        rank = (
                            checkpoint["Timestamp"],
                            checkpoint["SessionId"],
                            checkpoint["SeqId"],
                            sequence,
                        )
                        item = (rank, checkpoint)
                        if len(top_checkpoints) < page_capacity:
                            heapq.heappush(top_checkpoints, item)
                        elif rank > top_checkpoints[0][0]:
                            heapq.heapreplace(top_checkpoints, item)
                    total += 1
        except CheckpointScanRestartRequired as exc:
            if attempt == 0:
                continue
            raise SessionBackendUnavailable(
                "Checkpoint backend changed repeatedly during one request"
            ) from exc
        except NotImplementedError:
            return await _list_checkpoints_payload_legacy_filtered(
                request, session_ids, checkpoint_ids
            )
        finally:
            close = getattr(batches, "aclose", None)
            if callable(close):
                await close()
        break
    checkpoints = [item for _, item in sorted(top_checkpoints, reverse=True)]
    checkpoints = checkpoints[offset : offset + int(request.Limit)]
    return {
        "Checkpoints": checkpoints,
        "Total": total,
        "ResumableTotal": resumable_total,
        "HasResumableCheckpoint": resumable_total > 0,
        "SessionId": session_ids,
        "CheckpointId": checkpoint_ids,
        "ResumeStatus": request.ResumeStatus,
        "ResumeTypes": request.ResumeTypes,
        "Offset": offset,
        "Limit": request.Limit,
    }
