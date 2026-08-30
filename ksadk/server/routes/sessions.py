"""Session, UI bootstrap, checkpoint-list, and tool-receipt routes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException

from ksadk.server.factory import get_runtime_execution, get_state
from ksadk.sessions import SessionEvent
from ksadk.tools.gateway import tool_approval_capability
from ksadk.toolsets import describe_agentengine_tools
from ksadk_runtime_common.workspace_files import (
    build_workspace_files_bootstrap,
    workspace_files_enabled,
)

from . import dependencies as deps
from .common import (
    _action_response,
    _build_bootstrap_model_payload,
    _build_native_terminal_capability,
    _ensure_session,
    _hydrate_session,
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
    _event_run_id,
    _runtime_agent_id,
)
from .projection import (
    _apply_checkpoint_resume_audit,
    _apply_latest_checkpoint_policy,
    _checkpoint_event_to_action_payload,
    _event_invocation_id,
    _event_to_action_payload,
    _extend_invocation_after_window,
    _extend_invocation_before_window,
    _iter_scoped_event_pages,
    _iter_session_event_pages,
    _record_resume_audit,
    _require_action_session,
    _session_to_action_payload,
    _tool_receipt_event_to_action_payload,
)
from .routers import sessions_router, tools_router, ui_bootstrap_router
from .streaming import _cancel_detached_streams_for_session


@ui_bootstrap_router.post("/agentengine/api/v1/GetAgentUiBootstrap")
async def get_agent_ui_bootstrap(request: UiBootstrapRequest):
    state = get_state()
    executor, launch_context = get_runtime_execution()
    detection = launch_context.detection
    agent_id = request.AgentId or _runtime_agent_id(launch_context)
    description = str(getattr(detection, "description", "") or "")
    framework = launch_context.runtime_type.strip().lower()
    workspace_enabled = workspace_files_enabled(default=True)
    ui_spec = _resolve_agent_ui_spec()
    runtime_capabilities = executor.native_capabilities(launch_context)
    runtime_capability_matrix = executor.capability_matrix(launch_context)
    resume_capability = (
        runtime_capabilities.get("ResumeRun")
        if isinstance(runtime_capabilities, Mapping)
        else None
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
        runtime_capabilities.get("CancelRun")
        if isinstance(runtime_capabilities, Mapping)
        else None
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
                "Attachments": True,
                "WorkspaceFiles": workspace_enabled,
                "Approval": True,
                "ApprovalPolicy": tool_approval_capability(),
                "Thinking": True,
                "StopRun": cancel_run_supported,
                "ResumeRun": checkpoint_resume_supported,
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
                "NativeTerminal": _build_native_terminal_capability(framework),
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
async def create_session_action(request: CreateSessionActionRequest):
    session = await _ensure_session(request.AgentId, request.UserId or "user", request.SessionId)
    return _action_response("CreateSession", {"Session": await _session_to_action_payload(session)})


@sessions_router.post("/agentengine/api/v1/ListSessions")
async def list_sessions_action(request: ListSessionsActionRequest):
    service = deps.resolve_session_service()
    offset = (request.Page - 1) * request.PageSize
    sessions = await service.list_sessions(
        request.AgentId,
        request.UserId,
        offset=offset,
        limit=request.PageSize,
    )
    total = await service.count_sessions(request.AgentId, request.UserId)
    session_payloads = [await _session_to_action_payload(session) for session in sessions]
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
async def get_session_action(request: SessionIdRequest):
    service = deps.resolve_session_service()
    session = await _require_action_session(
        service,
        session_id=request.SessionId,
        agent_id=request.AgentId,
        user_id=request.UserId,
    )
    hydrated = await _hydrate_session(session)
    if hydrated is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return _action_response("GetSession", {"Session": await _session_to_action_payload(hydrated)})


@sessions_router.post("/agentengine/api/v1/DeleteSession")
async def delete_session_action(request: SessionIdRequest):
    service = deps.resolve_session_service()
    await _require_action_session(
        service,
        session_id=request.SessionId,
        agent_id=request.AgentId,
        user_id=request.UserId,
    )
    await _cancel_detached_streams_for_session(request.SessionId)
    deleted = await service.delete_session(request.SessionId)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return _action_response("DeleteSession", {"Deleted": True})


@sessions_router.post("/agentengine/api/v1/ListSessionEvents")
async def list_session_events_action(request: ListSessionEventsActionRequest):
    _executor, launch_context = get_runtime_execution()
    service = deps.resolve_session_service()
    session_id = str(request.SessionId or "").strip()
    if not session_id:
        # 未传 SessionId：返回该 agent 全部会话的事件（存储层跨会话查询，真分页无截断；
        # seq 游标是会话内序号，跨会话模式下不适用，直接忽略）
        agent_id = str(request.AgentId or "").strip() or _runtime_agent_id(launch_context)
        events = await service.get_events_for_agent(
            agent_id,
            user_id=request.UserId,
            offset=request.Offset,
            limit=request.Limit,
        )
        total = await service.count_events_for_agent(agent_id, user_id=request.UserId)
        return _action_response(
            "ListSessionEvents",
            {
                "Events": [_event_to_action_payload(event) for event in events],
                "Total": total,
                "Offset": request.Offset or 0,
                "Limit": request.Limit if request.Limit is not None else len(events),
                "AfterSeqId": request.AfterSeqId,
                "BeforeSeqId": request.BeforeSeqId,
                "ScopedAllSessions": True,
            },
        )
    await _require_action_session(
        service,
        session_id=session_id,
        agent_id=request.AgentId,
        user_id=request.UserId,
    )
    events = await service.get_events(
        session_id,
        offset=request.Offset,
        limit=request.Limit,
        after_seq_id=request.AfterSeqId,
        before_seq_id=request.BeforeSeqId,
    )
    total = await service.count_events(
        session_id,
        after_seq_id=request.AfterSeqId,
        before_seq_id=request.BeforeSeqId,
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
        },
    )


@sessions_router.post("/agentengine/api/v1/ListSessionMessages")
async def list_session_messages_action(request: ListSessionMessagesActionRequest):
    from ksadk.conversations.message_projection import project_session_messages

    service = deps.resolve_session_service()
    await _require_action_session(
        service,
        session_id=request.SessionId,
        agent_id=request.AgentId,
        user_id=request.UserId,
    )
    total_events = await service.count_events(
        request.SessionId,
        after_seq_id=request.AfterSeqId,
        before_seq_id=request.BeforeSeqId,
    )
    event_offset = 0
    if request.AfterSeqId is not None and total_events > 2000:
        # Storage pagination is tail-based. Offset past the newest surplus so
        # reconnect starts with the oldest unseen window and cannot skip gaps.
        event_offset = total_events - 2000
    events = await service.get_events(
        request.SessionId,
        offset=event_offset,
        limit=2000,
        after_seq_id=request.AfterSeqId,
        before_seq_id=request.BeforeSeqId,
    )

    def project(
        events_to_project: list[SessionEvent],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        serialized = [_event_to_action_payload(event) for event in events_to_project]
        return serialized, project_session_messages(
            serialized,
            include_reasoning=request.IncludeReasoning,
            include_tool_events=request.IncludeToolEvents,
            include_attachments=request.IncludeAttachments,
        )

    serialized_events, messages = project(events)
    if request.AfterSeqId is not None:
        if total_events > len(events):
            events = await _extend_invocation_after_window(
                service,
                request.SessionId,
                events,
                before_seq_id=request.BeforeSeqId,
            )
            serialized_events, messages = project(events)
        page = messages
        has_more = total_events > len(serialized_events)
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
            and total_events > len(events)
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
            )
            serialized_events, messages = project(events)
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
        has_more = page_start > 0 or total_events > len(serialized_events)
        # BeforeSeqId is exclusive. Use the invocation group boundary so the
        # next page cannot duplicate reasoning/tools or skip its user message.
        next_cursor = minimum_start_seq_id if has_more else None
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


async def _list_checkpoints_payload(request: ListSessionCheckpointsActionRequest) -> dict[str, Any]:
    service = deps.resolve_session_service()
    run_id_filter = str(request.RunId or "").strip()
    framework_filter = str(request.Framework or "").strip().lower()

    session_id = str(request.SessionId or "").strip()
    if session_id:
        await _require_action_session(
            service,
            session_id=session_id,
            agent_id=request.AgentId,
            user_id=request.UserId,
        )

    audit_by_session: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    latest_by_session_run: dict[tuple[str, str], int] = {}
    run_seen = not run_id_filter
    async for page in _iter_scoped_event_pages(
        service,
        session_id=session_id or None,
        agent_id=request.AgentId,
        user_id=request.UserId,
    ):
        for event in page:
            if run_id_filter and _event_run_id(event) == run_id_filter:
                run_seen = True
            _record_resume_audit(audit_by_session, event)
            checkpoint = _checkpoint_event_to_action_payload(event)
            if checkpoint is None or not (checkpoint.get("Metadata") or {}).get(
                "only_latest_resumable"
            ):
                continue
            key = (event.session_id, str(checkpoint.get("RunId") or ""))
            latest_by_session_run[key] = max(
                latest_by_session_run.get(key, 0),
                int(checkpoint.get("SeqId") or 0),
            )
    if session_id and not run_seen:
        raise HTTPException(status_code=409, detail="RunId does not belong to SessionId")

    def project_checkpoint(event: SessionEvent) -> dict[str, Any] | None:
        checkpoint = _checkpoint_event_to_action_payload(event)
        if checkpoint is None:
            return None
        checkpoint = _apply_checkpoint_resume_audit(
            checkpoint,
            audit_by_session.get(event.session_id, {}),
        )
        checkpoint = _apply_latest_checkpoint_policy(
            checkpoint,
            latest_by_session_run,
            session_id=event.session_id,
        )
        if run_id_filter and checkpoint["RunId"] != run_id_filter:
            return None
        if framework_filter and str(checkpoint["Framework"]).lower() != framework_filter:
            return None
        return checkpoint

    total = 0
    resumable_total = 0
    async for page in _iter_scoped_event_pages(
        service,
        session_id=session_id or None,
        agent_id=request.AgentId,
        user_id=request.UserId,
    ):
        for event in page:
            checkpoint = project_checkpoint(event)
            if checkpoint is None:
                continue
            if checkpoint.get("IsResumable") is True:
                resumable_total += 1
            if request.OnlyResumable and checkpoint.get("IsResumable") is not True:
                continue
            total += 1

    offset = int(request.Offset or 0)
    limit = int(request.Limit)
    window_end = max(total - offset, 0)
    window_start = max(window_end - limit, 0)
    checkpoints: list[dict[str, Any]] = []
    filtered_index = 0
    async for page in _iter_scoped_event_pages(
        service,
        session_id=session_id or None,
        agent_id=request.AgentId,
        user_id=request.UserId,
    ):
        for event in page:
            checkpoint = project_checkpoint(event)
            if checkpoint is None:
                continue
            if request.OnlyResumable and checkpoint.get("IsResumable") is not True:
                continue
            if window_start <= filtered_index < window_end:
                checkpoints.append(checkpoint)
            filtered_index += 1
            if filtered_index >= window_end:
                break
        if filtered_index >= window_end:
            break

    return {
        "Checkpoints": checkpoints,
        "Total": total,
        "ResumableTotal": resumable_total,
        "HasResumableCheckpoint": resumable_total > 0,
        "Offset": offset,
        "Limit": limit,
    }


@sessions_router.post("/agentengine/api/v1/ListSessionCheckpoints")
async def list_session_checkpoints_action(request: ListSessionCheckpointsActionRequest):
    return _action_response("ListSessionCheckpoints", await _list_checkpoints_payload(request))


@tools_router.post("/agentengine/api/v1/ListToolReceipts")
async def list_tool_receipts_action(request: ListToolReceiptsActionRequest):
    service = deps.resolve_session_service()
    await _require_action_session(
        service,
        session_id=request.SessionId,
        agent_id=request.AgentId,
        user_id=request.UserId,
    )

    run_id_filter = str(request.RunId or "").strip()
    checkpoint_id_filter = str(request.CheckpointId or "").strip()
    total = 0
    async for page in _iter_session_event_pages(service, request.SessionId):
        for event in page:
            receipt = _tool_receipt_event_to_action_payload(event)
            if receipt is None:
                continue
            if run_id_filter and receipt["RunId"] != run_id_filter:
                continue
            if checkpoint_id_filter and receipt["CheckpointId"] != checkpoint_id_filter:
                continue
            total += 1

    offset = int(request.Offset)
    limit = int(request.Limit)
    window_end = max(total - offset, 0)
    window_start = max(window_end - limit, 0)
    receipts: list[dict[str, Any]] = []
    filtered_index = 0
    async for page in _iter_session_event_pages(service, request.SessionId):
        for event in page:
            receipt = _tool_receipt_event_to_action_payload(event)
            if receipt is None:
                continue
            if run_id_filter and receipt["RunId"] != run_id_filter:
                continue
            if checkpoint_id_filter and receipt["CheckpointId"] != checkpoint_id_filter:
                continue
            if window_start <= filtered_index < window_end:
                receipts.append(receipt)
            filtered_index += 1
            if filtered_index >= window_end:
                break
        if filtered_index >= window_end:
            break

    return _action_response(
        "ListToolReceipts",
        {
            "ToolReceipts": receipts,
            "Total": total,
            "Offset": offset,
            "Limit": limit,
        },
    )
