"""Session, UI bootstrap, checkpoint-list, and tool-receipt routes."""
# ruff: noqa: I001
from __future__ import annotations

import heapq
import json
import logging
import os
from collections.abc import Mapping
from typing import Any

from fastapi import Depends, HTTPException

from ksadk.conversations.runtime_persistence import (
    ensure_conversation_session,
    require_conversation_session,
)
from ksadk.runtime.executor import project_legacy_runtime_capabilities
from ksadk.runtime_context import PlatformIdentityContext
from ksadk.server.factory import get_runtime_execution, get_state
from ksadk.sessions import SessionEvent
from ksadk.sessions.base import CheckpointEventQuery, SessionEventQuery
from ksadk.sessions.errors import CheckpointScanRestartRequired, SessionBackendUnavailable
from ksadk.sessions.in_memory import InMemorySessionService
from ksadk.sessions.persistence import persistence_probe_timeout
from ksadk.sessions.invocation_identity import (
    bind_or_validate_session_identity,
    identity_can_adopt_any_legacy_user,
    session_identity_binding_matches,
)
from ksadk.tools.gateway import tool_approval_capability
from ksadk.toolsets import describe_agentengine_tools
from ksadk_runtime_common.workspace_files import (
    build_workspace_files_bootstrap,
    workspace_files_enabled,
)

from ..invocation_identity import resolve_trusted_invocation_identity
from . import dependencies as deps
from .common import (
    _action_response,
    _build_bootstrap_model_payload,
    _build_native_terminal_capability,
    _ensure_session,
    _resolve_agent_ui_spec,
)
from .models import (
    CreateSessionActionRequest,
    ListSessionCheckpointsActionRequest,
    ListSessionEventsActionRequest,
    ListSessionMessagesActionRequest,
    ListSessionsActionRequest,
    ListToolReceiptsActionRequest,
    SessionIdRequest,
    UiBootstrapRequest,
    _runtime_agent_id,
)
from .projection import (
    _HISTORY_INITIAL_EVENTS,
    _apply_adk_only_latest_resumable,
    _apply_checkpoint_resume_audit,
    _apply_latest_checkpoint_policy,
    _checkpoint_event_to_action_payload,
    _event_invocation_id,
    _event_to_action_payload,
    _extend_invocation_after_window,
    _extend_invocation_before_window,
    _history_identity,
    _HistoryProjectionBudget,
    _require_action_session,
    _resume_audit_by_checkpoint,
    _session_to_action_payload,
    _tool_receipt_event_to_action_payload,
    _validate_history_cursor,
)
from .routers import sessions_router, tools_router, ui_bootstrap_router
from .streaming import _cancel_detached_streams_for_session
from .tool_receipt_claim_routes import get_tool_receipt_claim_action, reconcile_tool_receipt_claim_action  # noqa: E501,F401,I001


async def _require_identity_action_session(
    service: Any,
    *,
    session_id: str,
    agent_id: str | None,
    user_id: str | None,
    invocation_identity: PlatformIdentityContext,
    history_only: bool = False,
    history_header_loader: Any = None,
):
    if history_only:
        loader = history_header_loader or getattr(service, "get_session_header", service.get_session_metadata)
        session = await loader(session_id)
        from ksadk.sessions.agent_identity import session_agent_ids
        if (session is None or (agent_id is not None and session.agent_id not in session_agent_ids(agent_id))
                or (user_id is not None and invocation_identity.is_empty and session.user_id != user_id)):
            raise HTTPException(status_code=404, detail="Session not found")
        if invocation_identity.is_empty:
            return session
        if not agent_id:
            raise HTTPException(status_code=400, detail="AgentId is required")
        from ksadk.sessions.invocation_identity import bind_or_validate_session_identity
        return await bind_or_validate_session_identity(
            service=service, session=session, identity=invocation_identity,
            requested_user_id=str(user_id or ""),
        )
    if invocation_identity.is_empty:
        return await _require_action_session(
            service,
            session_id=session_id,
            agent_id=agent_id,
            user_id=user_id,
        )
    if not agent_id:
        raise HTTPException(status_code=400, detail="AgentId is required")
    return await require_conversation_session(
        agent_id=agent_id,
        user_id=str(user_id or ""),
        session_id=session_id,
        session_service_provider=lambda: service,
        invocation_identity=invocation_identity,
    )


async def _list_identity_session_metadata(
    service: Any,
    *,
    agent_id: str,
    user_id: str | None,
    invocation_identity: PlatformIdentityContext,
) -> list[Any]:
    """List bound sessions; only the verified IAM account may adopt legacy rows."""

    if invocation_identity.is_empty:
        return await service.list_session_metadata(agent_id, user_id)
    from ksadk.sessions.agent_identity import session_agent_ids
    from ksadk.sessions.invocation_identity import identity_native_user_id

    candidate_users: set[str | None] = {identity_native_user_id(invocation_identity)}
    if identity_can_adopt_any_legacy_user(invocation_identity):
        candidate_users.add(None)
    elif (
        invocation_identity.identity_namespace == "kscloud-iam"
        and invocation_identity.subject_type == "user"
    ):
        candidate_users.add(invocation_identity.subject_id)
    sessions: list[Any] = []
    seen: set[str] = set()
    for candidate_user in candidate_users:
        candidates = [
            session
            for owner_id in session_agent_ids(agent_id)
            for session in await service.list_session_metadata(owner_id, candidate_user)
        ]
        for session in candidates:
            if session.id in seen:
                continue
            matches = await session_identity_binding_matches(
                service=service,
                session=session,
                identity=invocation_identity,
            )
            if not matches:
                try:
                    await bind_or_validate_session_identity(
                        service=service,
                        session=session,
                        identity=invocation_identity,
                        requested_user_id=session.user_id,
                    )
                except HTTPException:
                    matches = False
                else:
                    matches = True
            if matches:
                sessions.append(session)
                seen.add(session.id)
    sessions.sort(
        key=lambda item: (item.updated_at, item.created_at, item.id),
        reverse=True,
    )
    return sessions


@ui_bootstrap_router.post("/agentengine/api/v1/GetAgentUiBootstrap")
async def get_agent_ui_bootstrap(
    request: UiBootstrapRequest,
    invocation_identity: PlatformIdentityContext = Depends(resolve_trusted_invocation_identity),
):
    state = get_state()
    executor, launch_context = get_runtime_execution()
    detection = launch_context.detection
    agent_id = request.AgentId or _runtime_agent_id(launch_context)
    description = str(getattr(detection, "description", "") or "")
    framework = launch_context.runtime_type.strip().lower()
    workspace_enabled = workspace_files_enabled(default=True)
    ui_spec = _resolve_agent_ui_spec()
    capability_subject = executor.capability_subject(launch_context)
    if capability_subject is not None:
        wait_timeout = persistence_probe_timeout()
        capability_snapshot = await state.persistence_capability.get_snapshot(
            runner=capability_subject,
            framework=framework,
            status_provider=deps.get_persistence_status,
            session_service_provider=deps.resolve_session_service,
            wait_timeout=wait_timeout,
        )
        persistence = dict(capability_snapshot.session_persistence)
        checkpoint_persistence = dict(capability_snapshot.checkpoint_persistence)
        runtime_capabilities = dict(capability_snapshot.runtime_capabilities)
        runtime_capability_matrix = executor.capability_matrix(launch_context)
    else:
        runtime_capability_matrix = executor.capability_matrix(launch_context)
        persistence_status = dict(await deps.get_persistence_status(framework=framework))
        if isinstance(persistence_status.get("Session"), Mapping):
            persistence = dict(persistence_status["Session"])
            checkpoint_persistence = dict(persistence_status.get("Checkpoint") or {})
        else:
            persistence = persistence_status
            checkpoint_persistence = dict(persistence_status)
        runtime_capabilities = project_legacy_runtime_capabilities(
            executor.native_capabilities(launch_context), runtime_capability_matrix
        )
    resume_capability = (
        runtime_capabilities.get("ResumeRun") if isinstance(runtime_capabilities, Mapping) else None
    )
    resume_capability = resume_capability if isinstance(resume_capability, Mapping) else {}
    checkpoint_capability = (
        runtime_capabilities.get("Checkpoint")
        if isinstance(runtime_capabilities, Mapping)
        else None
    )
    checkpoint_capability = (
        checkpoint_capability if isinstance(checkpoint_capability, Mapping) else {}
    )
    cancel_capability = (
        runtime_capabilities.get("CancelRun") if isinstance(runtime_capabilities, Mapping) else None
    )
    cancel_capability = cancel_capability if isinstance(cancel_capability, Mapping) else {}
    checkpoint_resume_capability = {
        "Supported": bool(resume_capability.get("Supported")),
        "Checkpoint": checkpoint_capability,
        "ResumeRun": resume_capability,
    }
    checkpoint_resume_supported = bool(checkpoint_resume_capability["Supported"])
    cancel_run_supported = bool(cancel_capability.get("Supported"))
    responses_transport = {
        "Protocol": "responses",
        "Runtime": "ksadk",
        "Endpoint": "/v1/responses",
        "Version": "v1",
        "Capabilities": {
            "A2UI": False,
            "Interrupt": checkpoint_resume_supported,
            "Cancel": cancel_run_supported,
        },
    }
    hosted_chat_transports = []
    if state.agui_agent is not None and state.agui_config is not None:
        from ksadk.agui.config import AGUI_PROTOCOL_VERSION

        hosted_chat_transports.append(
            {
                "Protocol": "ag-ui",
                "Runtime": "copilotkit",
                "Endpoint": state.agui_config.path,
                "Version": AGUI_PROTOCOL_VERSION,
                "Capabilities": {
                    "A2UI": True,
                    "Interrupt": checkpoint_resume_supported,
                    "Cancel": True,
                },
            }
        )
    hosted_chat_transports.append(responses_transport)
    preferred_transport = (
        "ag-ui"
        if hosted_chat_transports[0]["Protocol"] == "ag-ui"
        and bool(getattr(state.agui_config, "preferred", False))
        else "responses"
    )
    # Producer declaration is separate from transport availability. Consumers
    # intersect this surface with their renderer catalogue before selecting a
    # presentation profile; an older runtime has no such declaration.
    presentation_capabilities: dict[str, Any] = {}
    managed_remote_surface = (
        framework in {"codex", "langgraph"}
        and bool(getattr(state.a2a_bootstrap, "outbound_enabled", False))
    )
    if framework == "adk" or managed_remote_surface:
        from ksadk.conversations.contracts import ConversationCapability, ConversationSurface

        presentation_capabilities["ConversationSurface"] = ConversationSurface(
            surface_id=f"runtime:{agent_id}",
            session_id=request.SessionId or "new-session",
            provider_ref=framework,
            inputs=(ConversationCapability(name="ksadk.presentation", mode="native"),),
            outputs=(
                ConversationCapability(
                    name="agent.block",
                    mode="native" if framework == "adk" else "translated",
                ),
            ),
        ).model_dump(mode="json", by_alias=True)
    return _action_response(
        "GetAgentUiBootstrap",
        {
            "Agent": {
                "AgentId": agent_id,
                "Name": str(getattr(detection, "name", "") or agent_id),
                "Description": description or "",
                "Framework": framework,
            },
            "Modules": ["Chat", "Build", "Deploy"],
            "Capabilities": {
                # Lightweight protocol negotiation; it does not hydrate a
                # session or require historical Agent packages to be patched.
                "HistoryRead": {
                    "Protocol": "ksadk.history/v2",
                    "SessionContractVersion": 2,
                    "SupportsEpoch": True,
                    "SupportsSnapshot": True,
                },
                **presentation_capabilities,
                "Attachments": True,
                "WorkspaceFiles": workspace_enabled,
                "Approval": True,
                "ApprovalPolicy": tool_approval_capability(),
                "Thinking": True,
                "StopRun": cancel_run_supported,
                "ResumeRun": checkpoint_resume_supported,
                "Persistence": persistence,
                "CheckpointPersistence": checkpoint_persistence,
                "RuntimeCapabilities": runtime_capabilities,
                "RuntimeCapabilityMatrix": runtime_capability_matrix,
                "CheckpointResumeCapability": checkpoint_resume_capability,
                "RunLifecycle": {
                    "Enabled": True,
                    "Resume": True,
                    "Abort": True,
                    "Checkpoints": checkpoint_resume_supported,
                    "CheckpointResume": checkpoint_resume_supported,
                    "CheckpointResumePreview": checkpoint_resume_supported,
                },
                "MCP": False,
                "HostedRuntime": False,
                "NativeTerminal": _build_native_terminal_capability(framework, invocation_identity),
                "BuiltinTools": describe_agentengine_tools(),
            },
            "WorkspaceFiles": build_workspace_files_bootstrap(enabled=workspace_enabled),
            "AccessMode": "Owner",
            "SharePermissions": {
                "Interactive": True,
                "DefaultPath": ui_spec.get("ui_path") or ui_spec.get("path") or "/chat",
                "SharePath": ui_spec.get("ui_path") or ui_spec.get("path") or "/chat",
            },
            "CustomUI": {
                "Enabled": bool(ui_spec.get("enabled")),
                "Profile": ui_spec.get("ui_profile") or ui_spec.get("profile"),
                "Path": ui_spec.get("ui_path") or ui_spec.get("path"),
                "Url": ui_spec.get("ui_url") or ui_spec.get("url"),
                "BundlePath": ui_spec.get("ui_bundle_path") or ui_spec.get("bundle_path"),
            },
            "ApiFormats": ["responses", "chat_completions"],
            "HostedChat": {
                "PreferredTransport": preferred_transport,
                "Transports": hosted_chat_transports,
            },
            "Stream": True,
            "SessionId": request.SessionId,
            "SessionBackend": deps.describe_session_backend(),
            "HostedRuntime": None,
            "Model": _build_bootstrap_model_payload(),
        },
    )


@sessions_router.post("/agentengine/api/v1/CreateSession")
async def create_session_action(
    request: CreateSessionActionRequest,
    invocation_identity: PlatformIdentityContext = Depends(resolve_trusted_invocation_identity),
):
    service = deps.resolve_session_service()
    if invocation_identity.is_empty:
        session = await _ensure_session(
            request.AgentId, request.UserId or "user", request.SessionId
        )
    else:
        session = await ensure_conversation_session(
            agent_id=request.AgentId,
            user_id=request.UserId or "user",
            session_id=request.SessionId,
            session_service_provider=deps.resolve_session_service,
            invocation_identity=invocation_identity,
        )
    return _action_response("CreateSession", {"Session": await _session_to_action_payload(session, service)})


@sessions_router.post("/agentengine/api/v1/ListSessions")
async def list_sessions_action(
    request: ListSessionsActionRequest,
    invocation_identity: PlatformIdentityContext = Depends(resolve_trusted_invocation_identity),
):
    service = deps.resolve_session_service()
    offset = (request.Page - 1) * request.PageSize
    if invocation_identity.is_empty:
        if isinstance(service, InMemorySessionService):
            all_metadata = await service.list_session_metadata(request.AgentId, request.UserId)
            sessions = all_metadata[offset:offset + request.PageSize]
        else:
            sessions = await service.list_sessions(
                request.AgentId, request.UserId, offset=offset, limit=request.PageSize,
            )
        total = await service.count_sessions(request.AgentId, request.UserId)
    else:
        all_sessions = await _list_identity_session_metadata(
            service,
            agent_id=request.AgentId,
            user_id=request.UserId,
            invocation_identity=invocation_identity,
        )
        total = len(all_sessions)
        sessions = all_sessions[offset : offset + request.PageSize]
    session_payloads = [await _session_to_action_payload(session, service) for session in sessions]
    return _action_response(
        "ListSessions",
        {
            "Sessions": session_payloads,
            "Total": total,
            "Page": request.Page,
            "PageSize": request.PageSize,
            "DataSource": "runtime",
            "Degraded": False,
            "SessionContractVersion": 2,
        },
    )


@sessions_router.post("/agentengine/api/v1/GetSession")
async def get_session_action(
    request: SessionIdRequest,
    invocation_identity: PlatformIdentityContext = Depends(resolve_trusted_invocation_identity),
):
    service = deps.resolve_session_service()
    session = await _require_identity_action_session(
        service,
        session_id=request.SessionId,
        agent_id=request.AgentId,
        user_id=request.UserId,
        invocation_identity=invocation_identity,
    )
    return _action_response("GetSession", {"Session": await _session_to_action_payload(session, service)})


@sessions_router.post("/agentengine/api/v1/DeleteSession")
async def delete_session_action(
    request: SessionIdRequest,
    invocation_identity: PlatformIdentityContext = Depends(resolve_trusted_invocation_identity),
):
    service = deps.resolve_session_service()
    await _require_identity_action_session(
        service,
        session_id=request.SessionId,
        agent_id=request.AgentId,
        user_id=request.UserId,
        invocation_identity=invocation_identity,
    )
    await _cancel_detached_streams_for_session(request.SessionId)
    deleted = await service.delete_session(request.SessionId)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return _action_response("DeleteSession", {"Deleted": True})


def _raw_event_page_bytes() -> int:
    try:
        value = int(os.getenv("KSADK_HISTORY_EVENT_PAGE_BYTES", str(512 * 1024)))
        if value > 0:
            return min(4 * 1024 * 1024, max(64 * 1024, value))
    except ValueError:
        pass
    logging.getLogger(__name__).warning("Invalid KSADK_HISTORY_EVENT_PAGE_BYTES; using 512 KiB")
    return 512 * 1024


async def _raw_event_page(service, query):
    """Use a byte prefix only at the public HTTP boundary, never in replay."""
    maximum = _raw_event_page_bytes()
    fetch = getattr(service, "get_raw_event_page", None)
    events = (await fetch(query, byte_limit=maximum) if callable(fetch)
              else await service.query_events(query))
    key = ((lambda e: (e.session_id, e.seq_id, e.id)) if query.order_by_seq else
           (lambda e: (e.timestamp, e.session_id, e.seq_id, e.id)))
    ordered = sorted(events, key=key, reverse=not query.from_start)
    selected = []
    used = 0
    for event in ordered:
        size = len(json.dumps(_event_to_action_payload(event), ensure_ascii=False,
                              default=str).encode("utf-8")) + 1024
        if selected and used + size > maximum:
            break
        selected.append(event)
        used += size
    return selected if query.from_start else selected[::-1]


@sessions_router.post("/agentengine/api/v1/ListSessionEvents")
async def list_session_events_action(
    request: ListSessionEventsActionRequest,
    invocation_identity: PlatformIdentityContext = Depends(resolve_trusted_invocation_identity),
):
    _executor, launch_context = get_runtime_execution()
    service = deps.resolve_session_service()
    session_id = str(request.SessionId or "").strip()
    event_types = request.EventTypes or None
    checkpoint_ids = request.CheckpointIds or None
    if not session_id:
        # 未传 SessionId：返回该 agent 全部会话的事件（存储层跨会话查询，真分页无截断；
        # seq 游标是会话内序号，跨会话模式下不适用，直接忽略）
        agent_id = str(request.AgentId or "").strip() or _runtime_agent_id(launch_context)
        session_ids = None
        if request.UserId is not None or not invocation_identity.is_empty:
            session_ids = [
                session.id
                for session in await _list_identity_session_metadata(
                    service,
                    agent_id=agent_id,
                    user_id=request.UserId,
                    invocation_identity=invocation_identity,
                )
            ]
        if not invocation_identity.is_empty and not session_ids:
            return _action_response(
                "ListSessionEvents",
                {
                    "Events": [],
                    "Total": 0,
                    "Offset": request.Offset or 0,
                    "Limit": request.Limit or 0,
                    "AfterSeqId": request.AfterSeqId,
                    "BeforeSeqId": request.BeforeSeqId,
                    "CheckpointIds": request.CheckpointIds,
                    "EventTypes": request.EventTypes,
                    "ScopedAllSessions": True,
                },
            )
        query = SessionEventQuery(
            session_ids=session_ids,
            agent_id=agent_id,
            offset=request.Offset or 0,
            limit=request.Limit,
            event_types=event_types,
            checkpoint_ids=checkpoint_ids,
        )
        events = await service.query_events(query)
        total = await service.count_event_query(
            SessionEventQuery(
                session_ids=session_ids,
                agent_id=agent_id,
                event_types=event_types,
                checkpoint_ids=checkpoint_ids,
            )
        )
        events.sort(
            key=lambda event: (event.timestamp, event.session_id, event.seq_id, event.id),
            reverse=True,
        )
        return _action_response(
            "ListSessionEvents",
            {
                "Events": [_event_to_action_payload(event) for event in events],
                "Total": total,
                "Offset": request.Offset or 0,
                "Limit": request.Limit if request.Limit is not None else len(events),
                "AfterSeqId": request.AfterSeqId,
                "BeforeSeqId": request.BeforeSeqId,
                "CheckpointIds": request.CheckpointIds,
                "EventTypes": request.EventTypes,
                "ScopedAllSessions": True,
            },
        )
    session = await _require_identity_action_session(
        service,
        session_id=session_id,
        agent_id=request.AgentId,
        user_id=request.UserId,
        invocation_identity=invocation_identity,
        history_only=True,
    )
    epoch, is_live = _history_identity(service, session)
    _validate_history_cursor(
        epoch=epoch, is_live=is_live, requested_epoch=request.HistoryEpoch,
        cursor_source=request.CursorSource,
        has_cursor=(request.AfterSeqId or 0) > 0 or (request.BeforeSeqId or 0) > 0,
    )
    forward = request.EventOrder == "session_seq"
    seq_page = forward or (request.BeforeSeqId is not None and not request.Offset)
    max_seq = getattr(service, "get_max_seq_id", None)
    newest_seq = (await max_seq(session_id) if callable(max_seq)
                  else max((event.seq_id for event in await service.get_events(
                      session_id, limit=1)), default=0))
    snapshot_seq = (request.SnapshotSeqId if request.SnapshotSeqId is not None
                    else newest_seq)
    snapshot_upper = min(
        value for value in (request.BeforeSeqId, snapshot_seq + 1) if value is not None
    )
    query = SessionEventQuery(
        session_ids=[session_id],
        agent_id=session.agent_id,
        offset=request.Offset or 0,
        limit=request.Limit + 1 if seq_page else request.Limit,
        after_seq_id=request.AfterSeqId,
        before_seq_id=snapshot_upper,
        event_types=event_types,
        checkpoint_ids=checkpoint_ids,
        from_start=forward,
        order_by_seq=seq_page,
    )
    events = await _raw_event_page(service, query)
    if seq_page:
        events = events[:request.Limit] if forward else events[-request.Limit:]
    total = await service.count_event_query(
        SessionEventQuery(
            session_ids=[session_id],
            agent_id=session.agent_id,
            after_seq_id=request.AfterSeqId,
            before_seq_id=snapshot_upper,
            event_types=event_types,
            checkpoint_ids=checkpoint_ids,
        )
    )
    has_more = total > (request.Offset or 0) + len(events)
    if forward:
        events.sort(key=lambda event: (event.seq_id, event.id))
    elif seq_page:
        events.sort(key=lambda event: (event.seq_id, event.id), reverse=True)
    elif not seq_page:
        events.sort(
            key=lambda event: (event.timestamp, event.session_id, event.seq_id, event.id),
            reverse=True,
        )
    return _action_response(
        "ListSessionEvents",
        {
            "Events": [_event_to_action_payload(event) for event in events],
            "Total": total,
            "Offset": request.Offset or 0,
            "Limit": request.Limit if request.Limit is not None else len(events),
            "AfterSeqId": request.AfterSeqId,
            "BeforeSeqId": request.BeforeSeqId,
            "CheckpointIds": request.CheckpointIds,
            "EventTypes": request.EventTypes,
            "HistoryEpoch": epoch,
            "SnapshotSeqId": snapshot_seq,
            "PersistenceDegraded": is_live,
            "DataSource": "runtime",
            "Degraded": False,
            "CursorSource": "runtime",
            "CursorCompatibleWithRuntime": True,
            "SessionContractVersion": 2,
            "HasMore": has_more,
            "NextOffset": (request.Offset or 0) + len(events) if has_more else None,
            "PageLimitedByBytes": has_more and len(events) < request.Limit,
            **({
                "NextCursor": events[-1].seq_id if events else (request.AfterSeqId or 0) if forward else None,
            } if seq_page else {}),
            **({
                "EventOrder": "session_seq",
            } if forward else {}),
        },
    )


@sessions_router.post("/agentengine/api/v1/ListSessionMessages")
async def list_session_messages_action(
    request: ListSessionMessagesActionRequest,
    invocation_identity: PlatformIdentityContext = Depends(resolve_trusted_invocation_identity),
):
    service = deps.resolve_session_service()
    from ksadk.server.history_page_cache import cache_for, eligible, bounded_pg_read

    async def load_scope():
        newest_seq = None
        combined = getattr(service, 'get_session_header_with_watermark', None)

        async def header_with_watermark(session_id):
            nonlocal newest_seq
            header, newest_seq = await combined(session_id)
            return header

        session = await _require_identity_action_session(
            service,
            session_id=request.SessionId,
            agent_id=request.AgentId,
            user_id=request.UserId,
            invocation_identity=invocation_identity,
            history_only=True,
            history_header_loader=(header_with_watermark
                                   if callable(combined) and bounded_pg_read(service, False) else None),
        )
        epoch, is_live = _history_identity(service, session)
        max_seq = getattr(service, "get_max_seq_id", None)
        if newest_seq is None:
            newest_seq = (await max_seq(request.SessionId) if callable(max_seq)
                          else max((event.seq_id for event in await service.get_events(
                              request.SessionId, limit=1)), default=0))
        return session, epoch, is_live, newest_seq

    if eligible(service, False):
        # Includes the full verified principal (including actor), Agent and
        # legacy UserId hint. Never reuse authorization after the SQL finishes.
        scope_key = (request.SessionId, request.AgentId, request.UserId, invocation_identity)
        session, epoch, is_live, newest_seq = await cache_for(service).lookup(scope_key, load_scope)
    else:
        session, epoch, is_live, newest_seq = await load_scope()
    _validate_history_cursor(
        epoch=epoch, is_live=is_live, requested_epoch=request.HistoryEpoch,
        cursor_source=request.CursorSource,
        has_cursor=(request.AfterSeqId or 0) > 0 or (request.BeforeSeqId or 0) > 0,
    )
    snapshot_seq = (request.SnapshotSeqId if request.SnapshotSeqId is not None
                    else newest_seq)
    if eligible(service, is_live):
        import json
        from starlette.responses import Response

        key = (epoch, newest_seq, snapshot_seq, invocation_identity.user_scope,
               request.model_dump_json())

        async def produce():
            # Eligible clean PostgreSQL reads complete several adjacent event
            # windows serially. Avoid a pool reset/network round trip between
            # every window; retain normal cleanup once the projection ends.
            primary = getattr(service, 'primary', service)
            async with primary.history_read_scope():
                result = await _project_session_message_page(
                    request, service, invocation_identity, epoch, is_live, snapshot_seq)
            if _history_identity(service, session) != (epoch, is_live):
                from ksadk.sessions.errors import HistoryReloadRequired
                raise HistoryReloadRequired("History source changed during projection")
            return result["Data"]

        body = await cache_for(service).read(key, produce)
        if _history_identity(service, session) != (epoch, is_live):
            from ksadk.sessions.errors import HistoryReloadRequired
            raise HistoryReloadRequired("History source changed during read")
        # Each request keeps its own envelope/request id. The immutable Data
        # bytes are serialized once, rather than encoding the same MiB of
        # tools independently for every concurrent reader.
        envelope = _action_response("ListSessionMessages", None)
        del envelope["Data"]
        prefix = json.dumps(envelope, separators=(",", ":"))[:-1].encode()
        return Response(prefix + b',"Data":' + body + b'}', media_type="application/json")
    return await _project_session_message_page(
        request, service, invocation_identity, epoch, is_live, snapshot_seq)


async def _project_session_message_page(request, service, invocation_identity, epoch, is_live, snapshot_seq):
    from ksadk.sessions.history_budget import CURRENT_MESSAGE_PROJECTION

    token = CURRENT_MESSAGE_PROJECTION.set(True)
    try:
        return await _project_readable_session_message_page(
            request, service, invocation_identity, epoch, is_live, snapshot_seq)
    finally:
        CURRENT_MESSAGE_PROJECTION.reset(token)


async def _project_readable_session_message_page(request, service, invocation_identity, epoch, is_live, snapshot_seq):
    from ksadk.conversations.message_projection import project_session_messages
    from ksadk.server.history_page_cache import bounded_pg_read
    snapshot_upper = min(
        value for value in (request.BeforeSeqId, snapshot_seq + 1) if value is not None
    )
    message_window = getattr(service, 'get_message_event_window', None)
    if callable(message_window) and bounded_pg_read(service, is_live):
        events = await message_window(
            request.SessionId, limit=_HISTORY_INITIAL_EVENTS,
            from_start=request.AfterSeqId is not None,
            after_seq_id=request.AfterSeqId, before_seq_id=snapshot_upper,
        )
    elif request.AfterSeqId is not None:
        events = await service.query_events(SessionEventQuery(
            session_ids=[request.SessionId], from_start=True, order_by_seq=True,
            limit=_HISTORY_INITIAL_EVENTS, after_seq_id=request.AfterSeqId,
            before_seq_id=snapshot_upper,
        ))
    else:
        events = await service.get_events(
            request.SessionId, limit=_HISTORY_INITIAL_EVENTS,
            before_seq_id=snapshot_upper,
        )
    budget = _HistoryProjectionBudget()
    budget.account(events)
    boundary_hint: dict[str, bool] = {}
    projection_cache: dict[str, dict[str, Any]] = {}

    async def project(
        events_to_project: list[SessionEvent],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        serialized = [_event_to_action_payload(event) for event in events_to_project]
        from ksadk.kernel.bootstrap import get_agent_kernel_runtime
        from ksadk.kernel.history_projection import project_inbox_inputs

        message_events = await project_inbox_inputs(
            serialized, runtime=get_agent_kernel_runtime(), session_id=request.SessionId,
            tenant_id=invocation_identity.tenant_id,
            request_cache=projection_cache,
        )
        budget.account_enrichment(serialized, message_events)
        return serialized, project_session_messages(
            message_events,
            include_reasoning=request.IncludeReasoning,
            include_tool_events=request.IncludeToolEvents,
            include_attachments=request.IncludeAttachments,
            session_id=request.SessionId,
        )

    if request.AfterSeqId is None:
        from ksadk.kernel.bootstrap import get_agent_kernel_runtime
        from ksadk.kernel.history_projection import complete_history_input_boundary
        events = await complete_history_input_boundary(
            events, service=service, runtime=get_agent_kernel_runtime(),
            session_id=request.SessionId, budget=budget, request_cache=projection_cache,
        )
    serialized_events, messages = await project(events)
    if request.AfterSeqId is not None:
        if events:
            events = await _extend_invocation_after_window(
                service,
                request.SessionId,
                events,
                before_seq_id=snapshot_upper,
                budget=budget,
            )
            serialized_events, messages = await project(events)
        page = messages
        # Keep complete invocation groups, including a first group larger than
        # the requested message limit.
        if len(page) > request.Limit:
            end = request.Limit
            boundary = int(page[end - 1].get("StartSeqId") or 0)
            while end < len(page) and int(page[end].get("StartSeqId") or 0) == boundary:
                end += 1
            page = page[:end]
        last_seq = events[-1].seq_id if events else request.AfterSeqId
        has_more = len(page) < len(messages) or bool(await service.query_events(SessionEventQuery(
            session_ids=[request.SessionId], from_start=True, order_by_seq=True,
            limit=1, after_seq_id=last_seq, before_seq_id=snapshot_upper,
        )))
        next_cursor = None
    else:
        page_start = len(messages)
        while page_start > 0:
            group_start_seq_id = int(messages[page_start - 1].get("StartSeqId") or 0)
            group_start_index = next(
                (
                    index
                    for index, message in enumerate(messages[:page_start])
                    if int(message.get("StartSeqId") or 0) == group_start_seq_id
                ),
                page_start - 1,
            )
            current_size = len(messages) - page_start
            group_size = page_start - group_start_index
            if current_size and current_size + group_size > request.Limit:
                break
            page_start = group_start_index
            if len(messages) - page_start >= request.Limit:
                break
        page = messages[page_start:]
        if (
            page
            and events
            and _event_invocation_id(events[0])
            and any(
                str(message.get("InvocationId") or "") == _event_invocation_id(events[0])
                for message in page
            )
        ):
            events = await _extend_invocation_before_window(
                service,
                request.SessionId,
                events,
                after_seq_id=request.AfterSeqId,
                budget=budget,
                boundary_hint=boundary_hint,
            )
            serialized_events, messages = await project(events)
            page_start = len(messages)
            while page_start > 0:
                group_start_seq_id = int(messages[page_start - 1].get("StartSeqId") or 0)
                group_start_index = next(
                    (
                        index
                        for index, message in enumerate(messages[:page_start])
                        if int(message.get("StartSeqId") or 0) == group_start_seq_id
                    ),
                    page_start - 1,
                )
                current_size = len(messages) - page_start
                group_size = page_start - group_start_index
                if current_size and current_size + group_size > request.Limit:
                    break
                page_start = group_start_index
                if len(messages) - page_start >= request.Limit:
                    break
            page = messages[page_start:]
        minimum_start_seq_id = int(page[0].get("StartSeqId") or 0) if page else 0
        has_more = page_start > 0
        if not has_more and 'has_more' in boundary_hint:
            has_more = boundary_hint['has_more']
        elif not has_more and events:
            probe = getattr(service, "has_events", None)
            bounds = dict(after_seq_id=request.AfterSeqId, before_seq_id=events[0].seq_id)
            has_more = (await probe(request.SessionId, **bounds) if callable(probe)
                        else bool(await service.get_events(request.SessionId, limit=1, **bounds)))
        # BeforeSeqId is exclusive. Use the invocation group boundary so the
        # next page cannot duplicate reasoning/tools or skip its user message.
        next_cursor = (minimum_start_seq_id or (events[0].seq_id if events else 0)) if has_more else None
    latest_seq_id = (
        int(page[-1].get("SeqId") or 0)
        if page
        else max(
            (int(event.get("SeqId") or 0) for event in serialized_events),
            default=int(request.AfterSeqId or 0),
        )
    )
    return _action_response(
        "ListSessionMessages",
        {
            "SessionId": request.SessionId,
            "Messages": page,
            "LatestSeqId": latest_seq_id,
            "HasMore": has_more,
            "NextCursor": next_cursor,
            "DataSource": "runtime",
            "CursorSource": "runtime",
            "HistoryEpoch": epoch,
            "SnapshotSeqId": snapshot_seq,
            "PersistenceDegraded": is_live,
            "Degraded": False,
            "CursorCompatibleWithRuntime": True,
            "SessionContractVersion": 2,
        },
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
) -> dict[str, Any]:
    service = deps.resolve_session_service()
    session_ids = _normalize_action_id_list(request.SessionId)
    checkpoint_ids = _normalize_action_id_list(request.CheckpointId)
    if session_ids:
        for session_id in session_ids:
            await _require_identity_action_session(
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
            for session in await _list_identity_session_metadata(
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
                            checkpoint["Timestamp"], checkpoint["SessionId"],
                            checkpoint["SeqId"], sequence,
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
    checkpoints = checkpoints[offset:offset + int(request.Limit)]
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


@sessions_router.post("/agentengine/api/v1/ListSessionCheckpoints")
async def list_session_checkpoints_action(
    request: ListSessionCheckpointsActionRequest,
    invocation_identity: PlatformIdentityContext = Depends(resolve_trusted_invocation_identity),
):
    return _action_response(
        "ListSessionCheckpoints",
        await _list_checkpoints_payload(request, invocation_identity),
    )


@tools_router.post("/agentengine/api/v1/ListToolReceipts")
async def list_tool_receipts_action(
    request: ListToolReceiptsActionRequest,
    invocation_identity: PlatformIdentityContext = Depends(resolve_trusted_invocation_identity),
):
    service = deps.resolve_session_service()
    await _require_identity_action_session(
        service,
        session_id=request.SessionId,
        agent_id=request.AgentId,
        user_id=request.UserId,
        invocation_identity=invocation_identity,
        history_only=True,
    )

    run_id_filter = str(request.RunId or "").strip()
    checkpoint_id_filter = str(request.CheckpointId or "").strip()
    offset = int(request.Offset)
    limit = int(request.Limit)
    filtered_query = SessionEventQuery(
        session_ids=[request.SessionId], receipt_only=True,
        run_id=run_id_filter or None,
        checkpoint_id=checkpoint_id_filter or None,
    )
    total = await service.count_event_query(filtered_query)
    events = await service.query_events(SessionEventQuery(
        **{**filtered_query.__dict__, "offset": offset, "limit": limit}
    ))
    receipts = [receipt for event in events
                if (receipt := _tool_receipt_event_to_action_payload(event)) is not None]
    receipts.reverse()

    return _action_response(
        "ListToolReceipts",
        {
            "ToolReceipts": receipts,
            "Total": total,
            "Offset": offset,
            "Limit": limit,
        },
    )
