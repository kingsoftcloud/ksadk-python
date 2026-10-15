"""Signed cloud chat proxy routes and canonical event projection."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import StreamingResponse

from ksadk.conversations.projector import project_conversation_item
from ksadk.events.canonical import parse_runtime_event_lenient
from ksadk.studio.api_contracts import CloudChatInteractionSubmitRequest, CloudChatMessageRequest
from ksadk.studio.workspace_registry import WorkspaceRuntimeManager

_RUNTIME_EVENT_WIRE_ALIASES = {
    "schemaVersion": "schema_version",
    "eventId": "event_id",
    "runId": "run_id",
    "runSeq": "run_seq",
    "scopeId": "scope_id",
    "parentScopeId": "parent_scope_id",
    "eventType": "event_type",
    "itemId": "item_id",
    "itemKind": "item_kind",
    "interactionId": "interaction_id",
    "interactionKind": "interaction_kind",
    "continuationId": "continuation_id",
    "resumeAttemptId": "resume_attempt_id",
    "outputRefs": "output_refs",
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "totalTokens": "total_tokens",
    "cachedTokens": "cached_tokens",
    "reasoningTokens": "reasoning_tokens",
}

_SOURCE_WIRE_ALIASES = {
    "nativeEventId": "native_event_id",
    "nativeCursor": "native_cursor",
    "nativeRunId": "native_run_id",
    "nativeItemId": "native_item_id",
}

_CONTENT_WIRE_ALIASES = {
    "contentType": "content_type",
    "partId": "part_id",
    "callId": "call_id",
    "artifactId": "artifact_id",
    "mimeType": "mime_type",
    "isError": "is_error",
}


def _normalize_cloud_runtime_event_wire(value: dict[str, Any]) -> dict[str, Any]:
    """Normalize only historic REST casing at the RuntimeEvent boundary."""

    normalized = {_RUNTIME_EVENT_WIRE_ALIASES.get(key, key): item for key, item in value.items()}
    source = normalized.get("source")
    if isinstance(source, dict):
        normalized["source"] = {
            _SOURCE_WIRE_ALIASES.get(key, key): item for key, item in source.items()
        }

    def normalize_content(content: Any) -> Any:
        if not isinstance(content, dict):
            return content
        return {_CONTENT_WIRE_ALIASES.get(key, key): item for key, item in content.items()}

    normalized["update"] = normalize_content(normalized.get("update"))
    for key in ("initial", "snapshot"):
        snapshot = normalized.get(key)
        if not isinstance(snapshot, dict):
            continue
        parts = snapshot.get("parts")
        if isinstance(parts, list):
            normalized[key] = {
                **snapshot,
                "parts": [normalize_content(part) for part in parts],
            }
    return normalized


def _cloud_event_conversation_item(
    event: dict[str, Any], *, session_id: str
) -> dict[str, Any] | None:
    """Project a complete cloud RuntimeEvent without inventing a browser item.

    Cloud SessionEvent history has a compatibility envelope around the runtime
    payload.  Keep the source event untouched, but add the same typed
    ConversationItem that local Studio streams use whenever the nested event
    validates against RuntimeEvent/v2.  A malformed or older envelope is still
    observable to diagnostics, never guessed into an actionable chat card.
    """

    payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
    content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
    candidates = (
        content.get("runtime_event"),
        content.get("runtimeEvent"),
        payload.get("runtime_event"),
        payload.get("runtimeEvent"),
        event.get("runtime_event"),
        event.get("runtimeEvent"),
    )
    raw_event = next((candidate for candidate in candidates if isinstance(candidate, dict)), None)
    if raw_event is None:
        return None
    try:
        runtime_event = parse_runtime_event_lenient(_normalize_cloud_runtime_event_wire(raw_event))
        item = project_conversation_item(
            runtime_event,  # type: ignore[arg-type]
            session_id=session_id,
            run_id=runtime_event.run_id,
        )
    except (TypeError, ValueError):
        return None
    return item.model_dump(by_alias=True, exclude_none=True, mode="json")


def register_cloud_chat_routes(app: FastAPI, studio: WorkspaceRuntimeManager) -> None:
    @app.get("/api/v1/deployments/{deployment_id}/cloud-chat/sessions")
    async def list_cloud_chat_sessions(
        deployment_id: str,
        page: int = Query(default=1, ge=1),
        size: int = Query(default=50, ge=1, le=100),
    ):
        """List Server-owned sessions for this local deployment receipt only."""

        return await studio.cloud.list_cloud_chat_sessions(deployment_id, page=page, size=size)

    @app.get("/api/v1/deployments/{deployment_id}/cloud-chat/models")
    async def list_cloud_chat_models(deployment_id: str):
        """List models through Studio's signed Server client."""

        return await studio.cloud.list_cloud_chat_models(deployment_id)

    @app.post(
        "/api/v1/deployments/{deployment_id}/cloud-chat/sessions",
        status_code=201,
    )
    async def create_cloud_chat_session(deployment_id: str):
        """Create a cloud session via loopback-held AK/SK; no secret reaches JS."""

        return await studio.cloud.create_cloud_chat_session(deployment_id)

    @app.get("/api/v1/deployments/{deployment_id}/cloud-chat/sessions/{session_id}/messages")
    async def list_cloud_chat_messages(
        deployment_id: str,
        session_id: str,
        after_seq_id: int | None = Query(default=None, alias="afterSeqId", ge=0),
        limit: int = Query(default=100, ge=1, le=200),
    ):
        return await studio.cloud.list_cloud_chat_messages(
            deployment_id,
            session_id=session_id,
            after_seq_id=after_seq_id,
            limit=limit,
        )

    @app.get("/api/v1/deployments/{deployment_id}/cloud-chat/sessions/{session_id}/events")
    async def list_cloud_chat_events(
        deployment_id: str,
        session_id: str,
        after_seq_id: int | None = Query(default=None, alias="afterSeqId", ge=0),
        offset: int | None = Query(default=None, ge=0),
        limit: int = Query(default=200, ge=1, le=1000),
    ):
        return await studio.cloud.list_cloud_chat_events(
            deployment_id,
            session_id=session_id,
            after_seq_id=after_seq_id,
            offset=offset,
            limit=limit,
        )

    @app.get("/api/v1/deployments/{deployment_id}/cloud-chat/sessions/{session_id}/events/stream")
    async def stream_cloud_chat_events(
        request: Request,
        deployment_id: str,
        session_id: str,
        after_seq_id: int = Query(default=0, alias="afterSeqId", ge=0),
    ):
        """Stream canonical cloud events to the loopback browser.

        The public cloud control plane currently exposes cursor reads for this
        surface.  Keep that cursor in the Studio backend and present one SSE
        response to the browser, so assistant deltas arrive before the durable
        terminal message projection without exposing cloud credentials to JS.
        """

        async def event_stream() -> AsyncIterator[str]:
            cursor = after_seq_id
            idle_polls = 0
            while not await request.is_disconnected():
                payload = await studio.cloud.list_cloud_chat_events(
                    deployment_id,
                    session_id=session_id,
                    after_seq_id=cursor,
                    limit=200,
                )
                events = payload.get("events") or []
                if not isinstance(events, list):
                    events = []
                terminal = False
                emitted = False
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    event_payload = (
                        event.get("payload") if isinstance(event.get("payload"), dict) else event
                    )
                    seq = int(
                        event_payload.get("seq")
                        or event_payload.get("seq_id")
                        or event_payload.get("source_session_seq")
                        or event.get("seq")
                        or event.get("seq_id")
                        or 0
                    )
                    if seq and seq <= cursor:
                        continue
                    if seq:
                        cursor = max(cursor, seq)
                    event_type = str(
                        event.get("event_type")
                        or event.get("eventType")
                        or event_payload.get("event_type")
                        or event_payload.get("eventType")
                        or ""
                    ).lower()
                    content = (
                        event_payload.get("content")
                        if isinstance(event_payload.get("content"), dict)
                        else {}
                    )
                    status = str(event_payload.get("status") or content.get("status") or "").lower()
                    event_is_terminal = event_type in {
                        "run.completed",
                        "run.complete",
                        "run.succeeded",
                        "run.failed",
                        "run.cancelled",
                        "run.expired",
                        "run.error",
                    } or (
                        event_type in {"run_status", "run.status"}
                        and status
                        in {
                            "completed",
                            "complete",
                            "succeeded",
                            "success",
                            "failed",
                            "cancelled",
                            "canceled",
                            "expired",
                            "error",
                            "aborted",
                        }
                    )
                    terminal = terminal or event_is_terminal
                    emitted = True
                    projected_event = dict(event)
                    conversation_item = _cloud_event_conversation_item(
                        event,
                        session_id=session_id,
                    )
                    if conversation_item is not None:
                        projected_event["conversationItem"] = conversation_item
                    yield (
                        (f"id: {seq}\n" if seq else "")
                        + "event: session.event\n"
                        + f"data: {json.dumps(projected_event, ensure_ascii=False)}\n\n"
                    )
                if terminal or payload.get("session_deleted"):
                    break
                idle_polls = 0 if emitted else idle_polls + 1
                if idle_polls and idle_polls % 20 == 0:
                    yield ": keepalive\n\n"
                await asyncio.sleep(0.25)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.delete(
        "/api/v1/deployments/{deployment_id}/cloud-chat/sessions/{session_id}",
        status_code=204,
    )
    async def delete_cloud_chat_session(deployment_id: str, session_id: str):
        await studio.cloud.delete_cloud_chat_session(deployment_id, session_id=session_id)
        return Response(status_code=204)

    @app.post(
        "/api/v1/deployments/{deployment_id}/cloud-chat/sessions/{session_id}/messages",
        status_code=202,
    )
    async def send_cloud_chat_message(
        deployment_id: str,
        session_id: str,
        payload: CloudChatMessageRequest,
    ):
        """Admit one cloud message through Server; response is a durable receipt."""

        return await studio.cloud.send_cloud_chat_message(
            deployment_id,
            session_id=session_id,
            content=payload.content,
            model=payload.model,
            model_options=payload.model_options,
            tool_approval_mode=payload.tool_approval_mode,
            collaboration_mode=payload.collaboration_mode,
            goal_objective=payload.goal_objective,
        )

    @app.post(
        "/api/v1/deployments/{deployment_id}/cloud-chat/sessions/{session_id}/messages/stream"
    )
    async def stream_cloud_chat_message(
        request: Request,
        deployment_id: str,
        session_id: str,
        payload: CloudChatMessageRequest,
    ):
        """Proxy one signed foreground RunAgent SSE response to loopback UI."""

        upstream = await studio.cloud.stream_cloud_chat_message(
            deployment_id,
            session_id=session_id,
            content=payload.content,
            model=payload.model,
            model_options=payload.model_options,
            tool_approval_mode=payload.tool_approval_mode,
            collaboration_mode=payload.collaboration_mode,
            goal_objective=payload.goal_objective,
        )

        async def proxy_stream() -> AsyncIterator[bytes]:
            try:
                while not await request.is_disconnected():
                    try:
                        chunk = await anext(upstream)
                    except StopAsyncIteration:
                        break
                    if await request.is_disconnected():
                        break
                    yield chunk
            finally:
                close = getattr(upstream, "aclose", None)
                if close is not None:
                    await close()

        return StreamingResponse(
            proxy_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post(
        "/api/v1/deployments/{deployment_id}/cloud-chat/sessions/{session_id}/interactions",
        status_code=202,
    )
    async def submit_cloud_chat_interaction(
        deployment_id: str,
        session_id: str,
        payload: CloudChatInteractionSubmitRequest,
    ):
        return await studio.cloud.submit_cloud_chat_interaction(
            deployment_id,
            session_id=session_id,
            run_id=payload.run_id,
            interaction_id=payload.interaction_id,
            expected_revision=payload.expected_revision,
            action=payload.action,
            response=payload.response,
            idempotency_key=payload.idempotency_key,
        )
