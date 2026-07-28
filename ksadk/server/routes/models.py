"""Runtime HTTP request models and pure projection helpers."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

from ksadk.conversations.run_kinds import RUN_MODE_UNKNOWN, RUN_TRIGGER_UNKNOWN
from ksadk.conversations.run_status import RUN_STATUS_ACTIVE, RUN_STATUS_TERMINAL
from ksadk.runners.base_runner import BaseRunner
from ksadk.sessions import SessionEvent

_RUN_TERMINAL_STATUSES = RUN_STATUS_TERMINAL
_RUN_ACTIVE_STATUSES = RUN_STATUS_ACTIVE
_EVENT_SCAN_PAGE_SIZE = 500
_MAX_PREVIEW_TOOL_RECEIPTS = 500
_RUNTIME_RUN_STATUS_BY_EVENT_TYPE = {
    "run.started": "in_progress",
    "run.progress": "in_progress",
    "run.interrupted": "interrupted",
    "run.completed": "completed",
    "run.failed": "failed",
    "run.canceled": "cancelled",
}


def _parse_iso_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class UiBootstrapRequest(BaseModel):
    AgentId: Optional[str] = None
    SessionId: Optional[str] = None


class CreateSessionActionRequest(BaseModel):
    AgentId: str
    UserId: Optional[str] = "user"
    SessionId: Optional[str] = None


class ListSessionsActionRequest(BaseModel):
    AgentId: str
    UserId: Optional[str] = None
    Page: int = Field(1, ge=1)
    PageSize: int = Field(20, ge=1, le=200)


class SessionIdRequest(BaseModel):
    SessionId: str
    AgentId: Optional[str] = None
    UserId: Optional[str] = None


class ListSessionEventsActionRequest(BaseModel):
    AgentId: Optional[str] = None
    SessionId: Optional[str] = None
    UserId: Optional[str] = None
    Offset: Optional[int] = Field(None, ge=0)
    Limit: int = Field(200, ge=1, le=2000)
    AfterSeqId: Optional[int] = Field(None, ge=0)
    BeforeSeqId: Optional[int] = Field(None, ge=1)


class ListSessionMessagesActionRequest(BaseModel):
    AgentId: Optional[str] = None
    UserId: Optional[str] = None
    SessionId: str
    AfterSeqId: Optional[int] = Field(None, ge=0)
    BeforeSeqId: Optional[int] = Field(None, ge=1)
    Limit: int = Field(50, ge=1, le=200)
    IncludeReasoning: bool = False
    IncludeToolEvents: bool = False
    IncludeAttachments: bool = True


class ListSessionCheckpointsActionRequest(BaseModel):
    AgentId: str
    SessionId: Optional[str] = None
    UserId: Optional[str] = None
    RunId: Optional[str] = None
    OnlyResumable: bool = False
    Framework: Optional[str] = None
    Offset: Optional[int] = Field(None, ge=0)
    Limit: int = Field(100, ge=1, le=500)


class ListToolReceiptsActionRequest(BaseModel):
    AgentId: str
    UserId: Optional[str] = None
    SessionId: str
    RunId: Optional[str] = None
    CheckpointId: Optional[str] = None
    Offset: int = Field(0, ge=0)
    Limit: int = Field(200, ge=1, le=500)


class ResumeRunActionRequest(BaseModel):
    AgentId: str
    UserId: Optional[str] = None
    SessionId: str
    RunId: str
    CheckpointId: str
    ResumeAttemptId: Optional[str] = None
    InvocationId: Optional[str] = None
    Stream: bool = False
    Model: Optional[str] = None
    ModelMetadata: Optional[Dict[str, Any]] = None
    ModelOptions: Optional[Dict[str, Any]] = None
    Metadata: Optional[Dict[str, Any]] = None
    ResumeInstructionEnabled: bool = False
    ResumeInstruction: Optional[str] = None


class GetCheckpointResumePreviewActionRequest(BaseModel):
    AgentId: str
    UserId: Optional[str] = None
    SessionId: str
    RunId: str
    CheckpointId: str


class RunAgentActionRequest(BaseModel):
    AgentId: str
    Messages: List[Dict[str, Any]] = Field(default_factory=list)
    UserId: Optional[str] = "user"
    AccountId: Optional[str] = None
    SessionId: Optional[str] = None
    InvocationId: Optional[str] = None
    ApiFormat: str = "responses"
    Stream: bool = False
    Background: bool = False  # 立即返回 job 句柄，后台执行，进度走 SubscribeRunEvents
    Model: Optional[str] = None
    ModelMetadata: Optional[Dict[str, Any]] = None
    ModelOptions: Optional[Dict[str, Any]] = None
    Metadata: Optional[Dict[str, Any]] = None
    ResponsesInput: Optional[Any] = None
    PreviousResponseId: Optional[str] = None


class ResponseFeedbackRefActionRequest(BaseModel):
    AgentId: str
    SessionId: str
    ResponseId: str


class UpsertResponseFeedbackActionRequest(ResponseFeedbackRefActionRequest):
    Rating: str
    Comment: Optional[str] = ""
    EventId: Optional[str] = None
    TraceId: Optional[str] = None
    RootSpanId: Optional[str] = None


class ResponsesRequest(BaseModel):
    input: Any
    model: Optional[str] = None
    model_metadata: Optional[Dict[str, Any]] = None
    model_options: Optional[Dict[str, Any]] = None
    instructions: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    conversation: Optional[Any] = None
    safety_identifier: Optional[str] = None
    prompt_cache_key: Optional[str] = None
    user: Optional[str] = None
    account_id: Optional[str] = None
    store: Optional[bool] = None
    previous_response_id: Optional[str] = None
    stream: bool = False
    session_id: Optional[str] = None

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if value is None:
            return None
        public_items = [(key, item) for key, item in value.items() if key != "agentengine"]
        if len(public_items) > 16:
            raise ValueError("Responses metadata supports at most 16 key-value pairs")
        for key, item in public_items:
            if len(key) > 64:
                raise ValueError("Responses metadata keys must be at most 64 characters")
            if not isinstance(item, str):
                raise ValueError("Responses metadata values must be strings")
            if len(item) > 512:
                raise ValueError("Responses metadata values must be at most 512 characters")
        return value


class WorkspaceListActionRequest(BaseModel):
    AgentId: Optional[str] = None
    Path: str = "."
    Recursive: bool = False


def _clean_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _resolve_responses_conversation_id(conversation_value: Any) -> str | None:
    if conversation_value is None:
        return None
    if isinstance(conversation_value, str):
        return _clean_optional_string(conversation_value)
    if isinstance(conversation_value, Mapping):
        return _clean_optional_string(conversation_value.get("id"))
    raise HTTPException(
        status_code=400,
        detail="Responses field 'conversation' must be a string or an object with an 'id'.",
    )


def _resolve_responses_session_and_user(request: ResponsesRequest) -> tuple[str | None, str]:
    conversation_id = _resolve_responses_conversation_id(request.conversation)
    legacy_session_id = _clean_optional_string(request.session_id)

    if conversation_id and legacy_session_id and conversation_id != legacy_session_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "Responses field 'conversation' conflicts with ksadk legacy field "
                "'session_id'. Use 'conversation' for OpenAI-compatible calls."
            ),
        )
    if conversation_id and request.previous_response_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "Responses fields 'conversation' and 'previous_response_id' cannot be "
                "used together."
            ),
        )

    resolved_session_id = conversation_id or legacy_session_id
    resolved_user_id = (
        _clean_optional_string(request.safety_identifier)
        or _clean_optional_string(request.user)
        or "user"
    )
    return resolved_session_id, resolved_user_id


def _runtime_agent_id(active_runner: BaseRunner) -> str:
    runtime_id = _clean_optional_string(os.getenv("AGENT_RUNTIME_ID"))
    if runtime_id:
        return runtime_id
    return str(getattr(active_runner.detection_result, "name", "") or "agent")


def _metadata_invocation_id(metadata: Mapping[str, Any] | None) -> str | None:
    if not isinstance(metadata, Mapping):
        return None
    agentengine_metadata = metadata.get("agentengine")
    if not isinstance(agentengine_metadata, Mapping):
        return None
    return _clean_optional_string(agentengine_metadata.get("invocation_id"))


def _split_custom_metadata(
    metadata: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    public_metadata = dict(metadata or {})
    runtime_metadata: dict[str, Any] = {}
    agentengine_metadata = public_metadata.get("agentengine")
    public_metadata.pop("agentengine", None)
    if isinstance(agentengine_metadata, Mapping):
        runtime_metadata["agentengine"] = dict(agentengine_metadata)
        # The approval profile is a small, validated-by-the-agent runtime control.
        # It must not be mixed into caller-visible public metadata.
        tool_approval_mode = agentengine_metadata.get("tool_approval_mode")
        if isinstance(tool_approval_mode, str):
            runtime_metadata["tool_approval_mode"] = tool_approval_mode
    return public_metadata, runtime_metadata


def _run_agent_response_metadata(
    custom_metadata: Mapping[str, Any] | None,
    result: Mapping[str, Any] | None,
) -> dict[str, Any]:
    response_metadata = dict(custom_metadata or {})
    result_metadata = result.get("metadata") if isinstance(result, Mapping) else None
    if isinstance(result_metadata, Mapping):
        agentengine_metadata = result_metadata.get("agentengine")
        if isinstance(agentengine_metadata, Mapping):
            response_metadata["agentengine"] = dict(agentengine_metadata)
    return response_metadata


def _event_text(event: SessionEvent) -> str:
    parts = (event.content or {}).get("parts")
    if isinstance(parts, list):
        text_parts: list[str] = []
        for part in parts:
            if isinstance(part, Mapping):
                text_parts.append(str(part.get("text") or ""))
            else:
                text_parts.append(str(part or ""))
        text = "".join(text_parts).strip()
        if text:
            return text
    return str((event.content or {}).get("text") or "").strip()


def _truncate_session_text(text: str, limit: int = 512) -> str:
    normalized = " ".join(str(text or "").strip().split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: max(limit - 1, 0)].rstrip()}…"


def _session_user_prompt_from_event(event: SessionEvent) -> str:
    metadata = event.metadata or {}
    content = event.content or {}
    return str(
        metadata.get("agent_input")
        or metadata.get("user_input")
        or content.get("agent_input")
        or _event_text(event)
        or ""
    ).strip()


def _run_status_payload_status(event: SessionEvent) -> str:
    legacy_status = str(
        (event.metadata or {}).get("status")
        or (event.metadata or {}).get("run_status")
        or (event.content or {}).get("status")
        or ""
    ).strip()
    if legacy_status:
        return legacy_status
    runtime_payload = (event.content or {}).get("payload")
    runtime_status = (
        str(runtime_payload.get("status") or "").strip()
        if isinstance(runtime_payload, Mapping)
        else ""
    )
    return runtime_status or _RUNTIME_RUN_STATUS_BY_EVENT_TYPE.get(event.event_type, "")


def _is_run_lifecycle_event(event: SessionEvent) -> bool:
    return event.event_type == "run_status" or event.event_type in _RUNTIME_RUN_STATUS_BY_EVENT_TYPE


def _event_run_id(event: SessionEvent) -> str:
    return str(
        (event.metadata or {}).get("run_id")
        or (event.metadata or {}).get("invocation_id")
        or event.invocation_id
        or ""
    ).strip()


def _session_topic_from_events(events: list[SessionEvent]) -> str:
    for event in reversed(events):
        metadata = event.metadata or {}
        tool_output = metadata.get("tool_output")
        if isinstance(tool_output, Mapping):
            topic = str(tool_output.get("topic") or tool_output.get("research_title") or "").strip()
            if topic:
                return topic
        topic = str(metadata.get("research_title") or metadata.get("task_title") or "").strip()
        if topic:
            return topic
    return ""


def _latest_session_run_status(events: list[SessionEvent]) -> tuple[str, str]:
    latest_by_invocation: dict[str, tuple[str, SessionEvent]] = {}
    for event in reversed(events):
        if not _is_run_lifecycle_event(event):
            continue
        status = _run_status_payload_status(event)
        invocation_id = _event_run_id(event)
        if status or invocation_id:
            latest_by_invocation.setdefault(invocation_id, (status, event))
    for invocation_id, (status, _) in latest_by_invocation.items():
        if status in _RUN_ACTIVE_STATUSES:
            return invocation_id, status
    for invocation_id, (status, _) in latest_by_invocation.items():
        if status not in _RUN_TERMINAL_STATUSES:
            return invocation_id, status
    if latest_by_invocation:
        invocation_id, (status, _) = next(iter(latest_by_invocation.items()))
        return invocation_id, status
    for event in reversed(events):
        invocation_id = _event_run_id(event)
        if invocation_id:
            return invocation_id, ""
    return "", ""


def _latest_session_run_metadata(
    events: list[SessionEvent],
) -> tuple[str, str, str, str]:
    """返回 (invocation_id, status, run_mode, run_trigger)。

    与 _latest_session_run_status 同语义，但额外从最新 run_status 事件的 metadata
    读取 run_mode/run_trigger。旧事件缺字段降级 unknown。原 _latest_session_run_status
    不动，保护现有 ActiveInvocationId/ActiveRunStatus 契约。
    """
    invocation_id, status = _latest_session_run_status(events)
    run_mode = RUN_MODE_UNKNOWN
    run_trigger = RUN_TRIGGER_UNKNOWN
    if invocation_id:
        for event in reversed(events):
            if not _is_run_lifecycle_event(event) or _event_run_id(event) != invocation_id:
                continue
            metadata = event.metadata or {}
            run_mode = str(metadata.get("run_mode") or RUN_MODE_UNKNOWN)
            run_trigger = str(metadata.get("run_trigger") or RUN_TRIGGER_UNKNOWN)
            break
    return invocation_id, status, run_mode, run_trigger


class WorkspaceDeleteActionRequest(BaseModel):
    AgentId: Optional[str] = None
    Path: str


class CancelRunActionRequest(BaseModel):
    AgentId: Optional[str] = None
    UserId: Optional[str] = None
    SessionId: Optional[str] = None
    InvocationId: str
