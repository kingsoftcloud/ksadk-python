"""Shared conversation preprocessing for RuntimeAdapter-backed transports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ksadk.conversations.runtime_input import (
    _build_runner_ambient_contexts,
    _build_runner_request_payload,
    _inject_runner_deferred_tools_for_request,
    _runner_type_name,
)
from ksadk.conversations.runtime_preparation import build_run_input
from ksadk.runtime.adapter import (
    CONVERSATION_PREPROCESSING_METADATA_KEY,
    StartRequest,
)
from ksadk.runtime_context import PlatformInvocationContext


@dataclass
class PreparedRuntimeStart:
    """Runner payload plus the ContextVar/tracing identity used to execute it."""

    runner_input: dict[str, Any]
    context: PlatformInvocationContext
    input_text: str
    response_id: str | None = None


def _fallback_messages(input_value: Any) -> list[dict[str, Any]]:
    if isinstance(input_value, list) and all(isinstance(item, dict) for item in input_value):
        return [dict(item) for item in input_value]
    if isinstance(input_value, dict) and isinstance(input_value.get("messages"), list):
        return [dict(item) for item in input_value["messages"] if isinstance(item, dict)]
    return [{"role": "user", "content": input_value}]


async def prepare_runtime_start(request: StartRequest, runner: Any) -> PreparedRuntimeStart | None:
    """Prepare an opted-in StartRequest through the canonical conversation pipeline.

    Requests without ``conversation_request`` retain the frozen RuntimeAdapter v1
    behavior.  This keeps A2A, Harness and framework contract callers unchanged while
    allowing AG-UI and future transports to share the production input semantics.
    """

    conversation = request.conversation_preprocessing()
    if conversation is None:
        return None

    prepare_for_request = getattr(runner, "prepare_for_request", None)
    if callable(prepare_for_request):
        prepare_for_request(request.model)

    outer_metadata = {
        key: value
        for key, value in request.metadata.items()
        if key != CONVERSATION_PREPROCESSING_METADATA_KEY
    }
    request_metadata = {**outer_metadata, **conversation.request_metadata}
    messages = conversation.messages or _fallback_messages(request.input)
    prepared = await build_run_input(
        agent_id=str(request.agent_id or "agent"),
        user_id=request.user_id,
        session_id=request.session_id,
        messages=messages,
        model=request.model,
        model_metadata=conversation.model_metadata or None,
        model_options=conversation.model_options or None,
        state_delta=conversation.state_delta or None,
        instructions=conversation.instructions,
        request_metadata=request_metadata,
        custom_metadata=conversation.custom_metadata,
        invocation_id=str(request.metadata.get("invocation_id") or "") or None,
    )
    _inject_runner_deferred_tools_for_request(runner, prepared)
    ambient_contexts = _build_runner_ambient_contexts(
        runner=runner,
        user_id=request.user_id,
        user_input=prepared.user_input,
    )
    runtime_context = PlatformInvocationContext(
        agent_id=str(request.agent_id or "agent"),
        user_id=request.user_id,
        account_id=str(conversation.account_id or ""),
        session_id=prepared.session_id,
        history=list(prepared.history),
        input_content=list(prepared.input_content),
        input_messages=list(prepared.input_messages),
        input_parts=list(prepared.user_parts),
        attachments=list(prepared.attachments),
        attachment_results=list(prepared.attachment_results),
        current_attachments=list(prepared.current_attachments),
        current_attachment_results=list(prepared.current_attachment_results),
        has_current_files=prepared.has_current_files,
        runner_type=_runner_type_name(runner),
        metadata=dict(conversation.custom_metadata),
        model=request.model,
        model_options=prepared.model_options,
        kb_context=ambient_contexts.get("kb_context"),
        memory_context=ambient_contexts.get("memory_context"),
    )
    canonical_payload = _build_runner_request_payload(
        prepared=prepared,
        model=request.model,
        runtime_context=runtime_context,
        runner=runner,
    )
    return PreparedRuntimeStart(
        runner_input={**request.config, **canonical_payload},
        context=runtime_context,
        input_text=prepared.user_input or prepared.user_display_input,
        response_id=conversation.response_id,
    )


__all__ = ["PreparedRuntimeStart", "prepare_runtime_start"]
