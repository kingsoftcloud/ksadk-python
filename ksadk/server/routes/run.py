"""RunAgent, legacy SSE, and builder-cancel execution routes."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from fastapi.responses import StreamingResponse

from ksadk.conversations.normalize import (
    normalize_kop_messages,
    normalize_parts_content,
    normalize_responses_input,
)
from ksadk.conversations.run_kinds import (
    RUN_MODE_BACKGROUND,
    RUN_MODE_FOREGROUND,
    trigger_from_resume_input,
)
from ksadk.conversations.runtime_metadata import prime_session_metadata_for_user_turn
from ksadk.conversations.runtime_payloads import (
    build_chat_completions_payload,
    build_responses_payload,
    extract_responses_resume_input,
)
from ksadk.conversations.runtime_persistence import (
    append_run_status_event,
    ensure_conversation_session,
)
from ksadk.conversations.runtime_streaming import (
    stream_runtime_conversation_turn,
    stream_runtime_responses_conversation_turn,
)

if TYPE_CHECKING:
    pass
from ksadk.ids import new_run_id
from ksadk.runtime.conversation_execution import (
    invoke_runtime_conversation_once,
    iter_runtime_conversation_semantic_events,
)
from ksadk.server.api_models import AgentRunRequest
from ksadk.server.factory import get_runtime_execution

from . import dependencies as deps
from .checkpoint_resolution import _resolve_checkpoint_resume_input_from_session
from .common import (
    _action_response,
)
from .kernel_ingress import (
    _kernel_error_response,
    kernel_conversation_turn,
    kernel_stream_response,
)
from ksadk.kernel.ingress import kernel_route_active
from .models import (
    RunAgentActionRequest,
    _clean_optional_string,
    _run_agent_response_metadata,
    _runtime_agent_id,
    _split_custom_metadata,
)
from .routers import control_router, run_router
from .streaming import (
    _clear_detached_resume_key,
    _detached_resume_key_from_input,
    _DetachedSSEStream,
    _reject_if_detached_resume_active,
)

logger = logging.getLogger(__name__)


@run_router.post("/agentengine/api/v1/RunAgent")
async def run_agent_action(request: RunAgentActionRequest):
    executor, launch_context = get_runtime_execution()
    if kernel_route_active():
        return await _kernel_run_agent_action(request, launch_context)
    api_format = (request.ApiFormat or "responses").strip().lower()
    run_user_id = _clean_optional_string(request.UserId) or "user"
    account_id = _clean_optional_string(request.AccountId)
    service = deps.resolve_session_service()
    resume_input = (
        extract_responses_resume_input(request.ResponsesInput)
        if request.ResponsesInput is not None
        else None
    )
    resume_input = await _resolve_checkpoint_resume_input_from_session(
        service=service,
        agent_id=request.AgentId,
        session_id=request.SessionId,
        resume_input=resume_input,
    )
    if resume_input is not None:
        messages = []
    elif request.ResponsesInput is not None and api_format == "responses":
        messages = normalize_responses_input(request.ResponsesInput)
    else:
        messages = normalize_kop_messages(request.Messages)
    request_metadata: dict[str, Any] = (
        {"previous_response_id": request.PreviousResponseId} if request.PreviousResponseId else {}
    )
    custom_metadata, metadata_runtime_controls = _split_custom_metadata(request.Metadata)
    request_metadata.update(metadata_runtime_controls)
    if api_format == "responses":
        request_metadata["responses_conversation"] = True

    if request.Background:
        runtime_preparation = (
            None
            if resume_input is not None
            else await executor.prepare_start(launch_context)
        )
        invocation_id = request.InvocationId or new_run_id(request.SessionId)
        # 后台 stream 在 detached task 里才被消费（lazy），此时 session 尚未创建。
        # 先 ensure 出 session，才能立刻写 run_status=in_progress（供 SubscribeRunEvents
        # 拉到起始态），并把 resolved session_id 回填给 detached stream 的终态写入与 SubscribeUrl。
        background_session = await ensure_conversation_session(
            agent_id=request.AgentId,
            user_id=run_user_id,
            session_id=request.SessionId,
            session_service_provider=deps.resolve_session_service,
        )
        resolved_background_session_id = background_session.id
        if resume_input is None:
            await prime_session_metadata_for_user_turn(
                service=service,
                session=background_session,
                messages=messages,
            )
        await append_run_status_event(
            session_id=resolved_background_session_id,
            author=_runtime_agent_id(launch_context),
            status="in_progress",
            invocation_id=invocation_id,
            session_service_provider=deps.resolve_session_service,
            run_mode=RUN_MODE_BACKGROUND,
            run_trigger=trigger_from_resume_input(resume_input),
        )
        resume_key = _detached_resume_key_from_input(resolved_background_session_id, resume_input)
        _reject_if_detached_resume_active(resume_key)
        detached = _DetachedSSEStream(
            stream_runtime_responses_conversation_turn(
                executor=executor,
                launch_context=launch_context,
                agent_id=request.AgentId,
                user_id=run_user_id,
                messages=messages,
                session_id=resolved_background_session_id,
                model=request.Model,
                model_metadata=request.ModelMetadata,
                model_options=request.ModelOptions,
                request_metadata=request_metadata or None,
                custom_metadata=custom_metadata,
                include_agentengine_metadata=True,
                resume_input=resume_input,
                account_id=account_id,
                invocation_id=invocation_id,
                session_service_provider=deps.resolve_session_service,
                run_mode=RUN_MODE_BACKGROUND,
                runtime_preparation=runtime_preparation,
            ),
            invocation_id=invocation_id,
            session_id=resolved_background_session_id,
            run_mode=RUN_MODE_BACKGROUND,
            run_trigger=trigger_from_resume_input(resume_input),
        )
        if invocation_id and resume_key:
            resolved_resume_key = resume_key
            registry = detached._registry
            registry.resume_keys_by_invocation[invocation_id] = resolved_resume_key
            registry.active_resume_invocation_by_key[resolved_resume_key] = invocation_id

            def clear_resume_key(_task: Any) -> None:
                _clear_detached_resume_key(registry, invocation_id, resolved_resume_key)

            detached._task.add_done_callback(clear_resume_key)
        return _action_response(
            "RunAgent",
            {
                "SessionId": resolved_background_session_id,
                "InvocationId": invocation_id,
                "Status": "running",
                "Background": True,
                "SubscribeUrl": (
                    "/agentengine/api/v1/SubscribeRunEvents"
                    f"?SessionId={resolved_background_session_id}"
                    f"&InvocationId={invocation_id}"
                ),
            },
        )

    if request.Stream:
        runtime_preparation = (
            None
            if resume_input is not None
            else await executor.prepare_start(launch_context)
        )
        if api_format == "chat_completions":
            return StreamingResponse(
                stream_runtime_conversation_turn(
                    executor=executor,
                    launch_context=launch_context,
                    agent_id=_runtime_agent_id(launch_context),
                    user_id=run_user_id,
                    messages=messages,
                    session_id=request.SessionId,
                    model=request.Model,
                    model_metadata=request.ModelMetadata,
                    model_options=request.ModelOptions,
                    request_metadata=request_metadata or None,
                    custom_metadata=custom_metadata,
                    account_id=account_id,
                    invocation_id=request.InvocationId,
                    session_service_provider=deps.resolve_session_service,
                    run_mode=RUN_MODE_FOREGROUND,
                    runtime_preparation=runtime_preparation,
                ),
                media_type="text/event-stream",
            )
        resume_key = _detached_resume_key_from_input(request.SessionId, resume_input)
        _reject_if_detached_resume_active(resume_key)
        return deps.detached_streaming_response(
            stream_runtime_responses_conversation_turn(
                executor=executor,
                launch_context=launch_context,
                agent_id=request.AgentId,
                user_id=run_user_id,
                messages=messages,
                session_id=request.SessionId,
                model=request.Model,
                model_metadata=request.ModelMetadata,
                model_options=request.ModelOptions,
                request_metadata=request_metadata or None,
                custom_metadata=custom_metadata,
                include_agentengine_metadata=True,
                resume_input=resume_input,
                account_id=account_id,
                invocation_id=request.InvocationId,
                session_service_provider=deps.resolve_session_service,
                run_mode=RUN_MODE_FOREGROUND,
                runtime_preparation=runtime_preparation,
            ),
            invocation_id=request.InvocationId,
            resume_key=resume_key,
            run_mode=RUN_MODE_FOREGROUND,
            run_trigger=trigger_from_resume_input(resume_input),
        )

    responses_response_id = f"resp_{uuid.uuid4().hex}" if api_format != "chat_completions" else None
    resolved_session_id, result = await invoke_runtime_conversation_once(
        executor=executor,
        launch_context=launch_context,
        agent_id=request.AgentId,
        user_id=run_user_id,
        messages=messages,
        session_id=request.SessionId,
        model=request.Model,
        model_metadata=request.ModelMetadata,
        model_options=request.ModelOptions,
        request_metadata=request_metadata or None,
        custom_metadata=custom_metadata,
        resume_input=resume_input,
        response_id=responses_response_id,
        account_id=account_id,
        invocation_id=request.InvocationId,
        session_service_provider=deps.resolve_session_service,
        run_mode=RUN_MODE_FOREGROUND,
    )
    output_text = result["output_text"]
    if api_format == "chat_completions":
        payload = build_chat_completions_payload(
            output_text=output_text,
            model=request.Model,
            session_id=resolved_session_id,
            metadata=result.get("metadata"),
        )
    else:
        payload = build_responses_payload(
            output_text=output_text,
            model=request.Model,
            session_id=resolved_session_id,
            response_id=responses_response_id,
            metadata=_run_agent_response_metadata(custom_metadata, result),
            usage=result.get("usage") if isinstance(result.get("usage"), Mapping) else None,
        )
    return _action_response("RunAgent", payload)


async def _kernel_run_agent_action(request: RunAgentActionRequest, launch_context):
    """kernel 路径（灰度 opt-in）：RunAgent -> AgentControlCommand -> receipt。

    旧响应 shape 不变；receipt 状态走 RECEIPT_HTTP_STATUS 映射；
    stream 从 SessionEventSubscription 统一 cursor 读取。
    """
    from .kernel_ingress import _kernel_submit

    run_user_id = _clean_optional_string(request.UserId) or "user"
    session = await ensure_conversation_session(
        agent_id=request.AgentId,
        user_id=run_user_id,
        session_id=request.SessionId,
        session_service_provider=deps.resolve_session_service,
    )
    session_id = session.id
    idempotency_key = (
        _clean_optional_string(request.InvocationId)
        or _clean_optional_string(
            (request.Metadata or {}).get("IdempotencyKey")
            if isinstance(request.Metadata, dict)
            else None
        )
        or new_run_id(session_id)
    )
    messages = (
        normalize_responses_input(request.ResponsesInput)
        if request.ResponsesInput is not None
        and (request.ApiFormat or "responses").strip().lower() == "responses"
        else normalize_kop_messages(request.Messages)
    )
    receipt, trusted = await _kernel_submit(
        mapper="map_run_request",
        session_id=session_id,
        idempotency_key=idempotency_key,
        content=messages,
        correlation_ref=request.InvocationId,
        source_kind="system",
    )
    if receipt.status not in ("accepted", "duplicate"):
        return _kernel_error_response(receipt)

    def build_payload(output_text: str):
        if (request.ApiFormat or "responses").strip().lower() == "chat_completions":
            return _action_response(
                "RunAgent",
                build_chat_completions_payload(
                    output_text=output_text,
                    model=request.Model,
                    session_id=session_id,
                    metadata=None,
                ),
            )
        return _action_response(
            "RunAgent",
            build_responses_payload(
                output_text=output_text,
                model=request.Model,
                session_id=session_id,
                response_id=f"resp_{uuid.uuid4().hex}",
                metadata=None,
                usage=None,
            ),
        )

    if request.Stream or request.Background:
        return kernel_stream_response(
            receipt=receipt,
            trusted=trusted,
            session_id=session_id,
        )
    return await kernel_conversation_turn(
        receipt=receipt,
        trusted=trusted,
        session_id=session_id,
        build_payload=build_payload,
    )


# ============================================================
# Session Management API (ADK Web Compatible)
# ============================================================


def _adk_sse_event(payload: Mapping[str, Any], *, event: str | None = None) -> str:
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {json.dumps(dict(payload), ensure_ascii=False)}\n\n"


def _adk_usage_metadata(usage: Mapping[str, Any] | None) -> dict[str, int] | None:
    if not isinstance(usage, Mapping):
        return None
    prompt = usage.get("input_tokens", usage.get("prompt_tokens"))
    candidates = usage.get("output_tokens", usage.get("completion_tokens"))
    if not isinstance(prompt, int) or not isinstance(candidates, int):
        return None
    return {
        "promptTokenCount": prompt,
        "candidatesTokenCount": candidates,
        "totalTokenCount": prompt + candidates,
    }


async def _runtime_run_sse(request: AgentRunRequest) -> StreamingResponse:
    executor, launch_context = get_runtime_execution()
    # Validate explicit ownership before StreamingResponse commits HTTP 200.
    # Also resolve an omitted id exactly once so preparation cannot create a
    # second, orphaned session after the response generator starts.
    execution_session = await ensure_conversation_session(
        agent_id=request.appName,
        user_id=request.userId,
        session_id=request.sessionId,
        session_service_provider=deps.resolve_session_service,
    )
    execution_session_id = execution_session.id
    normalized = normalize_parts_content(request.newMessage.parts)
    message = {
        "role": "user",
        "content": str(normalized.get("content") or ""),
        "display_content": str(normalized.get("display_content") or ""),
        "parts": list(normalized.get("parts") or []),
        "attachments": list(normalized.get("attachments") or []),
        "attachment_results": list(normalized.get("attachment_results") or []),
    }
    invocation_id = request.invocationId or new_run_id(execution_session_id)
    author = _runtime_agent_id(launch_context)

    async def event_generator():
        if not request.streaming:
            try:
                session_id, result = await invoke_runtime_conversation_once(
                    executor=executor,
                    launch_context=launch_context,
                    agent_id=request.appName,
                    user_id=request.userId,
                    messages=[message],
                    session_id=execution_session_id,
                    model=request.model,
                    state_delta=request.stateDelta or {},
                    invocation_id=invocation_id,
                    session_service_provider=deps.resolve_session_service,
                    run_mode=RUN_MODE_FOREGROUND,
                )
                output_text = str(result.get("output_text") or "")
                payload: dict[str, Any] = {
                    "id": str(uuid.uuid4()),
                    "author": author,
                    "sessionId": session_id,
                    "invocationId": invocation_id,
                    "content": {"role": "model", "parts": [{"text": output_text}]},
                    "actions": {"finishReason": "STOP"},
                    "modelVersion": request.model or "models/unknown",
                    "timestamp": int(time.time() * 1000),
                }
                usage_metadata = _adk_usage_metadata(result.get("usage"))
                if usage_metadata is not None:
                    payload["usageMetadata"] = usage_metadata
                yield _adk_sse_event(payload)
            except Exception:
                logger.exception("Error in RuntimeAdapter invoke")
                yield _adk_sse_event(
                    {
                        "id": str(uuid.uuid4()),
                        "sessionId": execution_session_id,
                        "invocationId": invocation_id,
                        "error": "agent_run_failed",
                        "errorMessage": "The agent run failed. See server logs for details.",
                        "timestamp": int(time.time() * 1000),
                    }
                )
            return

        session_id = execution_session_id
        visible_text = ""
        try:
            async for event in iter_runtime_conversation_semantic_events(
                executor=executor,
                launch_context=launch_context,
                agent_id=request.appName,
                user_id=request.userId,
                messages=[message],
                session_id=execution_session_id,
                model=request.model,
                state_delta=request.stateDelta or {},
                invocation_id=invocation_id,
                session_service_provider=deps.resolve_session_service,
                run_mode=RUN_MODE_FOREGROUND,
            ):
                event_type = event.get("type")
                session_id = str(event.get("session_id") or session_id)
                if event_type == "thinking":
                    yield _adk_sse_event(
                        {"delta": str(event.get("delta") or "")},
                        event="response.reasoning.delta",
                    )
                elif event_type == "compaction":
                    phase = str(event.get("phase") or "done")
                    yield _adk_sse_event(
                        {
                            key: value
                            for key, value in event.items()
                            if key not in {"type", "phase"} and value is not None
                        },
                        event=f"response.compaction.{phase}",
                    )
                elif event_type == "text":
                    delta = str(event.get("delta") or "")
                    replace = bool(event.get("replace"))
                    visible_text = delta if replace else visible_text + delta
                    text_payload: dict[str, Any] = {
                        "id": str(uuid.uuid4()),
                        "author": author,
                        "sessionId": session_id,
                        "invocationId": invocation_id,
                        "content": {"role": "model", "parts": [{"text": delta}]},
                        "timestamp": int(time.time() * 1000),
                    }
                    if replace:
                        # A replacement snapshot is authoritative final output,
                        # not another partial token. Keep the replacement hint
                        # so clients can discard previously rendered deltas.
                        text_payload.update(
                            {
                                "replace": True,
                                "actions": {"finishReason": "STOP"},
                                "modelVersion": request.model or "models/unknown",
                            }
                        )
                    else:
                        text_payload.update({"partial": True, "replace": False})
                    yield _adk_sse_event(
                        text_payload
                    )
                elif event_type == "tool_call":
                    yield _adk_sse_event(
                        {"name": event.get("name"), "args": event.get("args") or {}},
                        event="response.tool_call",
                    )
                elif event_type == "tool_result":
                    yield _adk_sse_event(
                        {"name": event.get("name"), "output": event.get("output")},
                        event="response.tool_result",
                    )
                elif event_type == "interrupt":
                    yield _adk_sse_event(
                        {"interrupt_info": event.get("interrupt_info")},
                        event="response.approval_request",
                    )
                elif event_type == "completed":
                    final_text = str(event.get("output_text") or visible_text)
                    if final_text != visible_text:
                        yield _adk_sse_event(
                            {
                                "id": str(uuid.uuid4()),
                                "author": author,
                                "sessionId": session_id,
                                "invocationId": invocation_id,
                                "content": {
                                    "role": "model",
                                    "parts": [{"text": final_text}],
                                },
                                "actions": {"finishReason": "STOP"},
                                "modelVersion": request.model or "models/unknown",
                                "timestamp": int(time.time() * 1000),
                            }
                        )
                elif event_type == "error":
                    yield _adk_sse_event(
                        {
                            "id": str(uuid.uuid4()),
                            "sessionId": session_id,
                            "invocationId": invocation_id,
                            "error": "agent_run_failed",
                            "errorMessage": str(event.get("message") or "Agent run failed"),
                            "timestamp": int(time.time() * 1000),
                        }
                    )
        except Exception:
            logger.exception("Error in RuntimeAdapter stream")
            yield _adk_sse_event(
                {
                    "id": str(uuid.uuid4()),
                    "sessionId": session_id,
                    "invocationId": invocation_id,
                    "error": "agent_run_failed",
                    "errorMessage": "The agent run failed. See server logs for details.",
                    "timestamp": int(time.time() * 1000),
                }
            )

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@run_router.post("/run_sse")
async def run_sse(request: AgentRunRequest):
    """Unified Streaming Endpoint compatible with ADK Web

    Respects the `streaming` parameter:
    - streaming=False: Accumulate full response, send as single event
    - streaming=True: Stream tokens as they arrive (real-time)
    """
    return await _runtime_run_sse(request)


# ============================================================
# Trace / Debug API (ADK Web Compatible)
# ============================================================


@control_router.post("/builder/app/{app_name}/cancel")
async def cancel_agent_changes(app_name: str):
    """Cancel agent builder changes - stub for ADK-Web"""
    return True
