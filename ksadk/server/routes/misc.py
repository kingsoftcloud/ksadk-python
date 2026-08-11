"""Static UI, builder, trace, feedback, and ADK compatibility routes."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping
from typing import Any, Dict, List, Optional, cast

from fastapi import HTTPException, Request

from ksadk.sessions import SessionEvent
from ksadk.tracing import get_memory_exporter

from . import dependencies as deps
from .common import (
    _action_response,
    _ensure_session,
    _hydrate_session,
    _resolve_ui_static_response,
)
from .models import ResponseFeedbackRefActionRequest, UpsertResponseFeedbackActionRequest
from .routers import (
    builder_router,
    debug_router,
    feedback_router,
    health_meta_router,
    models_router,
    sessions_adk_compat_router,
)
from .workspace import ListAgentModelsRequest, _build_models_payload


@health_meta_router.get("/{requested_path:path}", include_in_schema=False)
async def serve_agent_ui_static(requested_path: str):
    response = _resolve_ui_static_response(requested_path)
    if response is not None:
        return response
    raise HTTPException(status_code=404, detail="Not Found")


# ============================================================
# goal-01: create_runtime_app factory 装配(普通 runtime app 与 HarnessApp 共用入口)
# ============================================================

# ---- builder 域(ADK-Web stub)----


@builder_router.get("/builder/app/{app_name}")
async def get_agent_builder(
    app_name: str, ts: int = 0, tmp: bool = False, file_path: Optional[str] = None
):
    """Get agent builder config - stub for ADK-Web"""
    # Return minimal YAML config for non-ADK projects
    return f"""name: {app_name}
model: glm-5.1
description: {app_name} agent
instruction: You are a helpful assistant.
"""


@builder_router.post("/builder/save")
async def save_agent_builder(request: Request, tmp: bool = False):
    """Save agent builder config - stub for ADK-Web"""
    return True


# ---- models 域 ----


@models_router.post("/agentengine/api/v1/ListAgentModels")
async def list_agent_models_action(_request: ListAgentModelsRequest):
    payload = await _build_models_payload()
    return _action_response(
        "ListAgentModels",
        {
            "Models": payload.get("data", []),
            "Current": payload.get("current"),
            "Source": payload.get("source", ""),
        },
    )


# ---- debug 域(OpenTelemetry trace 查询)----


@debug_router.get("/debug/trace/session/{session_id}")
async def get_session_trace(session_id: str):
    """Get traces for a session - returns array of Span objects"""
    exporter = get_memory_exporter()
    if not exporter:
        return []  # Return empty array, not object

    # Get all spans and transform to ADK-Web expected format
    raw_spans = exporter.get_finished_spans()

    # Get session events for invocation mapping
    service = deps.resolve_session_service()
    events = await service.get_events(session_id)

    # Build invocation ID mapping from session events
    invocation_ids = {}
    for event in events:
        if event.id and event.invocation_id:
            invocation_ids[event.id] = event.invocation_id

    # Transform spans to ADK-Web format
    spans = []
    for span in raw_spans:
        # Use session_id as trace_id for grouping
        trace_id = span.get("trace_id", session_id)

        # Get or create invocation_id
        invocation_id = span.get("attributes", {}).get("gcp.vertex.agent.invocation_id")
        if not invocation_id:
            # Try to derive from event association
            invocation_id = trace_id[:36] if len(trace_id) >= 36 else trace_id

        # Build attributes with required ADK fields
        attrs = span.get("attributes", {}).copy()
        attrs["gcp.vertex.agent.invocation_id"] = invocation_id

        # If this is a LLM span, add request/response
        if "llm" in span.get("name", "").lower() or "invoke" in span.get("name", "").lower():
            if "user.input" in attrs:
                attrs["gcp.vertex.agent.llm_request"] = json.dumps(
                    {
                        "contents": [
                            {"role": "user", "parts": [{"text": attrs.get("user.input", "")}]}
                        ]
                    }
                )
            if "agent.output" in attrs:
                attrs["gcp.vertex.agent.llm_response"] = json.dumps(
                    {
                        "candidates": [
                            {
                                "content": {
                                    "role": "model",
                                    "parts": [{"text": attrs.get("agent.output", "")}],
                                }
                            }
                        ]
                    }
                )

        formatted_span = {
            "trace_id": trace_id,
            "span_id": span.get("span_id", str(uuid.uuid4())[:16]),
            "parent_span_id": span.get("parent_span_id"),
            "name": span.get("name", "unknown"),
            "start_time": span.get("start_time", 0),
            "end_time": span.get("end_time", 0),
            "attributes": attrs,
            "status": span.get("status", {}),
        }
        spans.append(formatted_span)

    return spans  # Return array directly


@debug_router.get("/debug/trace/{event_id}")
async def get_event_trace(event_id: str):
    """Get trace for a specific event - returns array of Span objects"""
    exporter = get_memory_exporter()
    if not exporter:
        return []

    spans = exporter.get_finished_spans()
    # Filter by event_id or return recent spans
    filtered = [s for s in spans if s.get("attributes", {}).get("event_id") == event_id]
    return filtered if filtered else spans[-10:]


@debug_router.get("/traces")
async def get_traces(limit: int = 50):
    """Get recent traces (OpenTelemetry)"""
    exporter = get_memory_exporter()
    if not exporter:
        return {"traces": []}

    spans = exporter.get_finished_spans()
    traces = []
    for span in spans[-limit:]:
        traces.append(
            {
                "name": span.get("name", "unknown"),
                "status": span.get("status", {}).get("code", "UNSET"),
                "start_time": span.get("start_time"),
                "end_time": span.get("end_time"),
                "attributes": span.get("attributes", {}),
            }
        )
    return {"traces": traces}


# ---- feedback 域 ----


def _feedback_state_key(response_id: str) -> str:
    return str(response_id or "").strip()


def _feedback_payload_from_state(item: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    rating = str(item.get("Rating") or item.get("rating") or "").strip().lower()
    if rating not in {"up", "down"}:
        return None
    return {
        "AgentId": str(item.get("AgentId") or item.get("agent_id") or ""),
        "SessionId": str(item.get("SessionId") or item.get("session_id") or ""),
        "ResponseId": str(item.get("ResponseId") or item.get("response_id") or ""),
        "EventId": str(item.get("EventId") or item.get("event_id") or ""),
        "Rating": rating,
        "Comment": str(item.get("Comment") or item.get("comment") or ""),
        "TraceId": str(item.get("TraceId") or item.get("trace_id") or ""),
        "RootSpanId": str(item.get("RootSpanId") or item.get("root_span_id") or ""),
        "CreatedAt": str(item.get("CreatedAt") or item.get("created_at") or ""),
        "UpdatedAt": str(item.get("UpdatedAt") or item.get("updated_at") or ""),
    }


async def _find_feedback_assistant_event(
    *,
    session_id: str,
    response_id: str,
    event_id: str | None = None,
) -> SessionEvent | None:
    events = await deps.resolve_session_service().get_events(session_id)
    normalized_event_id = str(event_id or "").strip()
    normalized_response_id = str(response_id or "").strip()
    for event in reversed(events):
        if normalized_event_id and event.id != normalized_event_id:
            continue
        metadata = event.metadata or {}
        if (
            normalized_response_id
            and str(metadata.get("response_id") or "") != normalized_response_id
        ):
            continue
        event_type = deps.conversation().canonical_event_type(
            event.event_type,
            author=event.author,
            role=str((event.content or {}).get("role") or ""),
        )
        if event_type == "assistant_message":
            return cast(SessionEvent, event)
    return None


@feedback_router.post("/agentengine/api/v1/GetResponseFeedback")
async def get_response_feedback_action(request: ResponseFeedbackRefActionRequest):
    session = await deps.resolve_session_service().get_session(request.SessionId)
    if not session or session.agent_id != request.AgentId:
        return _action_response("GetResponseFeedback", {"Feedback": None})
    feedbacks = session.state.get("__ksadk_response_feedback__")
    feedback = None
    if isinstance(feedbacks, Mapping):
        feedback = _feedback_payload_from_state(
            feedbacks.get(_feedback_state_key(request.ResponseId))
        )
    return _action_response("GetResponseFeedback", {"Feedback": feedback})


@feedback_router.post("/agentengine/api/v1/UpsertResponseFeedback")
async def upsert_response_feedback_action(request: UpsertResponseFeedbackActionRequest):
    rating = str(request.Rating or "").strip().lower()
    if rating not in {"up", "down"}:
        raise HTTPException(status_code=400, detail="Feedback rating must be up or down")

    service = deps.resolve_session_service()
    session = await service.get_session(request.SessionId)
    if not session or session.agent_id != request.AgentId:
        raise HTTPException(status_code=404, detail="Session not found")

    assistant_event = await _find_feedback_assistant_event(
        session_id=request.SessionId,
        response_id=request.ResponseId,
        event_id=request.EventId,
    )
    if assistant_event is None:
        raise HTTPException(status_code=404, detail="Assistant response not found")

    now = str(time.time())
    existing_feedbacks = session.state.get("__ksadk_response_feedback__")
    feedbacks = dict(existing_feedbacks) if isinstance(existing_feedbacks, Mapping) else {}
    existing = (
        _feedback_payload_from_state(feedbacks.get(_feedback_state_key(request.ResponseId))) or {}
    )
    metadata = assistant_event.metadata or {}
    feedback = {
        "AgentId": request.AgentId,
        "SessionId": request.SessionId,
        "ResponseId": request.ResponseId,
        "EventId": request.EventId or assistant_event.id,
        "Rating": rating,
        "Comment": request.Comment or "",
        "TraceId": request.TraceId or str(metadata.get("trace_id") or ""),
        "RootSpanId": request.RootSpanId or str(metadata.get("root_span_id") or ""),
        "CreatedAt": existing.get("CreatedAt") or now,
        "UpdatedAt": now,
    }
    feedbacks[_feedback_state_key(request.ResponseId)] = feedback
    await service.update_state(
        agent_id=session.agent_id,
        user_id=session.user_id,
        session_id=session.id,
        scope="session",
        state_delta={"__ksadk_response_feedback__": feedbacks},
    )
    return _action_response("UpsertResponseFeedback", {"Feedback": feedback})


@feedback_router.post("/agentengine/api/v1/DeleteResponseFeedback")
async def delete_response_feedback_action(request: ResponseFeedbackRefActionRequest):
    service = deps.resolve_session_service()
    session = await service.get_session(request.SessionId)
    if not session or session.agent_id != request.AgentId:
        return _action_response("DeleteResponseFeedback", {"Deleted": False})
    existing_feedbacks = session.state.get("__ksadk_response_feedback__")
    feedbacks = dict(existing_feedbacks) if isinstance(existing_feedbacks, Mapping) else {}
    deleted = feedbacks.pop(_feedback_state_key(request.ResponseId), None) is not None
    if deleted:
        await service.update_state(
            agent_id=session.agent_id,
            user_id=session.user_id,
            session_id=session.id,
            scope="session",
            state_delta={"__ksadk_response_feedback__": feedbacks},
        )
    return _action_response("DeleteResponseFeedback", {"Deleted": deleted})


# ---- sessions_adk_compat 域(ADK-Web 兼容)----


@sessions_adk_compat_router.post("/apps/{app_name}/users/{user_id}/sessions")
async def create_session(app_name: str, user_id: str, request: Request):
    """Create a new session"""
    # Check if importing existing events
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    service = deps.resolve_session_service()
    session = await _ensure_session(app_name, user_id, body.get("sessionId") or body.get("id"))

    for raw_event in body.get("events", []):
        session_event = SessionEvent.from_dict(raw_event, session_id=session.id)
        await service.append_event(session.id, session_event)

    hydrated = await _hydrate_session(await service.get_session(session.id))
    return hydrated.to_legacy_dict() if hydrated else session.to_legacy_dict()


@sessions_adk_compat_router.get("/apps/{app_name}/users/{user_id}/sessions")
async def list_sessions(app_name: str, user_id: str):
    """List all sessions for a user"""
    service = deps.resolve_session_service()
    sessions = await service.list_sessions(app_name, user_id)
    hydrated: List[Dict[str, Any]] = []
    for session in sessions:
        session.events = await service.get_events(session.id)
        hydrated.append(session.to_legacy_dict())
    return hydrated


@sessions_adk_compat_router.get("/apps/{app_name}/users/{user_id}/sessions/{session_id}")
async def get_session(app_name: str, user_id: str, session_id: str):
    """Get a specific session with its events"""
    service = deps.resolve_session_service()
    session = await _hydrate_session(await service.get_session(session_id))
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session.to_legacy_dict()


@sessions_adk_compat_router.delete("/apps/{app_name}/users/{user_id}/sessions/{session_id}")
async def delete_session(app_name: str, user_id: str, session_id: str):
    """Delete a session"""
    service = deps.resolve_session_service()
    if await service.delete_session(session_id):
        return {"status": "deleted"}
    raise HTTPException(status_code=404, detail="Session not found")


@sessions_adk_compat_router.post(
    "/apps/{app_name}/users/{user_id}/sessions/{session_id}/save_memory"
)
async def save_session_to_memory(app_name: str, user_id: str, session_id: str):
    """Fail closed until long-term memory is exposed as a Runtime capability."""

    del app_name, user_id, session_id
    raise HTTPException(
        status_code=501,
        detail={
            "code": "RUNTIME_NOT_SUPPORTED",
            "message": "当前 RuntimeAdapter 未声明长期记忆写入能力",
            "hint": "请通过 Runtime Catalog 检查长期记忆能力后重试",
        },
    )


@sessions_adk_compat_router.get(
    "/apps/{app_name}/users/{user_id}/sessions/{session_id}/events/{event_id}/graph"
)
async def get_event_graph(app_name: str, user_id: str, session_id: str, event_id: str):
    """Get event graph (DOT format) - placeholder"""
    return {"dotSrc": None}


@sessions_adk_compat_router.get("/apps/{app_name}/eval_sets")
async def list_eval_sets(app_name: str):
    """List evaluation sets - stub for ADK-Web"""
    return []


@sessions_adk_compat_router.get("/apps/{app_name}/eval_results")
async def list_eval_results(app_name: str):
    """List evaluation results - stub for ADK-Web"""
    return []
