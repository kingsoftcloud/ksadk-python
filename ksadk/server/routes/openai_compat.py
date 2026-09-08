"""OpenAI Responses and Chat Completions compatibility routes."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ksadk.conversations.normalize import normalize_kop_messages, normalize_responses_input
from ksadk.conversations.run_kinds import RUN_MODE_FOREGROUND, trigger_from_resume_input
from ksadk.conversations.runtime_payloads import (
    build_chat_completions_payload,
    build_responses_payload,
    extract_responses_resume_input,
)
from ksadk.conversations.runtime_streaming import (
    stream_runtime_conversation_turn,
    stream_runtime_responses_conversation_turn,
)
from ksadk.kernel.ingress import kernel_route_active
from ksadk.runtime.conversation_execution import invoke_runtime_conversation_once
from ksadk.server.factory import get_runtime_execution

from . import dependencies as deps
from .checkpoint_resolution import _resolve_checkpoint_resume_input_from_session
from .kernel_ingress import (
    _kernel_error_response,
    _kernel_submit,
    kernel_conversation_turn,
    kernel_stream_response,
)
from .models import (
    ResponsesRequest,
    _clean_optional_string,
    _metadata_invocation_id,
    _resolve_responses_session_and_user,
    _runtime_agent_id,
    _split_custom_metadata,
)
from .routers import openai_compat_router
from .streaming import (
    _detached_resume_key_from_input,
    _reject_if_detached_resume_active,
)
from .workspace import _build_models_payload


class ChatCompletionRequest(BaseModel):
    messages: List[Dict[str, Any]]
    model: Optional[str] = None
    model_metadata: Optional[Dict[str, Any]] = None
    model_options: Optional[Dict[str, Any]] = None
    stream: bool = False
    session_id: Optional[str] = None
    user: Optional[str] = None
    account_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None


@openai_compat_router.get("/v1/models")
async def list_openai_models():
    """Expose the current model catalog through the OpenAI-compatible path."""

    payload = await _build_models_payload()
    try:
        _executor, launch_context = get_runtime_execution()
    except HTTPException:
        launch_context = None
    if launch_context is not None:
        config = dict(launch_context.config)
        raw_allowed = (
            config.get("models") or config.get("allowed_models") or config.get("allowedModels")
        )
        if isinstance(raw_allowed, (list, tuple, set)):
            allowed = {str(item).strip() for item in raw_allowed if str(item).strip()}
            default_model = str(config.get("model") or "").strip()
            if default_model:
                allowed.add(default_model)
            payload = dict(payload)
            payload["data"] = [
                item
                for item in payload.get("data", [])
                if str(item.get("id") or "").strip() in allowed
            ]
    return {
        "object": "list",
        "data": payload.get("data", []),
        "current": payload.get("current"),
        "source": payload.get("source", ""),
    }


@openai_compat_router.post("/v1/responses")
async def responses(request: ResponsesRequest):
    """OpenAI Responses 兼容接口。"""
    executor, launch_context = get_runtime_execution()
    if kernel_route_active():
        return await _kernel_responses(request, launch_context)
    resolved_session_id, resolved_user_id = _resolve_responses_session_and_user(request)
    agent_id = _runtime_agent_id(launch_context)

    resume_input = extract_responses_resume_input(request.input)
    resume_input = await _resolve_checkpoint_resume_input_from_session(
        service=deps.resolve_session_service(),
        agent_id=agent_id,
        session_id=resolved_session_id,
        resume_input=resume_input,
    )
    messages = [] if resume_input is not None else normalize_responses_input(request.input)
    custom_metadata, request_metadata = _split_custom_metadata(request.metadata)
    if request.previous_response_id:
        request_metadata["previous_response_id"] = request.previous_response_id
    if request.prompt_cache_key:
        request_metadata["prompt_cache_key"] = request.prompt_cache_key
    if request.safety_identifier:
        request_metadata["safety_identifier"] = request.safety_identifier
    if request.user:
        request_metadata["user"] = request.user
    if request.conversation is not None:
        request_metadata["conversation"] = request.conversation
    if request.store is not None:
        request_metadata["store"] = request.store
    account_id = _clean_optional_string(request.account_id)
    invocation_id = _metadata_invocation_id(request_metadata)

    if request.stream:
        runtime_preparation = (
            None if resume_input is not None else await executor.prepare_start(launch_context)
        )
        resume_key = _detached_resume_key_from_input(resolved_session_id, resume_input)
        _reject_if_detached_resume_active(resume_key)
        return deps.detached_streaming_response(
            stream_runtime_responses_conversation_turn(
                executor=executor,
                launch_context=launch_context,
                agent_id=agent_id,
                user_id=resolved_user_id,
                messages=messages,
                session_id=resolved_session_id,
                model=request.model,
                model_metadata=request.model_metadata,
                model_options=request.model_options,
                instructions=request.instructions,
                request_metadata=request_metadata,
                custom_metadata=custom_metadata,
                resume_input=resume_input,
                account_id=account_id,
                invocation_id=invocation_id,
                session_service_provider=deps.resolve_session_service,
                run_mode=RUN_MODE_FOREGROUND,
                runtime_preparation=runtime_preparation,
            ),
            invocation_id=invocation_id,
            resume_key=resume_key,
            run_mode=RUN_MODE_FOREGROUND,
            run_trigger=trigger_from_resume_input(resume_input),
        )

    response_id = f"resp_{uuid.uuid4().hex}"
    resolved_session_id, result = await invoke_runtime_conversation_once(
        executor=executor,
        launch_context=launch_context,
        agent_id=agent_id,
        user_id=resolved_user_id,
        messages=messages,
        session_id=resolved_session_id,
        model=request.model,
        model_metadata=request.model_metadata,
        model_options=request.model_options,
        instructions=request.instructions,
        request_metadata=request_metadata,
        custom_metadata=custom_metadata,
        resume_input=resume_input,
        response_id=response_id,
        account_id=account_id,
        invocation_id=invocation_id,
        session_service_provider=deps.resolve_session_service,
        run_mode=RUN_MODE_FOREGROUND,
    )
    return build_responses_payload(
        output_text=result["output_text"],
        model=request.model,
        session_id=resolved_session_id,
        response_id=response_id,
        metadata=custom_metadata,
        usage=result.get("usage") if isinstance(result.get("usage"), Mapping) else None,
    )


async def _kernel_responses(request: ResponsesRequest, launch_context):
    """kernel 路径（灰度 opt-in）：Responses -> AgentControlCommand -> receipt。"""

    from ksadk.conversations.runtime_persistence import ensure_conversation_session

    resolved_session_id, resolved_user_id = _resolve_responses_session_and_user(request)
    session = await ensure_conversation_session(
        agent_id=_runtime_agent_id(launch_context),
        user_id=resolved_user_id,
        session_id=resolved_session_id,
        session_service_provider=deps.resolve_session_service,
    )
    session_id = session.id
    metadata = request.metadata if isinstance(request.metadata, dict) else {}
    idempotency_key = (
        _clean_optional_string(metadata.get("idempotency_key"))
        or _metadata_invocation_id(metadata)
        or f"resp_{uuid.uuid4().hex}"
    )
    messages = normalize_responses_input(request.input)
    response_id = f"resp_{uuid.uuid4().hex}"
    receipt, trusted = await _kernel_submit(
        mapper="map_responses_request",
        session_id=session_id,
        idempotency_key=idempotency_key,
        content=messages,
        correlation_ref=response_id,
        source_kind="responses",
    )
    if receipt.status not in ("accepted", "duplicate"):
        return _kernel_error_response(receipt)

    def build_payload(output_text: str):
        return build_responses_payload(
            output_text=output_text,
            model=request.model,
            session_id=session_id,
            response_id=response_id,
            metadata=None,
            usage=None,
        )

    if request.stream:
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


@openai_compat_router.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    """OpenAI 兼容的聊天补全接口 (支持流式和非流式)"""
    executor, launch_context = get_runtime_execution()
    messages = normalize_kop_messages(request.messages)
    agent_id = _runtime_agent_id(launch_context)
    resolved_user_id = _clean_optional_string(request.user) or "user"
    account_id = _clean_optional_string(request.account_id)
    custom_metadata, request_metadata = _split_custom_metadata(request.metadata)
    invocation_id = _metadata_invocation_id(request_metadata)

    if request.stream:
        runtime_preparation = await executor.prepare_start(launch_context)
        return StreamingResponse(
            stream_runtime_conversation_turn(
                executor=executor,
                launch_context=launch_context,
                agent_id=agent_id,
                user_id=resolved_user_id,
                messages=messages,
                session_id=request.session_id,
                model=request.model,
                model_metadata=request.model_metadata,
                model_options=request.model_options,
                request_metadata=request_metadata,
                custom_metadata=custom_metadata,
                invocation_id=invocation_id,
                account_id=account_id,
                session_service_provider=deps.resolve_session_service,
                run_mode=RUN_MODE_FOREGROUND,
                runtime_preparation=runtime_preparation,
            ),
            media_type="text/event-stream",
        )

    resolved_session_id, result = await invoke_runtime_conversation_once(
        executor=executor,
        launch_context=launch_context,
        agent_id=agent_id,
        user_id=resolved_user_id,
        messages=messages,
        session_id=request.session_id,
        model=request.model,
        model_metadata=request.model_metadata,
        model_options=request.model_options,
        request_metadata=request_metadata,
        custom_metadata=custom_metadata,
        invocation_id=invocation_id,
        account_id=account_id,
        session_service_provider=deps.resolve_session_service,
        run_mode=RUN_MODE_FOREGROUND,
    )
    return build_chat_completions_payload(
        output_text=result["output_text"],
        model=request.model,
        session_id=resolved_session_id,
        metadata=result.get("metadata"),
    )
