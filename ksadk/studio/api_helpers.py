"""Small protocol helpers shared by Studio HTTP route composition."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from ksadk.studio.errors import StudioError
from ksadk.studio.shared_web import StudioSharedWebBridge


def is_local_origin(origin: str, *, local_hosts: set[str]) -> bool:
    parsed = urlparse(origin)
    return parsed.scheme in {"http", "https"} and (parsed.hostname or "").lower() in local_hosts


def responses_input(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str) and value.strip():
        return [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": value.strip()}],
            }
        ]
    if isinstance(value, list) and value:
        return value
    raise StudioError(
        "RUN_INPUT_REQUIRED",
        "Responses input 必须是非空字符串或消息数组",
        status_code=422,
        field="input",
    )


def responses_session_id(
    payload: dict[str, Any],
    *,
    bridge: StudioSharedWebBridge,
) -> str | None:
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        candidate = metadata.get("session_id") or metadata.get("sessionId")
        if candidate:
            return str(candidate)
    conversation = payload.get("conversation")
    if isinstance(conversation, str) and conversation.strip():
        return conversation.strip()
    if isinstance(conversation, dict) and conversation.get("id"):
        return str(conversation["id"])
    previous_response_id = payload.get("previous_response_id")
    if previous_response_id:
        return bridge.response_session_id(str(previous_response_id))
    return None


def error_response(exc: StudioError, request: Request) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.as_dict(request_id=request_id),
        headers={"X-Request-Id": request_id or ""},
    )


def require_idempotency_key(value: str | None) -> str:
    if not value or not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", value):
        raise StudioError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "异步创建接口必须提供合法 Idempotency-Key",
            status_code=400,
        )
    return value


def parse_revision(value: str | None) -> int:
    if not value:
        raise StudioError(
            "AGENT_REVISION_REQUIRED",
            "更新 Agent 必须提供 If-Match revision",
            status_code=428,
        )
    try:
        return int(value.strip().strip('"'))
    except ValueError as exc:
        raise StudioError(
            "AGENT_REVISION_INVALID",
            "If-Match 必须是整数 revision",
            status_code=400,
        ) from exc


def optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise StudioError(
            "REQUEST_VALIDATION_FAILED",
            "分页游标必须是整数",
            status_code=422,
        ) from exc


def sse(events: list[Any]) -> StreamingResponse:
    def render():
        for event in events:
            data = json.dumps(event.data, ensure_ascii=False, separators=(",", ":"))
            yield f"id: {event.id}\nevent: {event.type}\ndata: {data}\n\n"

    return StreamingResponse(
        render(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


__all__ = [
    "error_response",
    "is_local_origin",
    "optional_int",
    "parse_revision",
    "require_idempotency_key",
    "responses_input",
    "responses_session_id",
    "sse",
]
