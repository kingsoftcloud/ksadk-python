from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from ksadk.conversations.reasoning_markup import strip_reasoning_markup
from ksadk.conversations.run_kinds import (
    RUN_MODE_FOREGROUND,
    trigger_from_resume_input,
    validate_run_mode,
)
from ksadk.conversations.runtime_constants import (
    PTL_RETRY_KEEP_TAIL_GROUPS,
)
from ksadk.conversations.runtime_governance import (
    RuntimeCircuitOpen,
    _compact_conversation_history_with_governance,
    _governance_record_turn_start,
    _runtime_governance_from_env,
)
from ksadk.conversations.runtime_input import (
    _auto_save_ltm_turn,
    _build_runner_ambient_contexts,
    _build_runner_request_payload,
    _inject_runner_deferred_tools_for_request,
    _is_prompt_too_long_error,
    _runner_name,
    _runner_type_name,
)
from ksadk.conversations.runtime_metadata import _update_session_metadata_after_assistant_turn
from ksadk.conversations.runtime_observability import (
    _conversation_span_scope,
    _normalize_usage_payload,
    _set_conversation_input_attributes,
    _set_conversation_output_attributes,
    _set_conversation_span_attributes,
    _set_conversation_usage_attributes,
    _span_feedback_metadata,
)
from ksadk.conversations.runtime_persistence import (
    append_conversation_event,
    append_run_checkpoint_event,
    append_run_status_event,
)
from ksadk.conversations.runtime_preparation import _refresh_history, build_run_input
from ksadk.conversations.runtime_resume import (
    _checkpoint_event_args_from_agentengine_metadata,
    _extract_agentengine_metadata,
    _failed_status_for_resume,
    _merge_agentengine_metadata,
)
from ksadk.model_policy import fallback_model_for_exception, model_policy_options_for_model
from ksadk.runtime_context import (
    PlatformInvocationContext,
    platform_invocation_scope,
    tool_execution_scope,
)
from ksadk.sessions import resolve_session_service


async def invoke_conversation_once(
    *,
    runner: Any,
    agent_id: str,
    user_id: str,
    session_id: Optional[str],
    messages: Sequence[Dict[str, Any]],
    model: Optional[str],
    prepare_runner: Callable[[Any, Optional[str]], None],
    model_metadata: Mapping[str, Any] | None = None,
    model_options: Mapping[str, Any] | None = None,
    state_delta: Optional[dict[str, Any]] = None,
    instructions: Optional[str] = None,
    request_metadata: Mapping[str, Any] | None = None,
    custom_metadata: Mapping[str, Any] | None = None,
    resume_input: Mapping[str, Any] | None = None,
    response_id: str | None = None,
    account_id: str | None = None,
    invocation_id: Optional[str] = None,
    session_service_provider: Callable[[], Any] | None = None,
    run_mode: str = RUN_MODE_FOREGROUND,
) -> tuple[str, dict[str, Any]]:
    """非流式 turn 编排入口。

    顺序固定为：写用户事件 -> 需要时 compact -> 写 run_status(in_progress)
    -> 调 runner -> PTL 时 compact/retry -> 写 assistant 结果 -> 写 completed。
    """
    provider = session_service_provider or resolve_session_service
    prepare_runner(runner, model)
    governance = _runtime_governance_from_env()
    _governance_record_turn_start(governance)
    # 入口算 run_trigger（不依赖 prepared，build_run_input 失败时也能用）
    entry_run_mode = validate_run_mode(run_mode)
    entry_run_trigger = trigger_from_resume_input(resume_input)
    try:
        prepared = await build_run_input(
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            messages=messages,
            model=model,
            model_metadata=model_metadata,
            model_options=model_options,
            state_delta=state_delta,
            instructions=instructions,
            request_metadata=request_metadata,
            custom_metadata=custom_metadata,
            resume_input=resume_input,
            invocation_id=invocation_id,
            governance_state=governance,
            session_service_provider=provider,
            run_mode=entry_run_mode,
        )
        # prepared 之后的 run_status 写入复用 prepared 的 mode/trigger
        run_mode = prepared.run_mode
        run_trigger = prepared.run_trigger
    except RuntimeCircuitOpen as exc:
        if session_id:
            await append_run_status_event(
                session_id=session_id,
                author=_runner_name(runner),
                status=_failed_status_for_resume(resume_input),
                invocation_id=invocation_id,
                detail=str(exc),
                metadata={"governance": exc.metadata},
                session_service_provider=provider,
                run_mode=entry_run_mode,
                run_trigger=entry_run_trigger,
            )
        raise
    _inject_runner_deferred_tools_for_request(runner, prepared)
    ambient_contexts = _build_runner_ambient_contexts(
        runner=runner,
        user_id=user_id,
        user_input=prepared.user_input,
    )
    runtime_context = PlatformInvocationContext(
        agent_id=agent_id,
        user_id=user_id,
        account_id=str(account_id or ""),
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
        metadata=dict(custom_metadata or {}),
        model=model,
        model_options=prepared.model_options,
        kb_context=ambient_contexts.get("kb_context"),
        memory_context=ambient_contexts.get("memory_context"),
        tool_approval_mode=str(
            prepared.request_metadata.get("tool_approval_mode") or ""
        ),
    )
    runner_name = _runner_name(runner)
    async with _conversation_span_scope(runner_name) as span:
        _set_conversation_span_attributes(
            span,
            agent_id=agent_id,
            user_id=user_id,
            session_id=prepared.session_id,
            invocation_id=prepared.invocation_id,
            runner_name=runner_name,
            model=model,
            response_id=response_id,
        )
        _set_conversation_input_attributes(span, prepared.user_input or prepared.user_display_input)
        trace_metadata = _span_feedback_metadata(span)
        await append_run_status_event(
            session_id=prepared.session_id,
            author=runner_name,
            status="in_progress",
            invocation_id=prepared.invocation_id,
            session_service_provider=provider,
            run_mode=run_mode,
            run_trigger=run_trigger,
        )

        result: dict[str, Any] | None = None
        last_invoke_error: Exception | None = None
        for attempt in range(2):
            try:
                last_invoke_error = None
                runtime_context.history = list(prepared.history)
                with platform_invocation_scope(runtime_context):
                    with tool_execution_scope(
                        session_id=prepared.session_id,
                        run_id=prepared.invocation_id,
                        invocation_id=prepared.invocation_id,
                    ):
                        result = await runner.invoke(
                            _build_runner_request_payload(
                                prepared=prepared,
                                model=model,
                                runtime_context=runtime_context,
                                runner=runner,
                            )
                        )
                break
            except asyncio.CancelledError:
                await append_run_status_event(
                    session_id=prepared.session_id,
                    author=runner_name,
                    status="cancelled",
                    invocation_id=prepared.invocation_id,
                    detail="cancel_requested",
                    session_service_provider=provider,
                    run_mode=run_mode,
                    run_trigger=run_trigger,
                )
                raise
            except Exception as exc:
                if attempt == 0 and _is_prompt_too_long_error(exc):
                    try:
                        checkpoint = await _compact_conversation_history_with_governance(
                            governance,
                            session_id=prepared.session_id,
                            author=runner_name,
                            invocation_id=prepared.invocation_id,
                            model=model,
                            model_metadata=prepared.model_metadata,
                            force=True,
                            trigger="prompt_too_long",
                            keep_tail_groups=PTL_RETRY_KEEP_TAIL_GROUPS,
                            session_service_provider=provider,
                        )
                    except RuntimeCircuitOpen as circuit_exc:
                        await append_run_status_event(
                            session_id=prepared.session_id,
                            author=runner_name,
                            status=_failed_status_for_resume(resume_input),
                            invocation_id=prepared.invocation_id,
                            detail=str(circuit_exc),
                            metadata={"governance": circuit_exc.metadata},
                            session_service_provider=provider,
                            run_mode=run_mode,
                            run_trigger=run_trigger,
                        )
                        raise circuit_exc
                    if checkpoint:
                        prepared = await _refresh_history(
                            prepared, session_service_provider=provider
                        )
                        runtime_context.history = list(prepared.history)
                        continue
                fallback_model = fallback_model_for_exception(exc, current_model=model or "")
                if attempt == 0 and fallback_model:
                    model = fallback_model
                    runtime_context.model = fallback_model
                    runtime_context.model_options = {
                        **prepared.model_options,
                        **model_policy_options_for_model(fallback_model),
                    }
                    prepare_runner(runner, fallback_model)
                    await append_run_status_event(
                        session_id=prepared.session_id,
                        author=runner_name,
                        status="in_progress",
                        invocation_id=prepared.invocation_id,
                        detail=f"fallback_model:{fallback_model}",
                        session_service_provider=provider,
                        run_mode=run_mode,
                        run_trigger=run_trigger,
                    )
                    continue
                last_invoke_error = exc
                await append_run_status_event(
                    session_id=prepared.session_id,
                    author=runner_name,
                    status=_failed_status_for_resume(resume_input),
                    invocation_id=prepared.invocation_id,
                    detail=str(exc),
                    session_service_provider=provider,
                    run_mode=run_mode,
                    run_trigger=run_trigger,
                )
                break

        if last_invoke_error is not None:
            raise last_invoke_error
        result = result or {}
        output_text = strip_reasoning_markup(str(result.get("output", "")))
        result_usage = _normalize_usage_payload(result.get("usage"))
        result_last_usage = _normalize_usage_payload(
            (result.get("metadata") or {}).get("last_usage")
        ) or (result_usage if result_usage else {})
        _set_conversation_output_attributes(span, output_text)
        _set_conversation_usage_attributes(span, result_usage)
        result_agentengine_metadata = _extract_agentengine_metadata(result)
        assistant_metadata: dict[str, Any] = {
            **trace_metadata,
            **_merge_agentengine_metadata(prepared.request_metadata, result_agentengine_metadata),
        }
        if prepared.request_metadata:
            request_metadata_without_agentengine = {
                key: value
                for key, value in prepared.request_metadata.items()
                if key != "agentengine"
            }
            if request_metadata_without_agentengine:
                assistant_metadata["request_metadata"] = request_metadata_without_agentengine
        if result_usage:
            assistant_metadata["usage"] = result_usage
        if result_last_usage:
            assistant_metadata["last_usage"] = result_last_usage
        if response_id:
            assistant_metadata["response_id"] = response_id
        checkpoint_args = _checkpoint_event_args_from_agentengine_metadata(
            (
                assistant_metadata.get("agentengine")
                if isinstance(assistant_metadata, Mapping)
                else None
            ),
            fallback_run_id=prepared.invocation_id,
        )
        if checkpoint_args:
            await append_run_checkpoint_event(
                session_id=prepared.session_id,
                author=runner_name,
                run_id=checkpoint_args["run_id"],
                checkpoint_id=checkpoint_args["checkpoint_id"],
                framework=checkpoint_args["framework"],
                framework_ref=checkpoint_args["framework_ref"],
                phase=checkpoint_args.get("phase") or "completed",
                invocation_id=prepared.invocation_id,
                metadata=checkpoint_args.get("metadata"),
                session_service_provider=provider,
            )
        await append_conversation_event(
            session_id=prepared.session_id,
            author=runner_name,
            role="model",
            text=output_text,
            invocation_id=prepared.invocation_id,
            event_type="assistant_message",
            metadata=assistant_metadata or None,
            session_service_provider=provider,
        )
        await _update_session_metadata_after_assistant_turn(
            service=provider(),
            session_id=prepared.session_id,
            assistant_text=output_text,
            model=model,
        )
        await _auto_save_ltm_turn(
            agent_id=agent_id,
            user_id=user_id,
            prepared=prepared,
            output_text=output_text,
            runner_type=runtime_context.runner_type,
            model=model,
        )
        await append_run_status_event(
            session_id=prepared.session_id,
            author=runner_name,
            status="completed",
            invocation_id=prepared.invocation_id,
            session_service_provider=provider,
            run_mode=run_mode,
            run_trigger=run_trigger,
        )
        result_payload: dict[str, Any] = {
            "output_text": output_text,
            "model": model,
            "metadata": {
                **trace_metadata,
                **{
                    key: value
                    for key, value in prepared.request_metadata.items()
                    if key != "agentengine"
                },
                **_merge_agentengine_metadata(
                    prepared.request_metadata, result_agentengine_metadata
                ),
            },
        }
        if result_usage:
            result_payload["usage"] = result_usage
            result_payload["metadata"]["usage"] = result_usage
        if result_last_usage:
            result_payload["metadata"]["last_usage"] = result_last_usage
        if response_id:
            result_payload["response_id"] = response_id
        return prepared.session_id, result_payload


def _response_sse(event: str, data: Mapping[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(dict(data), ensure_ascii=False)}\n\n"
