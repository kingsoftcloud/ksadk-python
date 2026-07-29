from __future__ import annotations

import os
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from ksadk.conversations.attachments import compact_attachment_result_for_session
from ksadk.conversations.context import (
    build_history_from_events,
    build_request_history,
    build_responses_history_from_messages,
    project_responses_history,
)
from ksadk.conversations.model_options import normalize_model_options
from ksadk.conversations.normalize import (
    compact_attachment_for_session,
)
from ksadk.conversations.run_kinds import (
    RUN_MODE_FOREGROUND,
    RUN_TRIGGER_APPROVAL_RESUME,
    RUN_TRIGGER_CHECKPOINT_RESUME,
    trigger_from_resume_input,
    validate_run_mode,
)
from ksadk.conversations.runtime_governance import (
    RuntimeGovernanceState,
    _compact_conversation_history_with_governance,
    _governance_record_approval_response,
)
from ksadk.conversations.runtime_input import (
    _merge_request_history_with_session_history,
    _merge_responses_history_with_session_history,
)
from ksadk.conversations.runtime_metadata import (
    _build_attachment_context_state_delta,
    _canonical_input_messages,
    _latest_user_turn,
    _normalized_conversation_messages,
    _parts_include_file,
    _resolve_effective_attachment_context,
    _resolve_runtime_model_metadata,
    _update_session_metadata_after_user_turn,
    _user_event_content,
)
from ksadk.conversations.runtime_observability import _latest_deferred_tool_names
from ksadk.conversations.runtime_payloads import PreparedConversationTurn
from ksadk.conversations.runtime_persistence import (
    append_conversation_event,
    append_run_resume_event,
    append_run_status_event,
    ensure_conversation_session,
)
from ksadk.conversations.runtime_resume import (
    _agentengine_resume_metadata,
    _approval_decision_from_resume,
    _approval_resume_run_mode,
    _consecutive_approval_denials_from_events,
    _execute_approved_builtin_tool_resume,
    _find_tool_receipt_event_by_key,
    _format_resume_response_text,
    _has_pending_approval,
    _is_approval_resume_input,
    _is_checkpoint_resume_input,
    _normalize_approval_resume_input,
    _normalize_checkpoint_resume_input,
    _tool_receipt_idempotency_key_for_resume,
)
from ksadk.ids import new_run_id
from ksadk.model_policy import model_policy_options_for_model
from ksadk.sessions import resolve_session_service


async def build_run_input(
    *,
    agent_id: str,
    user_id: str,
    session_id: Optional[str],
    messages: Sequence[Dict[str, Any]],
    model: Optional[str] = None,
    model_metadata: Mapping[str, Any] | None = None,
    model_options: Mapping[str, Any] | None = None,
    state_delta: Optional[dict[str, Any]] = None,
    instructions: Optional[str] = None,
    request_metadata: Mapping[str, Any] | None = None,
    custom_metadata: Mapping[str, Any] | None = None,
    resume_input: Mapping[str, Any] | None = None,
    invocation_id: Optional[str] = None,
    governance_state: RuntimeGovernanceState | None = None,
    session_service_provider: Callable[[], Any] | None = None,
    run_mode: str = RUN_MODE_FOREGROUND,
) -> PreparedConversationTurn:
    """构建一次 turn 的标准运行输入，并在进入模型前做上下文投影/压缩。

    run_mode 由 caller 按 endpoint 语义传入（Background:true→background，普通→foreground）；
    run_trigger 由 resume_input 推导（new_run/checkpoint_resume/approval_resume）。
    """
    caller_run_mode = validate_run_mode(run_mode)
    caller_run_trigger = trigger_from_resume_input(resume_input)
    provider = session_service_provider or resolve_session_service
    service = provider()
    resolved_user_id = user_id
    if session_id:
        existing_session = await service.get_session(session_id)
        if existing_session and existing_session.user_id:
            resolved_user_id = existing_session.user_id

    session = await ensure_conversation_session(
        agent_id=agent_id,
        user_id=resolved_user_id,
        session_id=session_id,
        session_service_provider=provider,
    )
    resolved_session_id = session.id
    resolved_invocation_id = str(invocation_id or new_run_id(resolved_session_id))
    resolved_model_metadata = await _resolve_runtime_model_metadata(
        model,
        model_metadata=model_metadata,
    )
    normalized_request_metadata = dict(request_metadata or {})
    normalized_custom_metadata = dict(custom_metadata or {})
    policy_model = model or os.getenv("OPENAI_MODEL_NAME") or os.getenv("MODEL_NAME")
    normalized_model_options = {
        **normalize_model_options(model_options),
        **model_policy_options_for_model(policy_model or ""),
    }
    normalized_instructions = str(instructions or "").strip()

    if resume_input is not None:
        if not session_id:
            raise ValueError("Responses resume input requires session_id")
        existing_events = await service.get_events(resolved_session_id)
        normalized_resume_input = dict(resume_input)
        if _is_checkpoint_resume_input(normalized_resume_input):
            normalized_resume_input = _normalize_checkpoint_resume_input(normalized_resume_input)
            await append_run_resume_event(
                session_id=resolved_session_id,
                author=agent_id,
                run_id=str(normalized_resume_input["run_id"]),
                checkpoint_id=str(normalized_resume_input["checkpoint_id"]),
                resume_attempt_id=str(normalized_resume_input["resume_attempt_id"]),
                framework=str(normalized_resume_input["framework"]),
                framework_ref=normalized_resume_input["framework_ref"],
                invocation_id=resolved_invocation_id,
                session_service_provider=provider,
            )
            # 补写 run_status(resuming)：让 ActiveRunStatus 在 resume 期间正确反映"恢复中"。
            # append_run_resume_event 写的是 run_resume 事件（status=resuming），而
            # _latest_session_run_status 只扫 run_status 事件 → 不补写则
            # resuming 不进 ActiveRunStatus。
            await append_run_status_event(
                session_id=resolved_session_id,
                author=agent_id,
                status="resuming",
                invocation_id=resolved_invocation_id,
                detail="checkpoint_resume",
                session_service_provider=provider,
                run_mode=caller_run_mode,
                run_trigger=RUN_TRIGGER_CHECKPOINT_RESUME,
            )
            event_history = await service.get_events(resolved_session_id)
            history = build_history_from_events(event_history)
            responses_history = project_responses_history(event_history)
            return PreparedConversationTurn(
                session_id=resolved_session_id,
                invocation_id=resolved_invocation_id,
                user_input="",
                user_display_input="",
                history=history,
                responses_history=responses_history,
                input_content=[],
                input_messages=[],
                user_parts=[],
                attachments=[],
                attachment_results=[],
                current_attachments=[],
                current_attachment_results=[],
                has_current_files=False,
                model_metadata=resolved_model_metadata,
                model_options=normalized_model_options,
                instructions=normalized_instructions,
                request_metadata={
                    **normalized_request_metadata,
                    **_agentengine_resume_metadata(normalized_resume_input),
                },
                resume_input=normalized_resume_input,
                run_mode=caller_run_mode,
                run_trigger=RUN_TRIGGER_CHECKPOINT_RESUME,
            )

        is_approval_resume = _is_approval_resume_input(normalized_resume_input)
        existing_tool_receipt_event = None
        if is_approval_resume and not _has_pending_approval(existing_events):
            replay_candidate = _normalize_approval_resume_input(
                normalized_resume_input,
                existing_events,
                include_resolved=True,
            )
            receipt_key = _tool_receipt_idempotency_key_for_resume(
                session_id=resolved_session_id,
                resume_input=replay_candidate,
            )
            if receipt_key:
                existing_tool_receipt_event = _find_tool_receipt_event_by_key(
                    existing_events,
                    receipt_key,
                )
            if existing_tool_receipt_event is not None:
                normalized_resume_input = replay_candidate
            else:
                raise ValueError("Responses resume input requires a pending approval_request")
        if is_approval_resume:
            normalized_resume_input = _normalize_approval_resume_input(
                normalized_resume_input,
                existing_events,
            )
            caller_run_mode = _approval_resume_run_mode(
                existing_events,
                normalized_resume_input,
                fallback=caller_run_mode,
            )
            if governance_state is not None:
                governance_state.consecutive_approval_denials = (
                    _consecutive_approval_denials_from_events(existing_events)
                )

        resume_text = _format_resume_response_text(normalized_resume_input)
        resume_event_metadata: dict[str, Any] = {"resume_input": normalized_resume_input}
        if normalized_resume_input.get("call_id"):
            resume_event_metadata["tool_call_id"] = str(normalized_resume_input["call_id"])
        if "output" in normalized_resume_input:
            resume_event_metadata["tool_output"] = normalized_resume_input.get("output")
        await append_conversation_event(
            session_id=resolved_session_id,
            author="user",
            role="user",
            text=resume_text,
            invocation_id=resolved_invocation_id,
            event_type="approval_response" if is_approval_resume else "tool_result",
            session_service_provider=provider,
            metadata=resume_event_metadata,
        )
        if is_approval_resume and governance_state is not None:
            decision = normalized_resume_input.get("approval")
            if not isinstance(decision, Mapping):
                decision = _approval_decision_from_resume(normalized_resume_input)
            _governance_record_approval_response(governance_state, decision)
        tool_resume_input = None
        if is_approval_resume:
            tool_resume_input = await _execute_approved_builtin_tool_resume(
                session_id=resolved_session_id,
                invocation_id=resolved_invocation_id,
                resume_input=normalized_resume_input,
                session_service_provider=provider,
                existing_events=existing_events,
            )
        effective_resume_input = tool_resume_input or normalized_resume_input
        del existing_events
        event_history = await service.get_events(resolved_session_id)
        history = build_history_from_events(event_history)
        responses_history = project_responses_history(event_history)
        return PreparedConversationTurn(
            session_id=resolved_session_id,
            invocation_id=resolved_invocation_id,
            user_input=resume_text,
            user_display_input=resume_text,
            history=history,
            responses_history=responses_history,
            input_content=[],
            input_messages=[],
            user_parts=[],
            attachments=[],
            attachment_results=[],
            current_attachments=[],
            current_attachment_results=[],
            has_current_files=False,
            model_metadata=resolved_model_metadata,
            model_options=normalized_model_options,
            instructions=normalized_instructions,
            request_metadata=normalized_request_metadata,
            resume_input=effective_resume_input,
            run_mode=caller_run_mode,
            run_trigger=RUN_TRIGGER_APPROVAL_RESUME,
        )

    normalized_messages = _normalized_conversation_messages(messages)
    (
        user_input,
        user_display_input,
        input_content,
        user_parts,
        attachments,
        attachment_results,
    ) = _latest_user_turn(normalized_messages)
    input_messages = _canonical_input_messages(normalized_messages)
    effective_attachments, effective_attachment_results = _resolve_effective_attachment_context(
        normalized_messages=normalized_messages,
        session=session,
    )
    effective_state_delta = _build_attachment_context_state_delta(
        base_state_delta=state_delta,
        attachments=attachments,
        attachment_results=attachment_results,
    )
    event_metadata: dict[str, Any] = {
        "agent_input": user_input,
        "attachments": [compact_attachment_for_session(item) for item in attachments if item],
        "attachment_results": [
            compact_attachment_result_for_session(item) for item in attachment_results if item
        ],
    }

    if normalized_instructions:
        event_metadata["instructions"] = normalized_instructions
    deferred_tool_names = _latest_deferred_tool_names(await service.get_events(resolved_session_id))
    if deferred_tool_names and "deferred_tool_names" not in normalized_request_metadata:
        normalized_request_metadata["deferred_tool_names"] = deferred_tool_names
    if normalized_custom_metadata:
        event_metadata["request_metadata"] = normalized_custom_metadata
    if normalized_request_metadata:
        event_metadata["runtime_metadata"] = normalized_request_metadata

    await append_conversation_event(
        session_id=resolved_session_id,
        author="user",
        role="user",
        text=user_display_input or user_input,
        invocation_id=resolved_invocation_id,
        event_type="user_message",
        state_delta=effective_state_delta,
        content=_user_event_content(
            user_input=user_input,
            user_display_input=user_display_input,
            input_content=input_content,
            user_parts=user_parts,
        ),
        session_service_provider=provider,
        metadata=event_metadata,
    )
    await _update_session_metadata_after_user_turn(
        service=service,
        session=session,
        user_input=user_input or user_display_input,
    )

    checkpoint = await _compact_conversation_history_with_governance(
        governance_state,
        session_id=resolved_session_id,
        author=agent_id,
        invocation_id=resolved_invocation_id,
        model=model,
        model_metadata=resolved_model_metadata,
        session_service_provider=provider,
    )
    event_history = await service.get_events(resolved_session_id)
    history = build_history_from_events(event_history)
    responses_history = project_responses_history(event_history)
    request_history = build_request_history(normalized_messages[:-1])
    # Gateway / Responses callers may send full prompt context while the
    # runtime-local session is empty or stale (for example after pod
    # replacement). Preserve that request context, but do not duplicate it when
    # local session events already contain the same prefix.
    history = _merge_request_history_with_session_history(request_history, history)
    request_responses_history = build_responses_history_from_messages(normalized_messages[:-1])
    responses_history = _merge_responses_history_with_session_history(
        request_responses_history,
        responses_history,
    )

    return PreparedConversationTurn(
        session_id=resolved_session_id,
        invocation_id=resolved_invocation_id,
        user_input=user_input,
        user_display_input=user_display_input or user_input,
        history=history,
        request_history=request_history,
        request_responses_history=request_responses_history,
        responses_history=responses_history,
        input_content=input_content,
        input_messages=input_messages,
        user_parts=user_parts,
        attachments=effective_attachments,
        attachment_results=effective_attachment_results,
        current_attachments=attachments,
        current_attachment_results=attachment_results,
        has_current_files=bool(attachments or _parts_include_file(user_parts)),
        model_metadata=resolved_model_metadata,
        model_options=normalized_model_options,
        instructions=normalized_instructions,
        request_metadata=normalized_request_metadata,
        compaction_triggered=checkpoint is not None,
        compaction_trigger=(
            str((checkpoint.metadata or {}).get("trigger") or "auto") if checkpoint else None
        ),
        compacted_until_seq_id=(
            int((checkpoint.metadata or {}).get("compacted_until_seq_id") or 0)
            if checkpoint
            else None
        ),
        run_mode=caller_run_mode,
        run_trigger=caller_run_trigger,
    )


async def _refresh_history(
    prepared: PreparedConversationTurn, *, session_service_provider: Callable[[], Any] | None = None
) -> PreparedConversationTurn:
    """在 compaction 后刷新 prepared turn 的 history 视图。"""
    provider = session_service_provider or resolve_session_service
    service = provider()
    event_history = await service.get_events(prepared.session_id)
    prepared.history = _merge_request_history_with_session_history(
        prepared.request_history,
        build_history_from_events(event_history),
    )
    prepared.responses_history = _merge_responses_history_with_session_history(
        prepared.request_responses_history,
        project_responses_history(event_history),
    )
    return prepared
