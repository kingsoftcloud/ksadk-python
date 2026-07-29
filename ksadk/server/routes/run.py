"""RunAgent, legacy SSE, and builder-cancel execution routes."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from fastapi.responses import StreamingResponse

from ksadk.conversations.run_kinds import (
    RUN_MODE_BACKGROUND,
    RUN_MODE_FOREGROUND,
    RUN_TRIGGER_NEW_RUN,
    trigger_from_resume_input,
)

if TYPE_CHECKING:
    from ksadk.conversations.runtime_payloads import PreparedConversationTurn
from ksadk.ids import new_run_id
from ksadk.server.api_models import AgentRunRequest

from . import dependencies as deps
from .checkpoint_resolution import _resolve_checkpoint_resume_input_from_session
from .common import (
    _action_response,
    _ensure_runner_loaded,
    _prepare_runner_for_model,
    _resolve_active_runner,
)
from .models import (
    RunAgentActionRequest,
    _clean_optional_string,
    _run_agent_response_metadata,
    _runtime_agent_id,
    _split_custom_metadata,
)
from .projection import _iter_with_idle_heartbeat
from .routers import control_router, run_router
from .streaming import (
    _clear_detached_resume_key,
    _detached_resume_key_from_input,
    _reject_if_detached_resume_active,
)

logger = logging.getLogger(__name__)


@run_router.post("/agentengine/api/v1/RunAgent")
async def run_agent_action(request: RunAgentActionRequest):
    api_format = (request.ApiFormat or "responses").strip().lower()
    run_user_id = _clean_optional_string(request.UserId) or "user"
    account_id = _clean_optional_string(request.AccountId)
    service = deps.resolve_session_service()
    resume_input = (
        deps.conversation().extract_responses_resume_input(request.ResponsesInput)
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
        messages = deps.conversation().normalize_responses_input(request.ResponsesInput)
    else:
        messages = deps.conversation().normalize_kop_messages(request.Messages)
    request_metadata: dict[str, Any] = (
        {"previous_response_id": request.PreviousResponseId} if request.PreviousResponseId else {}
    )
    custom_metadata, metadata_runtime_controls = _split_custom_metadata(request.Metadata)
    request_metadata.update(metadata_runtime_controls)
    if api_format == "responses":
        request_metadata["responses_conversation"] = True

    if request.Background:
        invocation_id = request.InvocationId or new_run_id(request.SessionId)
        # 后台 stream 在 detached task 里才被消费（lazy），此时 session 尚未创建。
        # 先 ensure 出 session，才能立刻写 run_status=in_progress（供 SubscribeRunEvents
        # 拉到起始态），并把 resolved session_id 回填给 detached stream 的终态写入与 SubscribeUrl。
        background_session = await deps.conversation().ensure_conversation_session(
            agent_id=request.AgentId,
            user_id=run_user_id,
            session_id=request.SessionId,
            session_service_provider=deps.resolve_session_service,
        )
        resolved_background_session_id = background_session.id
        if resume_input is None:
            await deps.conversation().prime_session_metadata_for_user_turn(
                service=service,
                session=background_session,
                messages=messages,
            )
        await deps.conversation().append_run_status_event(
            session_id=resolved_background_session_id,
            author=_resolve_active_runner().detection_result.name,
            status="in_progress",
            invocation_id=invocation_id,
            session_service_provider=deps.resolve_session_service,
            run_mode=RUN_MODE_BACKGROUND,
            run_trigger=trigger_from_resume_input(resume_input),
        )
        resume_key = _detached_resume_key_from_input(resolved_background_session_id, resume_input)
        _reject_if_detached_resume_active(resume_key)
        detached = deps.detached_stream_class()(
            deps.conversation().stream_responses_conversation_turn(
                runner=_resolve_active_runner(),
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
                prepare_runner=_prepare_runner_for_model,
                session_service_provider=deps.resolve_session_service,
                run_mode=RUN_MODE_BACKGROUND,
            ),
            invocation_id=invocation_id,
            session_id=resolved_background_session_id,
            run_mode=RUN_MODE_BACKGROUND,
            run_trigger=trigger_from_resume_input(resume_input),
        )
        if invocation_id and resume_key:
            registry = detached._registry
            registry.resume_keys_by_invocation[invocation_id] = resume_key
            registry.active_resume_invocation_by_key[resume_key] = invocation_id
            detached._task.add_done_callback(
                lambda _t, inv=invocation_id, rk=resume_key: _clear_detached_resume_key(
                    registry, inv, rk
                )
            )
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
        if api_format == "chat_completions":
            active_runner = _resolve_active_runner()
            return StreamingResponse(
                deps.conversation().stream_conversation_turn(
                    runner=active_runner,
                    agent_id=_runtime_agent_id(active_runner),
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
                    prepare_runner=_prepare_runner_for_model,
                    session_service_provider=deps.resolve_session_service,
                    run_mode=RUN_MODE_FOREGROUND,
                ),
                media_type="text/event-stream",
            )
        resume_key = _detached_resume_key_from_input(request.SessionId, resume_input)
        _reject_if_detached_resume_active(resume_key)
        return deps.detached_streaming_response(
            deps.conversation().stream_responses_conversation_turn(
                runner=_resolve_active_runner(),
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
                prepare_runner=_prepare_runner_for_model,
                session_service_provider=deps.resolve_session_service,
                run_mode=RUN_MODE_FOREGROUND,
            ),
            invocation_id=request.InvocationId,
            resume_key=resume_key,
            run_mode=RUN_MODE_FOREGROUND,
            run_trigger=trigger_from_resume_input(resume_input),
        )

    responses_response_id = f"resp_{uuid.uuid4().hex}" if api_format != "chat_completions" else None
    resolved_session_id, result = await deps.conversation().invoke_conversation_once(
        runner=_resolve_active_runner(),
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
        prepare_runner=_prepare_runner_for_model,
        session_service_provider=deps.resolve_session_service,
        run_mode=RUN_MODE_FOREGROUND,
    )
    output_text = result["output_text"]
    if api_format == "chat_completions":
        payload = deps.conversation().build_chat_completions_payload(
            output_text=output_text,
            model=request.Model,
            session_id=resolved_session_id,
            metadata=result.get("metadata"),
        )
    else:
        payload = deps.conversation().build_responses_payload(
            output_text=output_text,
            model=request.Model,
            session_id=resolved_session_id,
            response_id=responses_response_id,
            metadata=_run_agent_response_metadata(custom_metadata, result),
            usage=result.get("usage") if isinstance(result.get("usage"), Mapping) else None,
        )
    return _action_response("RunAgent", payload)


# ============================================================
# Session Management API (ADK Web Compatible)
# ============================================================


@run_router.post("/run_sse")
async def run_sse(request: AgentRunRequest):
    """Unified Streaming Endpoint compatible with ADK Web

    Respects the `streaming` parameter:
    - streaming=False: Accumulate full response, send as single event
    - streaming=True: Stream tokens as they arrive (real-time)
    """
    active_runner = _ensure_runner_loaded()
    _prepare_runner_for_model(active_runner, request.model)
    use_streaming = request.streaming
    normalized_message = deps.conversation().normalize_parts_content(
        request.newMessage.parts if request.newMessage else []
    )
    user_message = {
        "role": "user",
        "content": str(normalized_message.get("content") or ""),
        "display_content": str(normalized_message.get("display_content") or ""),
        "parts": list(normalized_message.get("parts") or []),
        "attachments": list(normalized_message.get("attachments") or []),
        "attachment_results": list(normalized_message.get("attachment_results") or []),
    }

    model_version = "models/gemini-pro" if "gemini" in request.appName.lower() else "models/unknown"
    prepared_non_stream: PreparedConversationTurn | None = None
    if request.sessionId:
        await deps.conversation().ensure_conversation_session(
            agent_id=request.appName,
            user_id=request.userId,
            session_id=request.sessionId,
            session_service_provider=deps.resolve_session_service,
        )
    if not use_streaming:
        prepared_non_stream = await deps.conversation().build_run_input(
            agent_id=request.appName,
            user_id=request.userId,
            session_id=request.sessionId,
            messages=[user_message],
            state_delta=request.stateDelta or {},
            invocation_id=request.invocationId,
            session_service_provider=deps.resolve_session_service,
        )
        await deps.conversation().append_run_status_event(
            session_id=prepared_non_stream.session_id,
            author=active_runner.detection_result.name,
            status="in_progress",
            invocation_id=prepared_non_stream.invocation_id,
            session_service_provider=deps.resolve_session_service,
            run_mode=RUN_MODE_FOREGROUND,
            run_trigger=RUN_TRIGGER_NEW_RUN,
        )

    async def event_generator():
        if not use_streaming:
            try:
                assert prepared_non_stream is not None
                session_id = prepared_non_stream.session_id
                user_input = prepared_non_stream.user_input
                attachments = prepared_non_stream.attachments
                attachment_results = prepared_non_stream.attachment_results
                current_attachments = prepared_non_stream.current_attachments
                current_attachment_results = prepared_non_stream.current_attachment_results
                input_content = prepared_non_stream.input_content
                input_messages = prepared_non_stream.input_messages
                user_parts = prepared_non_stream.user_parts
                history = prepared_non_stream.history
                invocation_id = prepared_non_stream.invocation_id
                common_metadata = {
                    "modelVersion": model_version,
                    "usageMetadata": {
                        "promptTokenCount": len(user_input),
                        "candidatesTokenCount": 0,
                        "totalTokenCount": len(user_input),
                    },
                }
                input_data = {
                    "session_id": session_id,
                    "input": user_input,
                    "history": history,
                    "input_content": list(input_content),
                    "input_messages": list(input_messages),
                    "input_parts": list(user_parts),
                    "attachments": attachments,
                    "attachment_results": attachment_results,
                    "current_attachments": current_attachments,
                    "current_attachment_results": current_attachment_results,
                    "has_current_files": prepared_non_stream.has_current_files,
                    "model": request.model,
                }
                result = await active_runner.invoke(input_data)
                final_text = result.get("output", "")
                response_event = {
                    "id": str(uuid.uuid4()),
                    "author": active_runner.detection_result.name,
                    "sessionId": session_id,
                    "invocationId": invocation_id,
                    "content": {"role": "model", "parts": [{"text": final_text}]},
                    "actions": {"finishReason": "STOP"},
                    "modelVersion": common_metadata["modelVersion"],
                    "usageMetadata": {
                        "promptTokenCount": len(user_input),
                        "candidatesTokenCount": len(final_text),
                        "totalTokenCount": len(user_input) + len(final_text),
                    },
                    "timestamp": int(time.time() * 1000),
                }
                yield f"data: {json.dumps(response_event, ensure_ascii=False)}\n\n"
                if final_text:
                    await deps.conversation().append_conversation_event(
                        session_id=session_id,
                        author=active_runner.detection_result.name,
                        role="model",
                        text=final_text,
                        invocation_id=invocation_id,
                        event_type="assistant_message",
                        session_service_provider=deps.resolve_session_service,
                    )
                await deps.conversation().append_run_status_event(
                    session_id=session_id,
                    author=active_runner.detection_result.name,
                    status="completed",
                    invocation_id=invocation_id,
                    session_service_provider=deps.resolve_session_service,
                    run_mode=RUN_MODE_FOREGROUND,
                    run_trigger=RUN_TRIGGER_NEW_RUN,
                )

            except Exception as e:
                logger.error(f"Error in invoke: {e}")
                await deps.conversation().append_run_status_event(
                    session_id=session_id,
                    author=active_runner.detection_result.name,
                    status="failed",
                    invocation_id=invocation_id,
                    detail=str(e),
                    session_service_provider=deps.resolve_session_service,
                    run_mode=RUN_MODE_FOREGROUND,
                    run_trigger=RUN_TRIGGER_NEW_RUN,
                )
                error_event = {
                    "id": str(uuid.uuid4()),
                    "sessionId": session_id,
                    "invocationId": invocation_id,
                    "error": str(e),
                    "errorMessage": str(e),
                    "timestamp": int(time.time() * 1000),
                }
                yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"
        else:
            try:
                compaction_preview = await deps.conversation().preview_auto_compaction(
                    agent_id=request.appName,
                    user_id=request.userId,
                    session_id=request.sessionId,
                    messages=[user_message],
                    session_service_provider=deps.resolve_session_service,
                )
                if compaction_preview.should_compact:
                    yield deps.conversation().build_compaction_sse_event(
                        phase="start",
                        trigger="auto",
                        total_chars=compaction_preview.total_chars,
                        group_count=compaction_preview.group_count,
                    )

                prepared = await deps.conversation().build_run_input(
                    agent_id=request.appName,
                    user_id=request.userId,
                    session_id=request.sessionId,
                    messages=[user_message],
                    state_delta=request.stateDelta or {},
                    invocation_id=request.invocationId,
                    session_service_provider=deps.resolve_session_service,
                )
                if prepared.compaction_triggered:
                    yield deps.conversation().build_compaction_sse_event(
                        phase="done",
                        trigger=str(prepared.compaction_trigger or "auto"),
                        compacted_until_seq_id=prepared.compacted_until_seq_id,
                        total_chars=(
                            compaction_preview.total_chars
                            if compaction_preview.should_compact
                            else None
                        ),
                        group_count=(
                            compaction_preview.group_count
                            if compaction_preview.should_compact
                            else None
                        ),
                    )

                session_id = prepared.session_id
                user_input = prepared.user_input
                attachments = prepared.attachments
                attachment_results = prepared.attachment_results
                current_attachments = prepared.current_attachments
                current_attachment_results = prepared.current_attachment_results
                input_content = prepared.input_content
                input_messages = prepared.input_messages
                user_parts = prepared.user_parts
                history = prepared.history
                invocation_id = prepared.invocation_id
                common_metadata = {
                    "modelVersion": model_version,
                    "usageMetadata": {
                        "promptTokenCount": len(user_input),
                        "candidatesTokenCount": 0,
                        "totalTokenCount": len(user_input),
                    },
                }
                await deps.conversation().append_run_status_event(
                    session_id=session_id,
                    author=active_runner.detection_result.name,
                    status="in_progress",
                    invocation_id=invocation_id,
                    session_service_provider=deps.resolve_session_service,
                    run_mode=RUN_MODE_FOREGROUND,
                    run_trigger=RUN_TRIGGER_NEW_RUN,
                )

                client_visible_text = ""
                authoritative_text = ""
                responses_output: list[Any] = []
                responses_response_id: str | None = None
                stream_iter = active_runner.stream(
                    {
                        "session_id": session_id,
                        "input": user_input,
                        "history": history,
                        "input_content": list(input_content),
                        "input_messages": list(input_messages),
                        "input_parts": list(user_parts),
                        "attachments": attachments,
                        "attachment_results": attachment_results,
                        "current_attachments": current_attachments,
                        "current_attachment_results": current_attachment_results,
                        "has_current_files": prepared.has_current_files,
                        "model": request.model,
                    }
                )
                async for kind, chunk in _iter_with_idle_heartbeat(stream_iter):
                    if kind == "heartbeat":
                        yield ": ping\n\n"
                        continue
                    event_id = str(uuid.uuid4())
                    if chunk.get("type") == "responses_output":
                        raw_output = chunk.get("output")
                        responses_output = raw_output if isinstance(raw_output, list) else []
                        raw_response_id = chunk.get("response_id")
                        responses_response_id = (
                            str(raw_response_id) if raw_response_id else responses_response_id
                        )
                        continue
                    if chunk.get("type") == "thinking":
                        delta = str(chunk.get("delta", ""))
                        if delta:
                            await deps.conversation().append_reasoning_event(
                                session_id=session_id,
                                author=active_runner.detection_result.name,
                                text=delta,
                                invocation_id=invocation_id,
                                session_service_provider=deps.resolve_session_service,
                            )
                            yield (
                                "event: response.reasoning.delta\n"
                                f"data: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"
                            )
                        continue
                    if chunk.get("type") == "text":
                        delta_text = chunk.get("delta", "")
                        client_visible_text += delta_text
                        authoritative_text = client_visible_text
                        response_event = {
                            "id": event_id,
                            "author": chunk.get("node", active_runner.detection_result.name),
                            "sessionId": session_id,
                            "invocationId": invocation_id,
                            "content": {"role": "model", "parts": [{"text": delta_text}]},
                            "partial": True,
                            "timestamp": int(time.time() * 1000),
                        }
                        yield f"data: {json.dumps(response_event, ensure_ascii=False)}\n\n"
                        continue
                    if chunk.get("type") == "tool_call":
                        yield (
                            "event: response.tool_call\n"
                            "data: "
                            + json.dumps(
                                {
                                    "name": chunk.get("tool_name"),
                                    "args": chunk.get("tool_args", {}),
                                },
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        )
                        tool_event = {
                            "id": event_id,
                            "author": chunk.get("node", "tool"),
                            "sessionId": session_id,
                            "invocationId": invocation_id,
                            "content": {
                                "role": "model",
                                "parts": [
                                    {
                                        "functionCall": {
                                            "name": chunk.get("tool_name", "unknown"),
                                            "args": chunk.get("tool_args", {}),
                                        }
                                    }
                                ],
                            },
                            "actions": {
                                "finishReason": "STOP",
                                "stateDelta": {},
                            },
                            "modelVersion": common_metadata["modelVersion"],
                            "timestamp": int(time.time() * 1000),
                        }
                        yield f"data: {json.dumps(tool_event, ensure_ascii=False)}\n\n"
                        await deps.conversation().append_conversation_event(
                            session_id=session_id,
                            author=chunk.get("node", "tool"),
                            role="model",
                            text="",
                            invocation_id=invocation_id,
                            event_type="tool_call",
                            session_service_provider=deps.resolve_session_service,
                            metadata={
                                "tool_name": chunk.get("tool_name", "unknown"),
                                "tool_args": chunk.get("tool_args", {}),
                            },
                        )
                        continue
                    if chunk.get("type") == "tool_result":
                        await deps.conversation().append_conversation_event(
                            session_id=session_id,
                            author=active_runner.detection_result.name,
                            role="user",
                            text=str(chunk.get("tool_output", "")),
                            invocation_id=invocation_id,
                            event_type="tool_result",
                            session_service_provider=deps.resolve_session_service,
                            metadata={
                                "tool_name": chunk.get("tool_name"),
                                "tool_output": chunk.get("tool_output", {}),
                            },
                        )
                        yield (
                            "event: response.tool_result\n"
                            "data: "
                            + json.dumps(
                                {
                                    "name": chunk.get("tool_name"),
                                    "output": chunk.get("tool_output", {}),
                                },
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        )
                        continue
                    if chunk.get("type") == "interrupt":
                        await deps.conversation().append_conversation_event(
                            session_id=session_id,
                            author=active_runner.detection_result.name,
                            role="model",
                            text="approval requested",
                            invocation_id=invocation_id,
                            event_type="approval_request",
                            session_service_provider=deps.resolve_session_service,
                            metadata={"interrupt_info": chunk.get("interrupt_info")},
                        )
                        yield (
                            "event: response.approval_request\n"
                            "data: "
                            + json.dumps(
                                {"interrupt_info": chunk.get("interrupt_info")},
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        )
                        continue
                    if chunk.get("type") == "final":
                        final_text = chunk.get("output", "")
                        if not final_text:
                            continue
                        authoritative_text = final_text
                        if final_text != client_visible_text:
                            final_event = {
                                "id": event_id,
                                "author": active_runner.detection_result.name,
                                "sessionId": session_id,
                                "invocationId": invocation_id,
                                "content": {"role": "model", "parts": [{"text": final_text}]},
                                "actions": {"finishReason": "STOP"},
                                "modelVersion": common_metadata["modelVersion"],
                                "usageMetadata": {
                                    "promptTokenCount": len(user_input),
                                    "candidatesTokenCount": len(final_text),
                                    "totalTokenCount": len(user_input) + len(final_text),
                                },
                                "timestamp": int(time.time() * 1000),
                            }
                            yield f"data: {json.dumps(final_event, ensure_ascii=False)}\n\n"
                            client_visible_text = final_text

                if authoritative_text:
                    await deps.conversation().append_conversation_event(
                        session_id=session_id,
                        author=active_runner.detection_result.name,
                        role="model",
                        text=authoritative_text,
                        invocation_id=invocation_id,
                        event_type="assistant_message",
                        metadata={
                            **({"responses_output": responses_output} if responses_output else {}),
                            **(
                                {"response_id": responses_response_id}
                                if responses_response_id
                                else {}
                            ),
                        },
                        session_service_provider=deps.resolve_session_service,
                    )
                await deps.conversation().append_run_status_event(
                    session_id=session_id,
                    author=active_runner.detection_result.name,
                    status="completed",
                    invocation_id=invocation_id,
                    session_service_provider=deps.resolve_session_service,
                    run_mode=RUN_MODE_FOREGROUND,
                    run_trigger=RUN_TRIGGER_NEW_RUN,
                )

            except Exception:
                logger.exception("Error in stream")
                public_error = "The agent run failed. See server logs for details."
                await deps.conversation().append_run_status_event(
                    session_id=session_id,
                    author=active_runner.detection_result.name,
                    status="failed",
                    invocation_id=invocation_id,
                    detail=public_error,
                    session_service_provider=deps.resolve_session_service,
                    run_mode=RUN_MODE_FOREGROUND,
                    run_trigger=RUN_TRIGGER_NEW_RUN,
                )
                error_event = {
                    "id": str(uuid.uuid4()),
                    "sessionId": session_id,
                    "invocationId": invocation_id,
                    "error": "agent_run_failed",
                    "errorMessage": public_error,
                    "timestamp": int(time.time() * 1000),
                }
                yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ============================================================
# Trace / Debug API (ADK Web Compatible)
# ============================================================


@control_router.post("/builder/app/{app_name}/cancel")
async def cancel_agent_changes(app_name: str):
    """Cancel agent builder changes - stub for ADK-Web"""
    return True
